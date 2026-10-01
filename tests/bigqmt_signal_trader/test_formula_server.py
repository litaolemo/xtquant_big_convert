import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import formula_server as fs


class BsonCodecTest(unittest.TestCase):
    """The built-in codec is the no-dependency path; it must round-trip every
    type this wire carries and stay byte-compatible with pymongo's bson."""

    def _round_trip(self, document):
        return fs._decode_document(fs._encode_document(document.items()), 0)[0]

    def test_round_trips_scalars(self):
        doc = {"s": "平安银行", "i": 42, "big": 2 ** 40, "f": 10.29, "t": True, "f2": False, "n": None}

        self.assertEqual(self._round_trip(doc), doc)

    def test_round_trips_nested_containers(self):
        doc = {"func": "getMarketData", "params": {"fields": ["close", "volume"], "count": -1}}

        self.assertEqual(self._round_trip(doc), doc)

    def test_round_trips_the_actual_request_envelope(self):
        doc = {
            "func": "getMarketData",
            "params": {
                "fields": ["close"],
                "stockCodes": ["000001.SZ"],
                "startTime": "",
                "endTime": "",
                "period": "1d",
                "dividendType": "none",
                "count": 3,
            },
        }

        self.assertEqual(self._round_trip(doc), doc)

    def test_array_order_is_preserved(self):
        doc = {"codes": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"]}

        self.assertEqual(self._round_trip(doc)["codes"], doc["codes"])

    def test_matches_pymongo_bson_when_available(self):
        try:
            import bson
        except ImportError:
            self.skipTest("pymongo bson not installed")
        doc = {"func": "getLastVolume", "params": {"stockCode": "000001.SZ", "n": 1.5}}

        self.assertEqual(fs._encode_document(doc.items()), bson.BSON.encode(doc))
        self.assertEqual(fs._decode_document(bson.BSON.encode(doc), 0)[0], doc)

    def test_unsupported_type_is_rejected(self):
        with self.assertRaises(TypeError):
            fs._encode_document({"bad": object()}.items())


class AddressResolutionTest(unittest.TestCase):
    def test_reads_port_from_formulaserver_ini(self):
        import tempfile

        root = tempfile.mkdtemp()
        ini_dir = os.path.join(root, "config", "formulaserver")
        os.makedirs(ini_dir)
        with open(os.path.join(ini_dir, "formulaserver.ini"), "w") as handle:
            handle.write("[server_formula]\naddress = 0.0.0.0:58600\n")

        self.assertEqual(fs.read_formulaserver_port(root), 58600)
        self.assertEqual(fs.resolve_address({"qmt_root": root}), ("127.0.0.1", 58600))

    def test_missing_ini_falls_back_to_default_port(self):
        self.assertIsNone(fs.read_formulaserver_port(os.path.join(ROOT, "no-such-dir")))
        host, port = fs.resolve_address({"qmt_root": os.path.join(ROOT, "no-such-dir")})

        self.assertEqual((host, port), ("127.0.0.1", fs.DEFAULT_PORT))

    def test_explicit_config_wins(self):
        self.assertEqual(
            fs.resolve_address({"host": "10.0.0.5", "port": 59999}), ("10.0.0.5", 59999)
        )


class FakeClient(object):
    host = "127.0.0.1"
    port = 58600

    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []

    def request(self, func, params=None):
        self.calls.append((func, dict(params or {})))
        if self.error is not None:
            raise self.error
        return self.responses.get(func, {"result": None})

    def close(self):
        pass


class ParamTranslationTest(unittest.TestCase):
    def _router(self, responses=None, error=None):
        client = FakeClient(responses=responses, error=error)
        return fs.FormulaServerRouter(client=client), client

    def test_instrument_detail_is_not_routed_here_anymore(self):
        """#392: FormulaServer's getInstrumentDetail answers TotalVolume /
        OpenDate with zeros or stale snapshots on the Guojin build (43% / 32%
        of a 300-code sample zero, 22% non-zero-but-stale), while the bridge's
        ContextInfo path answered every code correctly. A well-formed wrong
        answer is worse than no answer, so the three instrument methods were
        taken out of METHOD_MAP -- they must be Unroutable here and fall back
        to the RPC bridge."""
        router, client = self._router(
            {"getInstrumentDetail": {"result": {"FloatVolumn": 1.0, "TotalVolumn": 2.0}}}
        )

        for method in ("get_instrument", "get_instrument_detail", "get_instrumentdetail"):
            with self.assertRaises(fs.Unroutable):
                router.call(method, {"code": "000001.SZ"})
        self.assertEqual(client.calls, [])

    def test_sector_normalizes_the_minus_one_sentinel(self):
        router, client = self._router({"getStockListInSector": {"result": ["600000.SH"]}})

        router.call("get_stock_list_in_sector", {"sector_name": "沪深300", "real_timetag": -1})

        self.assertEqual(client.calls[0][1], {"sectorName": "沪深300", "realtime": 0})

    def test_market_data_refuses_adjusted_bars(self):
        """dividendType is not honoured by the server; serving an adjusted
        request from here would hand back unadjusted prices silently."""
        router, client = self._router({"getMarketData": {"result": []}})

        for dividend_type in ("front", "back", "front_ratio"):
            with self.assertRaises(fs.Unroutable):
                router.call(
                    "get_market_data_ex",
                    {
                        "field_list": ["close"],
                        "stock_list": ["000001.SZ"],
                        "dividend_type": dividend_type,
                    },
                )
        self.assertEqual(client.calls, [])

    def test_market_data_refuses_non_bar_periods(self):
        """Non-bar periods must not go through the fastpath: a period='tick'
        getMarketData request wedged the whole FormulaServer behind the shared
        socket lock (drip-fed response, no timeout) on 2026-08-30, taking every
        fastpath read with it. Refuse and let RPC answer."""
        router, client = self._router({"getMarketData": {"result": []}})

        for period in ("tick", "l2quote", "l2order"):
            with self.assertRaises(fs.Unroutable):
                router.call(
                    "get_market_data_ex",
                    {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": period},
                )
        self.assertEqual(client.calls, [])

    def test_market_data_allows_bar_periods(self):
        router, client = self._router({"getMarketData": {"result": []}})

        router.call(
            "get_market_data_ex",
            {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "5m"},
        )

        self.assertEqual(client.calls[0][1]["period"], "5m")

    def test_market_data_allows_unadjusted(self):
        router, client = self._router({"getMarketData": {"result": []}})

        router.call(
            "get_market_data_ex",
            {"field_list": ["close"], "stock_list": ["000001.SZ"], "dividend_type": "none"},
        )

        self.assertEqual(client.calls[0][1]["dividendType"], "none")

    def test_market_data_refuses_tick_and_l2_periods(self):
        # issue #66: FormulaServer 不服务分笔/L2 周期——静默返回空会让调用方
        # 以为本地没有数据（数据其实在）。拒绝路由让 RPC 桥回答。
        router, client = self._router({"getMarketData": {"result": []}})

        for period in ("tick", "l2quote", "l2order"):
            with self.assertRaises(fs.Unroutable):
                router.call(
                    "get_market_data_ex",
                    {
                        "field_list": ["close"],
                        "stock_list": ["000001.SZ"],
                        "period": period,
                        "dividend_type": "none",
                    },
                )
        self.assertEqual(client.calls, [])

        # K 线周期照常走快速路径
        router.call(
            "get_market_data_ex",
            {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "1d",
             "dividend_type": "none"},
        )
        self.assertEqual(len(client.calls), 1)


    def test_market_data_translates_flat_wire_shape(self):
        router, _ = self._router(
            {
                "getMarketData": {
                    "result": [
                        "000001.SZ",
                        ["20260703", ["close", 10.29, "volume", 863327.0]],
                        "600000.SH",
                        ["20260703", ["close", 8.69, "volume", 695133.0]],
                    ]
                }
            }
        )

        out = router.call(
            "get_market_data_ex",
            {"field_list": ["close", "volume"], "stock_list": ["000001.SZ", "600000.SH"]},
        )

        self.assertEqual(out["000001.SZ"]["columns"], ["stime", "close", "volume"])
        self.assertEqual(
            out["000001.SZ"]["records"],
            [{"stime": "20260703", "close": 10.29, "volume": 863327.0}],
        )
        self.assertEqual(out["600000.SH"]["records"][0]["close"], 8.69)

    def test_market_data_keeps_requested_codes_with_no_bars(self):
        router, _ = self._router({"getMarketData": {"result": []}})

        out = router.call(
            "get_market_data_ex",
            {"field_list": ["close"], "stock_list": ["000001.SZ", "600000.SH"]},
        )

        self.assertEqual(sorted(out), ["000001.SZ", "600000.SH"])
        self.assertEqual(out["000001.SZ"]["records"], [])

    def test_missing_required_params_is_unroutable_not_a_crash(self):
        router, client = self._router()

        with self.assertRaises(fs.Unroutable):
            router.call("get_total_share", {})
        self.assertEqual(client.calls, [])


class FallbackBehaviourTest(unittest.TestCase):
    def test_unmapped_method_is_not_supported(self):
        router = fs.FormulaServerRouter(client=FakeClient())

        self.assertFalse(router.supports("get_asset"))
        self.assertFalse(router.supports("submit_order"))
        self.assertFalse(router.supports("get_full_tick"))

    def test_instrument_methods_stay_on_rpc(self):
        """#392: dishonest TotalVolume / OpenDate from getInstrumentDetail."""
        router = fs.FormulaServerRouter(client=FakeClient())

        self.assertFalse(router.supports("get_instrument"))
        self.assertFalse(router.supports("get_instrument_detail"))
        self.assertFalse(router.supports("get_instrumentdetail"))

    def test_trading_dates_and_dividends_stay_on_rpc(self):
        """Their FormulaServer params mean something different from ours."""
        router = fs.FormulaServerRouter(client=FakeClient())

        self.assertFalse(router.supports("get_trading_dates"))
        self.assertFalse(router.supports("get_divid_factors"))
        self.assertFalse(router.supports("get_risk_free_rate"))

    def test_transport_failure_trips_the_cooldown(self):
        router = fs.FormulaServerRouter(
            client=FakeClient(error=fs.FormulaServerUnavailable("down")),
            failure_cooldown_seconds=60,
        )

        with self.assertRaises(fs.Unroutable):
            router.call("get_last_volume", {"stock": "000001.SZ"})
        # Breaker is open: no further attempts until the cooldown expires.
        self.assertFalse(router.supports("get_last_volume"))

    def test_method_not_found_disables_only_that_method(self):
        router = fs.FormulaServerRouter(
            client=FakeClient(
                error=fs.FormulaServerError("nope", error_id=fs.ERROR_METHOD_NOT_FOUND)
            )
        )

        with self.assertRaises(fs.Unroutable):
            router.call("get_main_contract", {"code_market": "IF00.IF"})

        self.assertFalse(router.supports("get_main_contract"))
        self.assertTrue(router.supports("get_last_volume"))  # breaker not tripped

    def test_disabled_router_supports_nothing(self):
        router = fs.build_router({"enabled": False})

        for method in fs.SUPPORTED_METHODS:
            self.assertFalse(router.supports(method))

    def test_enabled_accepts_string_flags(self):
        self.assertFalse(fs.build_router({"enabled": "false"}).enabled)
        self.assertFalse(fs.build_router({"enabled": "0"}).enabled)
        self.assertTrue(fs.build_router({"enabled": "true", "port": 1}).enabled)


class ClientCallIntegrationTest(unittest.TestCase):
    """BigQmtRpcClient.call must prefer the router and fall back cleanly."""

    def _client(self, router):
        from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient

        client = BigQmtRpcClient(account_id="acct")
        client._formula_router_instance = router
        return client

    def test_routed_method_never_touches_rpc(self):
        router = fs.FormulaServerRouter(
            client=FakeClient({"getLastVolume": {"result": 123.0}})
        )
        client = self._client(router)

        def explode(*args, **kwargs):
            raise AssertionError("RPC must not be used for a routed method")

        client._transport = explode

        self.assertEqual(client.call("get_last_volume", {"stock": "000001.SZ"}), 123.0)

    def test_unroutable_falls_back_to_rpc(self):
        router = fs.FormulaServerRouter(
            client=FakeClient(error=fs.FormulaServerUnavailable("down"))
        )
        client = self._client(router)
        calls = []

        class FakeTransport:
            def send_request(self, request, timeout):
                calls.append(request["method"])
                return {"ok": True, "data": "from-rpc"}

        client._transport = lambda: FakeTransport()

        self.assertEqual(client.call("get_last_volume", {"stock": "000001.SZ"}), "from-rpc")
        self.assertEqual(calls, ["get_last_volume"])

    def test_unmapped_method_goes_straight_to_rpc(self):
        router = fs.FormulaServerRouter(client=FakeClient())
        client = self._client(router)
        calls = []

        class FakeTransport:
            def send_request(self, request, timeout):
                calls.append(request["method"])
                return {"ok": True, "data": {"cash": 1.0}}

        client._transport = lambda: FakeTransport()

        self.assertEqual(client.call("get_asset", {}), {"cash": 1.0})
        self.assertEqual(calls, ["get_asset"])

    def test_routed_dataframe_payload_is_restored_like_rpc(self):
        router = fs.FormulaServerRouter(
            client=FakeClient(
                {
                    "getMarketData": {
                        "result": ["000001.SZ", ["20260703", ["close", 10.29]]]
                    }
                }
            )
        )
        client = self._client(router)

        out = client.call(
            "get_market_data_ex", {"field_list": ["close"], "stock_list": ["000001.SZ"]}
        )

        frame = out["000001.SZ"]
        # _restore_jsonable rebuilds a DataFrame when pandas is present, and
        # degrades to the record list otherwise — same as the RPC path.
        if hasattr(frame, "columns"):
            self.assertEqual(list(frame.columns), ["stime", "close"])
            self.assertEqual(frame.iloc[0]["close"], 10.29)
        else:
            self.assertEqual(frame, [{"stime": "20260703", "close": 10.29}])


if __name__ == "__main__":
    unittest.main()
