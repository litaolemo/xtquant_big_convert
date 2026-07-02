"""Big QMT market data adapter.

This module wraps two QMT runtime objects:

* ``ContextInfo`` — the strategy-scoped object exposed inside ``handlebar`` /
  ``init``. It carries methods that operate on the *current* subscribed context
  (``get_market_data_ex``, ``get_full_tick``, ``get_instrumentdetail`` ...).
* the **native xtdata SDK** (``bin.x64/Lib/site-packages/xtquant/xtdata.py``) —
  a module of global functions that talk to the local quote service directly.
  Some APIs only exist here, never as ContextInfo methods.

The split matters. Per the official docs and the ContextInfo IDE stub
(``_PyContextInfo.py``):

* ``get_sector_list`` / ``get_holidays`` are **xtdata module functions**
  (SDK xtdata.py lines 784 / 1197). They are *not* ContextInfo methods, so
  calling ``ContextInfo.get_sector_list()`` raises NotImplementedError.
* ``get_markets`` / ``get_market_last_trade_date`` do not exist in either the
  ContextInfo stub or the xtdata SDK — they are MiniQMT-only conveniences that
  must be synthesized from ``get_trading_dates``.
* ``get_trading_dates`` exists on BOTH objects but with **different first
  arguments**: the ContextInfo method takes ``stockcode`` while the xtdata
  module function takes ``market``. We pass ``market`` (that is what every
  caller in this codebase supplies), so we route through xtdata.

This module does not make trading decisions.
"""

import importlib
import importlib.util

from ..code_utils import normalize_stock_code


MARKET_CODES = {"SH", "SZ", "BJ", "HK"}


def normalize_market_or_stock_code(code):
    text = str(code or "").strip().upper()
    if text in MARKET_CODES:
        return text
    return normalize_stock_code(text)


_NATIVE_XTDATA = None  # cached native xtdata SDK module (None = not yet tried)
_NATIVE_XTDATA_UNAVAILABLE = object()  # sentinel: looked, not importable


