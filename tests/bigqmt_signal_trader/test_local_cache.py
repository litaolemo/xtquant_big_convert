import os
import shutil
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.local_cache import LocalMarketCache


def _has_pyarrow():
    try:
        import pyarrow  # noqa: F401

        return True
    except Exception:
        return False


class LocalMarketCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_read_merge_dedupe(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("600000.SH", "1d", pd.DataFrame({"stime": ["20260101", "20260102"], "close": [1.0, 2.0]}))
        # overlapping second write: 20260102 should be replaced (keep last), 20260103 appended
        c.write("600000.SH", "1d", pd.DataFrame({"stime": ["20260102", "20260103"], "close": [2.5, 3.0]}))

        df = c.read("600000.SH", "1d")
        self.assertEqual(list(df["stime"]), ["20260101", "20260102", "20260103"])
        self.assertEqual(df[df["stime"] == "20260102"]["close"].iloc[0], 2.5)

    def test_range_and_count_filters(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102", "20260103"], "close": [1, 2, 3]}))

        self.assertEqual(list(c.read("X", "1d", start_time="20260102")["stime"]), ["20260102", "20260103"])
        self.assertEqual(list(c.read("X", "1d", end_time="20260102")["stime"]), ["20260101", "20260102"])
        self.assertEqual(list(c.read("X", "1d", count=1)["stime"]), ["20260103"])
        self.assertIsNone(c.read("MISSING", "1d"))
        self.assertEqual(c.covered("X", "1d"), ("20260101", "20260103", 3))

    def test_daily_range_accepts_full_timestamp_bounds(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102", "20260103"], "close": [1, 2, 3]}))

        out = c.read("X", "1d", start_time="20260102000000", end_time="20260102235959")

        self.assertEqual(list(out["stime"]), ["20260102"])

    def test_index_time_frames_slice_by_date_window(self):
        # issue #54 follow-up: MiniQMT-shaped frames carry time as the index
        # (the client normalizer moves stime to the index and drops the column).
        # The cache must slice by that index — otherwise get_local_data returns
        # every cached day regardless of the requested window.
        import pandas as pd

        c = LocalMarketCache(self.dir)
        df = pd.DataFrame(
            {"open": [1.0, 2.0, 3.0], "close": [1.5, 2.5, 3.5]},
            index=["20260101", "20260102", "20260103"],
        )
        c.write("X", "1d", df)

        out = c.read("X", "1d", start_time="20260102", end_time="20260102")
        self.assertEqual(list(out.index), ["20260102"])
        self.assertEqual(out["close"].iloc[0], 2.5)

    def test_index_time_merge_dedupes_by_index_keep_last(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("X", "1d", pd.DataFrame({"close": [1.0, 2.0]}, index=["20260101", "20260102"]))
        c.write("X", "1d", pd.DataFrame({"close": [2.5, 3.0]}, index=["20260102", "20260103"]))

        out = c.read("X", "1d")
        self.assertEqual(list(out.index), ["20260101", "20260102", "20260103"])
        self.assertEqual(out.loc["20260102", "close"], 2.5)
        self.assertEqual(c.covered("X", "1d"), ("20260101", "20260103", 3))


class LocalCacheReadMatrixTest(unittest.TestCase):
    """读路径参数矩阵：时间轴形态 × 周期形态 × 窗口参数组合。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _cache(self):
        return LocalMarketCache(self.dir)

    def _index_df(self, pairs):
        import pandas as pd

        return pd.DataFrame(
            {"open": [p[1] for p in pairs], "close": [p[2] for p in pairs]},
            index=[p[0] for p in pairs],
        )

    # -- 索引形态：日级窗口 --

    def test_index_time_start_only_end_only(self):
        c = self._cache()
        c.write("X", "1d", self._index_df([("20260101", 1, 10), ("20260102", 2, 20), ("20260103", 3, 30)]))
        self.assertEqual(list(c.read("X", "1d", start_time="20260102").index), ["20260102", "20260103"])
        self.assertEqual(list(c.read("X", "1d", end_time="20260102").index), ["20260101", "20260102"])

    def test_index_time_count_tail_and_window_plus_count(self):
        c = self._cache()
        c.write("X", "1d", self._index_df([("20260101", 1, 10), ("20260102", 2, 20), ("20260103", 3, 30)]))
        self.assertEqual(list(c.read("X", "1d", count=1).index), ["20260103"])
        # 窗口 + count：先切窗口再取尾部
        self.assertEqual(
            list(c.read("X", "1d", start_time="20260101", end_time="20260102", count=1).index),
            ["20260102"],
        )

    def test_index_time_empty_windows(self):
        c = self._cache()
        c.write("X", "1d", self._index_df([("20260101", 1, 10), ("20260102", 2, 20)]))
        # start > end
        self.assertEqual(c.read("X", "1d", start_time="20260102", end_time="20260101").shape[0], 0)
        # 未来窗口
        self.assertEqual(c.read("X", "1d", start_time="20270101").shape[0], 0)
        # 过去窗口
        self.assertEqual(c.read("X", "1d", end_time="20250101").shape[0], 0)
        # 恰好无交集（周末）
        self.assertEqual(c.read("X", "1d", start_time="20260103", end_time="20260104").shape[0], 0)

    def test_index_time_minute_level_14digit_index(self):
        # 分钟线索引（14 位时间戳形态）：按 8 位日期前缀切片
        c = self._cache()
        c.write("X", "1m", self._index_df([
            ("20260102093000", 1, 10), ("20260102093100", 2, 20), ("20260105093000", 3, 30),
        ]))
        out = c.read("X", "1m", start_time="20260102", end_time="20260102")
        self.assertEqual(list(out.index), ["20260102093000", "20260102093100"])
        # 精确到分钟的窗口
        out2 = c.read("X", "1m", start_time="20260102093100")
        self.assertEqual(list(out2.index), ["20260102093100", "20260105093000"])

    def test_index_time_placeholder_rows_dropped(self):
        # 全 0 占位行（QMT 对没下载的日期填 0）不该进缓存——索引形态也一样
        c = self._cache()
        c.write("X", "1d", self._index_df([("20260101", 0.0, 0.0), ("20260102", 2.0, 20.0)]))
        out = c.read("X", "1d")
        self.assertEqual(list(out.index), ["20260102"])

    def test_index_time_rewritten_after_new_dividend(self):
        # 前复权数据除权后历史重缩放：重写同区间必须覆盖旧值（keep last）
        c = self._cache()
        c.write("X", "1d", self._index_df([("20260101", 1, 10.0)]), dividend_type="front")
        c.write("X", "1d", self._index_df([("20260101", 1, 5.0)]), dividend_type="front")
        out = c.read("X", "1d", dividend_type="front")
        self.assertEqual(list(out["close"]), [5.0])

    # -- 列形态：窗口边界补齐 --

    def test_column_time_empty_windows(self):
        import pandas as pd

        c = self._cache()
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102"], "close": [1.0, 2.0]}))
        self.assertEqual(c.read("X", "1d", start_time="20260102", end_time="20260101").shape[0], 0)
        self.assertEqual(c.read("X", "1d", start_time="20270101").shape[0], 0)

    def test_column_time_14digit_stime_slice(self):
        import pandas as pd

        c = self._cache()
        c.write("X", "1m", pd.DataFrame({
            "stime": ["20260102093000", "20260102093100", "20260105093000"], "close": [1.0, 2.0, 3.0],
        }))
        out = c.read("X", "1m", start_time="20260102", end_time="20260102")
        self.assertEqual(list(out["stime"]), ["20260102093000", "20260102093100"])

    # -- 混合形态：老缓存（无时间轴）+ 新写入不崩 --

    def test_mixed_legacy_rangeindex_cache_plus_new_write_does_not_crash(self):
        import pandas as pd

        c = self._cache()
        # 模拟旧版写出的无时间轴缓存（RangeIndex，无 stime 列）
        c.write("X", "1d", pd.DataFrame({"close": [9.0]}))
        # 新版索引形态写入：老行无时间轴不可切片，丢弃老行保住新数据的时间索引
        c.write("X", "1d", self._index_df([("20260101", 1, 10.0)]))
        out = c.read("X", "1d")
        self.assertEqual(out.shape[0], 1)
        self.assertEqual(list(out.index), ["20260101"])
        # 老行被丢后，窗口过滤正常工作
        out2 = c.read("X", "1d", start_time="20260102")
        self.assertEqual(out2.shape[0], 0)

    # -- dividend_type 三种形态彻底隔离 --

    def test_three_dividend_types_fully_isolated(self):
        c = self._cache()
        for dtype, price in (("none", 10.0), ("front", 8.0), ("back", 12.0)):
            c.write("X", "1d", self._index_df([("20260101", 1, price)]), dividend_type=dtype)
        self.assertEqual(list(c.read("X", "1d", dividend_type="none")["close"]), [10.0])
        self.assertEqual(list(c.read("X", "1d", dividend_type="front")["close"]), [8.0])
        self.assertEqual(list(c.read("X", "1d", dividend_type="back")["close"]), [12.0])


    def test_drops_zero_fill_placeholder_rows(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        df = pd.DataFrame(
            {"stime": ["20200101", "20200102", "20260701"], "close": [0.0, 0.0, 8.65], "open": [0.0, 0.0, 8.58]}
        )
        c.write("X", "1d", df)
        self.assertEqual(list(c.read("X", "1d")["stime"]), ["20260701"])  # 0-fill dropped

        # an all-placeholder write must not create/overwrite a cache file
        self.assertEqual(c.write("Y", "1d", pd.DataFrame({"stime": ["20200101"], "close": [0.0]})), 0)
        self.assertIsNone(c.read("Y", "1d"))

    def test_dividend_type_keeps_separate_caches(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101"], "close": [10.0]}), dividend_type="none")
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101"], "close": [9.0]}), dividend_type="front")

        self.assertEqual(c.read("X", "1d", dividend_type="none")["close"].iloc[0], 10.0)
        self.assertEqual(c.read("X", "1d", dividend_type="front")["close"].iloc[0], 9.0)
        self.assertIsNone(c.read("X", "1d", dividend_type="back"))

    def test_pickle_format_roundtrip(self):
        import pandas as pd

        c = LocalMarketCache(self.dir, fmt="pkl")
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102"], "close": [1.0, 2.0]}))
        self.assertTrue(c.path("X", "1d").endswith(".pkl"))
        self.assertEqual(list(c.read("X", "1d")["close"]), [1.0, 2.0])

    @unittest.skipUnless(_has_pyarrow(), "pyarrow not installed")
    def test_parquet_format_roundtrip(self):
        import pandas as pd

        c = LocalMarketCache(self.dir, fmt="parquet")
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102"], "close": [1.0, 2.0]}))
        self.assertTrue(c.path("X", "1d").endswith(".parquet"))
        self.assertEqual(list(c.read("X", "1d")["close"]), [1.0, 2.0])

    @unittest.skipUnless(_has_pyarrow(), "pyarrow not installed")
    def test_migrates_pickle_to_parquet(self):
        import pandas as pd

        LocalMarketCache(self.dir, fmt="pkl").write("X", "1d", pd.DataFrame({"stime": ["20260101"], "close": [1.0]}))
        pq = LocalMarketCache(self.dir, fmt="parquet")
        self.assertEqual(list(pq.read("X", "1d")["close"]), [1.0])  # reads the old pkl
        pq.write("X", "1d", pd.DataFrame({"stime": ["20260102"], "close": [2.0]}))
        self.assertTrue(os.path.isfile(pq.path("X", "1d")))  # parquet now exists
        self.assertFalse(os.path.isfile(pq.path("X", "1d")[:-8] + ".pkl"))  # old pkl removed
        self.assertEqual(list(pq.read("X", "1d")["close"]), [1.0, 2.0])  # merged across formats


class FakeClient:
    def __init__(self, cache_dir, fallback_rpc=False):
        self.account_id = "acct"
        self.calls = []
        self.call_params = []
        self.call_timeouts = []
        self.local_cache_config = {"enabled": True, "dir": cache_dir, "fallback_rpc": fallback_rpc}

    def _redis(self):
        return None

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append(method)
        self.call_params.append((method, params))
        self.call_timeouts.append((method, timeout_seconds))
        if method == "get_market_data_ex":
            import pandas as pd

            codes = (params or {}).get("stock_list") or []
            return {
                c: pd.DataFrame({
                    "stime": ["20260626", "20260629"],
                    "close": [8.76, 8.73],
                    "openInterest": [0.0, 0.0],
                })
                for c in codes
            }
        if method == "download_history_data2":
            # Server-side raw download (raw bars + dividend factors).
            return True
        raise AssertionError("unexpected rpc: %s" % method)


class LocalCacheClientTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _xt(self, fallback_rpc=False):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        return BigQmtXtData(FakeClient(self.dir, fallback_rpc=fallback_rpc))

    def test_download_caches_then_get_local_reads_without_rpc(self):
        xt = self._xt()
        progress = []
        res = xt.download_history_data2(["600000.SH", "000001.SZ"], "1d", callback=lambda d: progress.append(d))

        self.assertEqual(res, {"finished": 2, "total": 2})
        self.assertEqual(len(progress), 2)
        self.assertEqual(progress[-1]["stockcode"], "000001.SZ")
        self.assertEqual(progress[-1]["finished"], 2)
        self.assertEqual(next(timeout for method, timeout in xt.client.call_timeouts if method == "get_market_data_ex"), 60.0)
        calls_after_download = list(xt.client.calls)

        data = xt.get_local_data(stock_list=["600000.SH", "000001.SZ"], period="1d")

        self.assertIn("600000.SH", data)
        self.assertIn("000001.SZ", data)
        self.assertEqual(list(data["600000.SH"]["close"]), [8.76, 8.73])
        # get_local_data must NOT issue any further RPC — pure local read.
        self.assertEqual(xt.client.calls, calls_after_download)

    def test_get_market_data_ex_caches_through(self):
        xt = self._xt()
        # a plain live read must also populate the cache (cache-through)
        xt.get_market_data_ex(field_list=["close"], stock_list=["600000.SH"], period="1d")
        n = len(xt.client.calls)

        data = xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertIn("600000.SH", data)
        self.assertEqual(len(xt.client.calls), n)  # served from cache, no extra RPC

    def test_get_local_miss_returns_empty_and_no_rpc(self):
        xt = self._xt()
        data = xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertEqual(data, {})
        self.assertEqual(xt.client.calls, [])

    def test_get_local_fallback_rpc_fetches_and_caches(self):
        xt = self._xt(fallback_rpc=True)
        data = xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertIn("600000.SH", data)
        self.assertIn("get_market_data_ex", xt.client.calls)  # fetched on miss
        # second read is served from cache — no new RPC
        n = len(xt.client.calls)
        xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertEqual(len(xt.client.calls), n)

    def test_download_then_different_adjustment_read_falls_back(self):
        """A MiniQMT-style raw download can be read as front_ratio data."""
        xt = self._xt(fallback_rpc=True)

        xt.download_history_data("600000.SH", "1d", "20260601", "20260630")
        calls_after_download = len(xt.client.calls)
        data = xt.get_local_data(
            [], ["600000.SH"], "1d", "20260601", "20260630", -1,
            "front_ratio", False,
        )

        self.assertIn("600000.SH", data)
        self.assertGreater(len(xt.client.calls), calls_after_download)
        frame = data["600000.SH"]
        self.assertIn("time", frame.columns)
        self.assertIn("openInterest", frame.columns)
        self.assertGreater(int(frame.iloc[0]["time"]), 0)
        method, params = xt.client.call_params[-1]
        self.assertEqual(method, "get_market_data_ex")
        self.assertEqual(params["dividend_type"], "front_ratio")
        self.assertEqual(
            params["field_list"],
            ["time", "open", "high", "low", "close", "volume", "amount", "openInterest", "preClose"],
        )


class CompatReadMatrixTest(unittest.TestCase):
    """compat 层读路径矩阵：download -> cache -> get_local_data 全链路，
    窗口/字段/count/复权/分批 参数逐一验证（#54 端到端回归）。"""

    _BARS = {
        "600000.SH": [("20260817", 9.04), ("20260818", 8.97), ("20260819", 9.08)],
        "000001.SZ": [("20260817", 12.0), ("20260818", 12.1), ("20260819", 12.2)],
    }

    class _MatrixClient:
        def __init__(self, cache_dir):
            self.account_id = "acct"
            self.calls = []
            self.call_params = []
            self.local_cache_config = {"enabled": True, "dir": cache_dir, "fallback_rpc": False}

        def _redis(self):
            return None

        def call(self, method, params=None, account_id=None, timeout_seconds=None):
            import pandas as pd

            params = dict(params or {})
            self.calls.append(method)
            self.call_params.append((method, params))
            if method == "get_market_data_ex":
                out = {}
                for code in params.get("stock_list") or []:
                    rows = CompatReadMatrixTest._BARS.get(code) or []
                    out[code] = pd.DataFrame(
                        {"stime": [r[0] for r in rows], "close": [r[1] for r in rows]}
                    )
                return out
            if method == "download_history_data2":
                return True
            raise AssertionError("unexpected rpc: %s" % method)

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _xt(self):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        return BigQmtXtData(self._MatrixClient(self.dir))

    def test_download_window_then_local_read_single_day(self):
        # issue #54 端到端：下载 0817-0819，读 0818 必须只返回 0818
        xt = self._xt()
        xt.download_history_data2(["600000.SH"], "1d", start_time="20260817", end_time="20260819")
        out = xt.get_local_data(["close"], ["600000.SH"], "1d",
                                start_time="20260818", end_time="20260818", count=-1)
        df = out["600000.SH"]
        self.assertEqual(df.shape[0], 1)
        self.assertEqual(list(df["close"]), [8.97])
        self.assertEqual(str(df.index[0]), "20260818")

    def test_local_read_windows_and_count(self):
        xt = self._xt()
        xt.download_history_data2(["600000.SH"], "1d", start_time="20260817", end_time="20260819")
        code = "600000.SH"
        self.assertEqual([str(i) for i in xt.get_local_data(
            ["close"], [code], "1d", start_time="20260818")[code].index], ["20260818", "20260819"])
        self.assertEqual([str(i) for i in xt.get_local_data(
            ["close"], [code], "1d", end_time="20260818")[code].index], ["20260817", "20260818"])
        self.assertEqual([str(i) for i in xt.get_local_data(
            ["close"], [code], "1d", count=2)[code].index], ["20260818", "20260819"])

    def test_local_read_field_selection(self):
        xt = self._xt()
        xt.download_history_data2(["600000.SH"], "1d")
        out = xt.get_local_data(["close"], ["600000.SH"], "1d")
        self.assertIn("close", out["600000.SH"].columns)
        out_all = xt.get_local_data([], ["600000.SH"], "1d")
        self.assertIn("close", out_all["600000.SH"].columns)

    def test_get_market_data_ex_passes_params_to_rpc(self):
        xt = self._xt()
        xt.get_market_data_ex(field_list=["close"], stock_list=["600000.SH"], period="1d",
                              start_time="20260817", end_time="20260819", count=-1,
                              dividend_type="front")
        method, params = xt.client.call_params[-1]
        self.assertEqual(method, "get_market_data_ex")
        self.assertEqual(params["start_time"], "20260817")
        self.assertEqual(params["end_time"], "20260819")
        self.assertEqual(params["dividend_type"], "front")
        self.assertEqual(params["count"], -1)

    def test_download_front_does_not_pollute_none_cache(self):
        xt = self._xt()
        xt.download_history_data2(["600000.SH"], "1d", dividend_type="front")
        front = xt.get_local_data(["close"], ["600000.SH"], "1d", dividend_type="front")
        self.assertEqual(front["600000.SH"].shape[0], 3)
        # none 缓存没有被 front 数据污染
        none = xt.get_local_data(["close"], ["600000.SH"], "1d", dividend_type="none")
        self.assertEqual(none, {})

    def test_get_market_data_ex_chunks_wide_stock_lists(self):
        from bigqmt_signal_trader.xtquant_compat import DEFAULT_MARKET_DATA_CHUNK

        xt = self._xt()
        codes = ["600000.SH", "000001.SZ"] * DEFAULT_MARKET_DATA_CHUNK  # 200 只 > 默认 100/批
        xt.get_market_data_ex(field_list=["close"], stock_list=codes, period="1d")
        n_calls = xt.client.calls.count("get_market_data_ex")
        self.assertEqual(n_calls, (len(codes) + DEFAULT_MARKET_DATA_CHUNK - 1) // DEFAULT_MARKET_DATA_CHUNK)

    def test_local_read_returns_miniqmt_time_index_shape(self):
        # MiniQMT 形态：时间做索引、没有 stime 列
        xt = self._xt()
        xt.download_history_data2(["600000.SH"], "1d")
        df = xt.get_local_data(["close"], ["600000.SH"], "1d")["600000.SH"]
        self.assertNotIn("stime", list(df.columns))
        self.assertEqual([str(i) for i in df.index], ["20260817", "20260818", "20260819"])

    def test_download_passes_window_to_server_rpc(self):
        xt = self._xt()
        xt.download_history_data2(["600000.SH"], "1d", start_time="20260815", end_time="20260819")
        dl = [p for m, p in xt.client.call_params if m == "download_history_data2"]
        self.assertTrue(dl)
        self.assertEqual(dl[0]["start_time"], "20260815")
        self.assertEqual(dl[0]["end_time"], "20260819")


class AdjustedDownloadTest(unittest.TestCase):
    """Adjusted (front/back) downloads must trigger the server-side raw
    download FIRST: Big QMT computes adjusted bars from raw bars + dividend
    factors, and without the server-side download the adjusted result is
    all zeros (verified live with 600654.SH)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _xt(self):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        return BigQmtXtData(FakeClient(self.dir))

    def test_front_download_triggers_server_side_raw_download_first(self):
        xt = self._xt()
        xt.download_history_data2(["600000.SH"], "1d", start_time="20200101", dividend_type="front")

        # The server-side raw download must run BEFORE the adjusted pull.
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertIn("download_history_data2", method_calls)
        self.assertIn("get_market_data_ex", method_calls)
        self.assertLess(
            method_calls.index("download_history_data2"),
            method_calls.index("get_market_data_ex"),
            "server-side raw download must precede the adjusted pull",
        )
        # The raw download carries the same codes/period/window.
        raw_call = next(p for m, p in xt.client.call_params if m == "download_history_data2")
        self.assertEqual(raw_call["stock_list"], ["600000.SH"])
        self.assertEqual(raw_call["period"], "1d")
        self.assertEqual(raw_call["start_time"], "20200101")

    def test_none_download_still_triggers_server_side_download(self):
        """issue #47: an unadjusted download used to skip the server RPC and
        only read what Big QMT already had -- a no-op that still reported
        {finished: N}. xtdata semantics are "populate the local QMT store", and
        callers (FormulaServer, get_local_data) depend on that actually happening."""
        xt = self._xt()
        xt.download_history_data2(["600000.SH"], "1d", dividend_type="none")

        method_calls = [m for m, _ in xt.client.call_params]
        self.assertIn("download_history_data2", method_calls)
        self.assertIn("get_market_data_ex", method_calls)
        self.assertLess(
            method_calls.index("download_history_data2"),
            method_calls.index("get_market_data_ex"),
            "the download must precede the pull, or the pull reads stale data",
        )
        raw_call = next(p for m, p in xt.client.call_params if m == "download_history_data2")
        self.assertEqual(raw_call["stock_list"], ["600000.SH"])
        self.assertEqual(raw_call["period"], "1d")

    def test_none_download_survives_server_download_failure(self):
        """Same best-effort contract the adjusted path already had: a deployment
        without the QMT global must still get its bars."""
        xt = self._xt()
        original_call = xt.client.call

        def failing_download(method, params=None, account_id=None, timeout_seconds=None):
            if method == "download_history_data2":
                raise RuntimeError("global not available")
            return original_call(method, params, account_id=account_id, timeout_seconds=timeout_seconds)

        xt.client.call = failing_download
        result = xt.download_history_data2(["600000.SH"], "1d", dividend_type="none")

        self.assertEqual(result["finished"], 1)
        self.assertIn("get_market_data_ex", [m for m, _ in xt.client.call_params])

    def test_front_download_survives_server_download_failure(self):
        # Deployments without the QMT global must still get the adjusted pull
        # (best-effort raw download, never fatal).
        xt = self._xt()
        original_call = xt.client.call

        def failing_download(method, params=None, account_id=None, timeout_seconds=None):
            if method == "download_history_data2":
                raise RuntimeError("global not available")
            return original_call(method, params, account_id=account_id, timeout_seconds=timeout_seconds)

        xt.client.call = failing_download
        result = xt.download_history_data2(["600000.SH"], "1d", dividend_type="front")
        self.assertEqual(result, {"finished": 1, "total": 1})  # adjusted pull still ran

    def test_download_with_cache_disabled_skips_client_pull(self):
        """local cache controls step 2 uniformly for every period (tick and
        bars alike): disabled = server-side download only (step 1; data lands
        in the server-side DATs, so this is real work, not the old fake
        progress of issue #47), no client pull, no fail-fast."""
        for period in ("tick", "1d"):
            xt = self._xt()
            xt.client.local_cache_config["enabled"] = False
            result = xt.download_history_data2(
                ["600000.SH", "000001.SZ"], period, "20260828", "20260828"
            )

            self.assertEqual(result, {"finished": 2, "total": 2})
            method_calls = [m for m, _ in xt.client.call_params]
            self.assertIn("download_history_data2", method_calls)  # server-side download ran
            self.assertNotIn("get_market_data_ex", method_calls)  # client pull skipped

    def test_download_with_cache_disabled_reports_progress_via_callback(self):
        xt = self._xt()
        xt.client.local_cache_config["enabled"] = False
        seen = []
        result = xt.download_history_data2(
            ["600000.SH"], "tick", "20260828", "20260828", callback=seen.append
        )

        self.assertEqual(result["finished"], 1)
        self.assertEqual(seen, [{"finished": 1, "total": 1, "stockcode": "600000.SH"}])

    def test_download_with_cache_disabled_raises_on_server_download_failure(self):
        """issue #47 follow-up: with the cache disabled the server-side download
        is the whole job -- a failed one must not come back as {finished: total}
        with per-code callbacks. That is the fake progress #47 fixed."""
        xt = self._xt()
        xt.client.local_cache_config["enabled"] = False
        seen = []
        original_call = xt.client.call

        def failing_download(method, params=None, account_id=None, timeout_seconds=None):
            if method == "download_history_data2":
                raise RuntimeError("global not available")
            return original_call(method, params, account_id=account_id, timeout_seconds=timeout_seconds)

        xt.client.call = failing_download
        with self.assertRaisesRegex(RuntimeError, "global not available"):
            xt.download_history_data2(
                ["600000.SH"], "tick", "20260828", "20260828", callback=seen.append
            )

        # No fake per-code progress was reported.
        self.assertEqual(seen, [])

    def test_tick_download_with_cache_enabled_pulls_like_bars(self):
        """tick and bars share one contract: with the cache enabled the
        download also populates the client-side cache (step 2 runs)."""
        xt = self._xt()
        xt.download_history_data2(
            ["600000.SH"], "tick", "20260828", "20260828"
        )

        method_calls = [m for m, _ in xt.client.call_params]
        self.assertIn("download_history_data2", method_calls)
        self.assertIn("get_market_data_ex", method_calls)
        self.assertLess(
            method_calls.index("download_history_data2"),
            method_calls.index("get_market_data_ex"),
            "server-side download must precede the pull",
        )


class _AllZeroThenRealClient(FakeClient):
    """First adjusted get_market_data_ex returns all-zero bars (server lacks
    raw data); after a server-side raw download, subsequent pulls are real."""

    def __init__(self, cache_dir):
        super(_AllZeroThenRealClient, self).__init__(cache_dir)
        self._downloaded = False

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append(method)
        self.call_params.append((method, params))
        self.call_timeouts.append((method, timeout_seconds))
        import pandas as pd

        if method == "download_history_data2":
            self._downloaded = True
            return True
        if method == "get_market_data_ex":
            codes = (params or {}).get("stock_list") or []
            if self._downloaded:
                return {c: pd.DataFrame({"stime": ["20260626", "20260629"], "close": [8.76, 8.73]}) for c in codes}
            # all-zero symptom: head zeros, last bar live
            return {c: pd.DataFrame({"stime": ["20260626", "20260629"], "close": [0.0, 8.73]}) for c in codes}
        raise AssertionError("unexpected rpc: %s" % method)


class _MissingThenCompleteClient(FakeClient):
    """First none-adjusted get_market_data_ex returns only the first requested
    code (server has no raw bars for the rest); after a server-side raw
    download, subsequent pulls return every code."""

    def __init__(self, cache_dir):
        super(_MissingThenCompleteClient, self).__init__(cache_dir)
        self._downloaded = False

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append(method)
        self.call_params.append((method, params))
        import pandas as pd

        if method == "download_history_data2":
            self._downloaded = True
            return True
        if method == "get_market_data_ex":
            codes = (params or {}).get("stock_list") or []
            served = codes if self._downloaded else codes[:1]
            return {c: pd.DataFrame({"stime": ["20260626", "20260629"], "close": [8.76, 8.73]}) for c in served}
        raise AssertionError("unexpected rpc: %s" % method)


class _NeverServesAllClient(FakeClient):
    """none-adjusted get_market_data_ex that permanently omits some codes
    (delisted / suspended / no quote permission): the server can never serve
    them, no matter how often the raw store is downloaded."""

    def __init__(self, cache_dir, unserved):
        super(_NeverServesAllClient, self).__init__(cache_dir)
        self._unserved = set(unserved)

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append(method)
        self.call_params.append((method, params))
        self.call_timeouts.append((method, timeout_seconds))
        import pandas as pd

        if method == "download_history_data2":
            return True
        if method == "get_market_data_ex":
            codes = (params or {}).get("stock_list") or []
            served = [c for c in codes if c not in self._unserved]
            return {c: pd.DataFrame({"stime": ["20260626", "20260629"], "close": [8.76, 8.73]}) for c in served}
        raise AssertionError("unexpected rpc: %s" % method)


class _FieldKeyedClient(FakeClient):
    """get_market_data answers field-keyed ({field: {code: [..]}}), the QMT
    ContextInfo shape -- served codes live one level below the top keys."""

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append(method)
        self.call_params.append((method, params))
        self.call_timeouts.append((method, timeout_seconds))

        if method == "download_history_data2":
            return True
        if method == "get_market_data":
            codes = (params or {}).get("stock_list") or []
            return {"close": {c: [8.76, 8.73] for c in codes}}
        raise AssertionError("unexpected rpc: %s" % method)


class AdjustedReadSelfHealTest(unittest.TestCase):
    """Reading adjusted bars that come back all-zero must self-heal:
    trigger a server-side raw download, wait, and retry once."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _xt(self):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        return BigQmtXtData(_AllZeroThenRealClient(self.dir))

    def _xt_missing(self):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        return BigQmtXtData(_MissingThenCompleteClient(self.dir))

    def test_front_read_self_heals_all_zero_to_real(self):
        xt = self._xt()
        data = xt.get_market_data_ex(
            field_list=["close"], stock_list=["600000.SH"], period="1d",
            dividend_type="front", timeout_seconds=60.0,
        )
        # After self-heal the retry returns real (non-zero) bars.
        self.assertEqual(list(data["600000.SH"]["close"]), [8.76, 8.73])
        # The heal path must have triggered a server-side raw download.
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertIn("download_history_data2", method_calls)
        # get_market_data_ex called twice: initial all-zero pull + retry.
        self.assertEqual(method_calls.count("get_market_data_ex"), 2)
        self.assertEqual(
            [timeout for method, timeout in xt.client.call_timeouts if method == "get_market_data_ex"],
            [60.0, 60.0],
        )

    def test_none_read_self_heals_missing_codes(self):
        # Big QMT's raw store is not auto-populated market-wide; a
        # none-adjusted pull that comes back missing requested codes means
        # the server has no raw bars for them (2026-08-30 --tick pipeline:
        # 5225 codes requested, only 8 came back) -- must heal like
        # adjusted all-zero does.
        xt = self._xt_missing()
        data = xt.get_market_data_ex(
            field_list=["close"], stock_list=["600000.SH", "000001.SZ"], period="1d",
            dividend_type="none",
        )
        # After self-heal the retry returns both requested codes.
        self.assertEqual(sorted(data.keys()), ["000001.SZ", "600000.SH"])
        # The heal path must have triggered a server-side raw download.
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertIn("download_history_data2", method_calls)
        # get_market_data_ex called twice: initial partial pull + retry.
        self.assertEqual(method_calls.count("get_market_data_ex"), 2)

    def test_none_read_majority_missing_self_heals(self):
        # The steady 2026-08-30 shape (5225 requested, 8 served ~ 0.15%):
        # majority missing is the raw-store-not-populated signal and must
        # still heal once the criterion stops being "any code missing".
        xt = self._xt_missing()
        codes = ["60000%d.SH" % i for i in range(10)]
        data = xt.get_market_data_ex(
            field_list=["close"], stock_list=codes, period="1d",
            dividend_type="none",
        )
        self.assertEqual(sorted(data.keys()), sorted(codes))
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertIn("download_history_data2", method_calls)
        self.assertEqual(method_calls.count("get_market_data_ex"), 2)

    def test_none_read_minority_missing_does_not_self_heal(self):
        # A full-market read always has a few codes the server can never
        # serve (delisted / suspended / no quote permission). Healing on
        # any missing code made each of those a per-call cost -- raw
        # download + sleep + full re-read, every time. A minority missing
        # must return the partial result as-is, single pull.
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        xt = BigQmtXtData(_NeverServesAllClient(self.dir, unserved=["000001.SZ"]))
        data = xt.get_market_data_ex(
            field_list=["close"],
            stock_list=["600000.SH", "000001.SZ", "600519.SH", "000002.SZ"],
            period="1d", dividend_type="none",
        )
        self.assertEqual(sorted(data.keys()), ["000002.SZ", "600000.SH", "600519.SH"])
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertNotIn("download_history_data2", method_calls)
        self.assertEqual(method_calls.count("get_market_data_ex"), 1)

    def test_field_keyed_get_market_data_none_does_not_self_heal(self):
        # get_market_data answers field-keyed ({field: {code: [..]}});
        # reading served codes off the top level would make every code look
        # missing and pay the heal (download + sleep + re-read) on every
        # none-adjusted call even when the result is complete.
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        xt = BigQmtXtData(_FieldKeyedClient(self.dir))
        data = xt.get_market_data(
            field_list=["close"], stock_list=["600000.SH", "000001.SZ"], period="1d",
            dividend_type="none",
        )
        self.assertEqual(sorted(data["close"].keys()), ["000001.SZ", "600000.SH"])
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertNotIn("download_history_data2", method_calls)
        self.assertEqual(method_calls.count("get_market_data"), 1)

    def test_none_read_does_not_self_heal(self):
        xt = self._xt()
        data = xt.get_market_data_ex(
            field_list=["close"], stock_list=["600000.SH"], period="1d",
            dividend_type="none",
        )
        # none read that comes back complete (every requested code served)
        # does not heal: returns whatever the server sent, single pull.
        self.assertEqual(list(data["600000.SH"]["close"]), [0.0, 8.73])
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertNotIn("download_history_data2", method_calls)
        self.assertEqual(method_calls.count("get_market_data_ex"), 1)


class _AsyncLandingClient(FakeClient):
    """模拟 QMT 下载全局的异步落地：前 N 次 get_market_data_ex 返回空，
    之后才返回真实数据（issue #66 的下载后立读竞态）。"""

    def __init__(self, cache_dir, empty_reads=2):
        super().__init__(cache_dir)
        self._empty_left = empty_reads
        self.pull_count = 0

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        if method == "get_market_data_ex":
            self.pull_count += 1
            if self._empty_left > 0:
                self._empty_left -= 1
                self.calls.append(method)
                self.call_params.append((method, params))
                return {}
        return super().call(method, params, account_id=account_id, timeout_seconds=timeout_seconds)


class DownloadWaitForDataTest(unittest.TestCase):
    """Issue #66：QMT 下载全局提交即返回、数据异步落地。download_history_data2
    必须轮询等到真实数据出现（或超时），而不是拉一次空结果就缓存。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _xt(self, client):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        return BigQmtXtData(client)

    def test_download_waits_until_real_data_lands(self):
        client = _AsyncLandingClient(self.dir, empty_reads=2)
        xt = self._xt(client)
        xt.download_history_data2(["600000.SH"], "1d", start_time="20260817", end_time="20260819")

        # 空返回之后还在重试，最终拿到真实数据
        self.assertEqual(client.pull_count, 3)
        data = xt.get_local_data(["close"], ["600000.SH"], "1d")
        self.assertEqual(list(data["600000.SH"]["close"]), [8.76, 8.73])

    def test_download_gives_up_after_wait_timeout(self):
        # 一直没数据（停牌/退市）：轮询到超时为止，不无限等，结果照常回报
        client = _AsyncLandingClient(self.dir, empty_reads=99)
        xt = self._xt(client)
        import time as _t
        t0 = _t.time()
        res = xt.download_history_data2(["600000.SH"], "1d", data_wait_seconds=2.0)
        elapsed = _t.time() - t0

        self.assertEqual(res, {"finished": 1, "total": 1})
        self.assertGreater(elapsed, 1.5)
        self.assertLess(elapsed, 10.0)
        self.assertGreater(client.pull_count, 1)
        # 没数据不缓存
        self.assertEqual(xt.get_local_data(["close"], ["600000.SH"], "1d"), {})


if __name__ == "__main__":
    unittest.main()