def _load_native_xtdata():
    """Return the *native* xtdata SDK module shipped with the QMT install.

    The Big QMT process ships two ``xtquant.xtdata`` modules:

    * ``python/xtquant/xtdata.py`` — our RPC shim (forwards back over Redis).
    * ``bin.x64/Lib/site-packages/xtquant/xtdata.py`` — the real SDK that
      connects to the local quote service via ``get_client()``.

    In the server-side adapter we need the real SDK because the global-data
    functions (sectors, holidays, trading dates) only exist there. We load it
    by absolute path so our shim (which may shadow it on ``sys.path``) never
    wins. Returns ``None`` when the SDK is unavailable (e.g. running outside
    QMT, or in a unit test) so callers can degrade gracefully.
    """
    global _NATIVE_XTDATA
    if _NATIVE_XTDATA is _NATIVE_XTDATA_UNAVAILABLE:
        return None
    if _NATIVE_XTDATA is not None:
        return _NATIVE_XTDATA
    try:
        import os

        rel = os.path.join("bin.x64", "Lib", "site-packages", "xtquant", "xtdata.py")
        # Walk up from this file looking for the QMT install root (the dir that
        # contains bin.x64/). Robust to wherever the package happens to live
        # (python/bigqmt_signal_trader/adapters/ in QMT, src/... in the repo).
        start = os.path.abspath(__file__)
        loaded = None
        for _ in range(8):
            parent = os.path.dirname(start)
            if parent == start:
                break
            candidate = os.path.join(parent, rel)
            if os.path.isfile(candidate):
                spec = importlib.util.spec_from_file_location(
                    "bigqmt_native_xtdata", candidate
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                loaded = module
                break
            start = parent
        if loaded is None:
            # Fall back to whatever ``xtquant.xtdata`` resolves to, as long as
            # it is the real SDK. Our RPC shim (python/xtquant/xtdata.py and the
            # repo's src/xtquant/xtdata.py) also exposes get_sector_list but only
            # forwards back over RPC — using it here would recurse. Reject any
            # module whose file lives under a "python" or "src" dir.
            try:
                candidate = importlib.import_module("xtquant.xtdata")
            except Exception:
                candidate = None
            cand_file = getattr(candidate, "__file__", "") or ""
            is_shim = (
                candidate is not None
                and hasattr(candidate, "get_sector_list")
                and (
                    candidate.__name__ == "bigqmt_native_xtdata"
                    or candidate.__name__.endswith("xtquant_compat")
                    or os.sep + "python" + os.sep in cand_file
                    or os.sep + "src" + os.sep in cand_file
                )
            )
            if candidate is not None and hasattr(candidate, "get_sector_list") and not is_shim:
                loaded = candidate
        _NATIVE_XTDATA = loaded if loaded is not None else _NATIVE_XTDATA_UNAVAILABLE
    except Exception:
        _NATIVE_XTDATA = _NATIVE_XTDATA_UNAVAILABLE
    return None if _NATIVE_XTDATA is _NATIVE_XTDATA_UNAVAILABLE else _NATIVE_XTDATA


class BigQmtMarketDataProvider:
    def __init__(self, context_info, native_xtdata=None):
        self.context_info = context_info
        # Allow injection for tests; otherwise resolve lazily on first use.
        self._native_xtdata = native_xtdata

    def _context_method(self, method_name):
        method = getattr(self.context_info, method_name, None)
        if method is None:
            raise NotImplementedError("ContextInfo.%s is not available" % method_name)
        return method

    def _call_context(self, method_name, *args, **kwargs):
        return self._context_method(method_name)(*args, **kwargs)

    def _native(self):
        """Return the native xtdata SDK, resolving it lazily on first use."""
        if self._native_xtdata is None:
            self._native_xtdata = _load_native_xtdata()
        return self._native_xtdata

    def _native_or_context(self, func_name, context_caller, *args, **kwargs):
        """Prefer the xtdata SDK function, fall back to a ContextInfo call.

        Several data APIs exist only as xtdata module functions. When the SDK
        is available we use it (the authoritative source). Otherwise we fall
        back to ContextInfo so that callers in backtest-only contexts still
        get a best-effort answer instead of a hard NotImplementedError.
        """
        module = self._native()
        if module is not None:
            fn = getattr(module, func_name, None)
            if fn is not None:
                return fn(*args, **kwargs)
        return context_caller()

    def _call_first_supported(self, shapes):
        last_error = None
        for method_name, args, kwargs in shapes:
            method = getattr(self.context_info, method_name, None)
            if method is None:
                continue
            try:
                return method(*args, **kwargs)
            except TypeError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise NotImplementedError("none of the ContextInfo methods is available")

    def _market_data_shapes(self, method_name, **params):
        field_list = list(params.get("field_list") or params.get("fields") or [])
        stock_list = list(params.get("stock_list") or params.get("stock_code") or [])
        period = params.get("period", "1d")
        start_time = params.get("start_time", "")
        end_time = params.get("end_time", "")
        count = params.get("count", -1)
        dividend_type = params.get("dividend_type", "none")
        fill_data = params.get("fill_data", True)
        data_dir = params.get("data_dir")

        mini_kwargs = {
            "field_list": field_list,
            "stock_list": stock_list,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type,
            "fill_data": fill_data,
        }
        big_kwargs = {
            "fields": field_list,
            "stock_code": stock_list,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type,
        }
        if method_name == "get_local_data" and data_dir is not None:
            mini_kwargs["data_dir"] = data_dir
            big_kwargs["data_dir"] = data_dir
        positional_tail_kwargs = {
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type,
        }
        if method_name == "get_local_data" and data_dir is not None:
            positional_tail_kwargs["data_dir"] = data_dir

        return [
            (method_name, (), big_kwargs),
            (method_name, (), mini_kwargs),
            (
                method_name,
                (field_list, stock_list, period, start_time, end_time, count, dividend_type, fill_data),
                {},
            ),
            (method_name, (field_list, stock_list), positional_tail_kwargs),
            (
                method_name,
                (field_list,),
                {
                    "stock_code": stock_list,
                    "period": period,
                    "start_time": start_time,
                    "end_time": end_time,
                    "count": count,
                    "dividend_type": dividend_type,
                },
            ),
            (
                method_name,
                (field_list,),
                {
                    "stock_list": stock_list,
                    "period": period,
                    "start_time": start_time,
                    "end_time": end_time,
                    "count": count,
                    "dividend_type": dividend_type,
                    "fill_data": fill_data,
                },
            ),
        ]

    def get_ticks(self, codes):
        normalized_codes = [normalize_market_or_stock_code(code) for code in codes]
        data = self.context_info.get_full_tick(normalized_codes)
        return data or {}

    def get_instrument(self, code):
        normalized = normalize_stock_code(code)
        data = self.context_info.get_instrumentdetail(normalized)
        return data or {}

    def get_instrument_type(self, code, variety_list=None):
        if hasattr(self.context_info, "get_instrument_type"):
            return self.context_info.get_instrument_type(code, variety_list)
        normalized = normalize_stock_code(code)
        pure = normalized.split(".")[0]
        result = {
            "stock": pure.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")),
            "fund": pure.startswith(("15", "16", "50", "51", "56", "58")),
            "etf": pure.startswith(("15", "51", "56", "58")),
            "bond": pure.startswith(("11", "12")),
            "index": pure.startswith(("000", "399")) and not normalized.startswith(("000001.SZ", "000002.SZ")),
        }
        if variety_list:
            return {str(name): bool(result.get(str(name), False)) for name in variety_list}
        return result

    def get_stock_list_in_sector(self, sector_name, real_timetag=-1):
        shapes = [
            ("get_stock_list_in_sector", (sector_name, real_timetag), {}),
            ("get_stock_list_in_sector", (sector_name,), {}),
        ]
        data = self._call_first_supported(shapes)
        return data or []

    def get_market_data(
        self,
        field_list=None,
        stock_list=None,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
        fill_data=True,
    ):
        return self._call_first_supported(
            self._market_data_shapes(
                "get_market_data",
                field_list=field_list,
                stock_list=stock_list,
                period=period,
                start_time=start_time,
                end_time=end_time,
                count=count,
                dividend_type=dividend_type,
                fill_data=fill_data,
            )
        )

    def get_market_data_ex(self, **kwargs):
        shapes = self._market_data_shapes("get_market_data_ex", **kwargs)
        if hasattr(self.context_info, "get_market_data"):
            shapes.extend(self._market_data_shapes("get_market_data", **kwargs))
        return self._call_first_supported(shapes)

    def get_local_data(self, **kwargs):
        shapes = self._market_data_shapes("get_local_data", **kwargs)
        if hasattr(self.context_info, "get_market_data"):
            shapes.extend(self._market_data_shapes("get_market_data", **kwargs))
        return self._call_first_supported(shapes)

    def get_divid_factors(self, stock_code, start_time="", end_time=""):
        return self._call_context("get_divid_factors", stock_code, start_time, end_time)

    def download_history_data(self, stock_code, period, start_time="", end_time="", incrementally=None):
        kwargs = {"stock_code": stock_code, "period": period, "start_time": start_time, "end_time": end_time}
        if incrementally is not None:
            kwargs["incrementally"] = incrementally
        return self._call_context("download_history_data", **kwargs)

    def download_history_data2(self, stock_list, period, start_time="", end_time="", incrementally=None):
        kwargs = {"stock_list": stock_list, "period": period, "start_time": start_time, "end_time": end_time}
        if incrementally is not None:
            kwargs["incrementally"] = incrementally
        return self._call_context("download_history_data2", **kwargs)

    def get_trading_dates(self, market, start_time="", end_time="", count=-1):
        # xtdata SDK signature: get_trading_dates(market, start_time, end_time, count)
        # ContextInfo stub signature: get_trading_dates(stockcode, start_date, end_date, count, period)
        # — note the FIRST argument differs (market vs stockcode). Every caller in
        # this codebase passes a market code, so the xtdata SDK is the correct path.
        def _via_context():
            # ContextInfo's first arg is stockcode; pass market through anyway so
            # backtest contexts still return something rather than crashing.
            return self._call_context("get_trading_dates", market, start_time, end_time, count)

        return self._native_or_context(
            "get_trading_dates", _via_context, market, start_time, end_time, count
        )

    def get_holidays(self):
        # Holiday list is a GLOBAL datum, not context-scoped — only the xtdata
        # SDK exposes it (xtdata.py line 1197). No ContextInfo method exists.
        def _via_context():
            return self._call_context("get_holidays")

        return self._native_or_context("get_holidays", _via_context)

    def download_holiday_data(self, incrementally=True):
        def _via_context():
            return self._call_context("download_holiday_data", incrementally=incrementally)

        module = self._native()
        if module is not None and hasattr(module, "download_holiday_data"):
            try:
                return module.download_holiday_data(incrementally)
            except TypeError:
                # older SDKs may not accept the keyword
                return module.download_holiday_data()
        return _via_context()

    def get_ipo_info(self, start_time="", end_time=""):
        return self._call_context("get_ipo_info", start_time, end_time)

    def get_etf_info(self):
        return self._call_context("get_etf_info")

    def download_etf_info(self):
        return self._call_context("download_etf_info")

    def get_option_list(self, undl_code, dedate, opttype="", isavailavle=False):
        return self._call_context("get_option_list", undl_code, dedate, opttype, isavailavle)

    def get_his_option_list(self, undl_code, dedate):
        return self._call_context("get_his_option_list", undl_code, dedate)

    def get_his_option_list_batch(self, undl_code, start_time="", end_time=""):
        return self._call_context("get_his_option_list_batch", undl_code, start_time, end_time)

    def get_financial_data(self, stock_list, table_list=None, start_time="", end_time="", report_type="report_time"):
        return self._call_context(
            "get_financial_data",
            stock_list,
            table_list or [],
            start_time,
            end_time,
            report_type,
        )

    def download_financial_data(self, stock_list, table_list=None, start_time="", end_time="", incrementally=None):
        kwargs = {
            "stock_list": stock_list,
            "table_list": table_list or [],
            "start_time": start_time,
            "end_time": end_time,
        }
        if incrementally is not None:
            kwargs["incrementally"] = incrementally
        return self._call_context("download_financial_data", **kwargs)

    def download_financial_data2(self, stock_list, table_list=None, start_time="", end_time=""):
        return self._call_context("download_financial_data2", stock_list, table_list or [], start_time, end_time)

    def get_sector_list(self):
        # Sector list is a GLOBAL datum — only the xtdata SDK exposes it
        # (xtdata.py line 784). No ContextInfo method exists.
        def _via_context():
            return self._call_context("get_sector_list")

        return self._native_or_context("get_sector_list", _via_context)

    def get_sector_info(self, sector_name=""):
        return self._call_context("get_sector_info", sector_name)

    def get_markets(self):
        # No such function exists in either ContextInfo or the xtdata SDK.
        # MiniQMT-only convenience; synthesize from the known A-share markets.
        return list(MARKET_CODES)

    def get_market_last_trade_date(self, market):
        # No such function exists in either ContextInfo or the xtdata SDK.
        # Derive it from get_trading_dates(market, count=1) — last entry.
        try:
            dates = self.get_trading_dates(market, "", "", 1) or []
        except Exception:
            dates = []
        if not dates:
            return None
        # xtdata returns millisecond timestamps (long list); take the last one.
        try:
            return dates[-1]
        except Exception:
            return None

    def call_formula(self, formula_name, stock_code, period, start_time="", end_time="", count=-1, dividend_type=None, extend_param=None):
        return self._call_context(
            "call_formula",
            formula_name,
            stock_code,
            period,
            start_time,
            end_time,
            count,
            dividend_type,
            extend_param or {},
        )

    def subscribe_formula(self, formula_name, stock_code, period, start_time="", end_time="", count=-1, dividend_type=None, extend_param=None):
        return self._call_context(
            "subscribe_formula",
            formula_name,
            stock_code,
            period,
            start_time,
            end_time,
            count,
            dividend_type,
            extend_param or {},
        )

    def unsubscribe_formula(self, request_id):
        return self._call_context("unsubscribe_formula", request_id)

    def get_formula_result(self, request_id, start_time="", end_time="", count=-1, timeout_second=-1):
        return self._call_context("get_formula_result", request_id, start_time, end_time, count, timeout_second)

    def gen_factor_index(self, data_name, formula_name, vars, sector_list, start_time="", end_time="", period="1d", dividend_type="none"):
        return self._call_context(
            "gen_factor_index",
            data_name,
            formula_name,
            vars,
            sector_list,
            start_time,
            end_time,
            period,
            dividend_type,
        )
