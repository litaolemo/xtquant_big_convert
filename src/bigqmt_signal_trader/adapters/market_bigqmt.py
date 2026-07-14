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


def _raw_frame_columns(field_list):
    columns = [str(field) for field in (field_list or [])]
    if columns and "stime" not in columns:
        columns.insert(0, "stime")
    return columns


def _raw_market_data_payload(payload, field_list, stock_list):
    if not isinstance(payload, dict):
        return payload
    source = {str(code): records for code, records in payload.items()}
    codes = []
    for code in list(stock_list or []) + list(source):
        text = str(code)
        if text not in codes:
            codes.append(text)
    columns = _raw_frame_columns(field_list)
    return {
        code: {
            "__bigqmt_type__": "DataFrame",
            "columns": columns,
            "records": source.get(code) or [],
        }
        for code in codes
    }


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
        import sys

        # Locate <qmt_root>/bin.x64/{lib,Lib}/site-packages that holds the REAL
        # xtquant package. Walk up from this file (works whether we live under
        # python/bigqmt_signal_trader/adapters/ in QMT or src/... in the repo).
        real_sp = None
        start = os.path.abspath(__file__)
        for _ in range(8):
            parent = os.path.dirname(start)
            if parent == start:
                break
            for libdir in ("lib", "Lib"):
                candidate = os.path.join(parent, "bin.x64", libdir, "site-packages")
                if os.path.isdir(os.path.join(candidate, "xtquant")):
                    real_sp = candidate
                    break
            if real_sp:
                break
            start = parent

        loaded = None
        if real_sp:
            # Import the real xtquant PACKAGE (not xtdata.py standalone) so its
            # package-relative imports (xtbson etc.) resolve. Un-shadow our RPC
            # shim (python/xtquant, src/xtquant) which otherwise wins on sys.path:
            # put the real site-packages first and drop any already-imported shim
            # xtquant modules (their __file__ is not under bin.x64/).
            if real_sp not in sys.path:
                sys.path.insert(0, real_sp)
            for name in [n for n in list(sys.modules) if n == "xtquant" or n.startswith("xtquant.")]:
                mod_file = getattr(sys.modules.get(name), "__file__", "") or ""
                if "bin.x64" not in mod_file:
                    del sys.modules[name]
            try:
                module = importlib.import_module("xtquant.xtdata")
                if "bin.x64" in (getattr(module, "__file__", "") or ""):
                    loaded = module
            except Exception:
                loaded = None
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
        """Return the native xtdata SDK, resolving it lazily on first use.

        Returns None when the SDK is not importable. NOTE: in a Big QMT
        (full trading terminal) process the SDK loads but its get_client()
        cannot connect to a quote service — there is no MiniQMT process
        writing ~/.xtquant/*/xtdata.cfg. Callers must therefore be ready for
        the SDK call itself to raise "无法连接行情服务" and fall back.
        """
        if self._native_xtdata is None:
            self._native_xtdata = _load_native_xtdata()
        return self._native_xtdata

    def _native_or_context(self, func_name, context_caller, *args, **kwargs):
        """Prefer the xtdata SDK function, fall back to a ContextInfo call.

        Several data APIs exist only as xtdata module functions. When the SDK
        is available AND its quote service is reachable we use it. Otherwise
        we fall back to ContextInfo so callers get a best-effort answer.
        """
        module = self._native()
        if module is not None:
            fn = getattr(module, func_name, None)
            if fn is not None:
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    # Big QMT path: SDK present but no quote service to talk
                    # to ("无法连接行情服务"). Don't crash — let the ContextInfo
                    # fallback have a turn.
                    pass
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
        raw_method = getattr(self.context_info, "get_market_data_ex_ori", None)
        if callable(raw_method):
            raw_data = self._call_first_supported(
                self._market_data_shapes("get_market_data_ex_ori", **kwargs)
            )
            return _raw_market_data_payload(
                raw_data,
                kwargs.get("field_list") or kwargs.get("fields"),
                kwargs.get("stock_list") or kwargs.get("stock_code"),
            )
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
        # ContextInfo stub: get_divid_factors(marketAndStock, date='') — only 2
        # positional args (code + a single date). The xtdata SDK has the same
        # 2-arg shape. We accept start_time/end_time for API compatibility but
        # pass end_time (or start_time) as the single date when supplied.
        date = end_time or start_time
        if date:
            return self._call_context("get_divid_factors", stock_code, date)
        return self._call_context("get_divid_factors", stock_code)

    def _download(self, func_name, sdk_args, sdk_kwargs, ctx_call):
        """Download history via the xtdata SDK (its natural home), falling back to
        ContextInfo. If neither works, raise with the REAL native reason so the
        failure is diagnosable instead of a bare 'ContextInfo has no ...'."""
        module = self._native()
        if module is None:
            native_err = "xtdata SDK not importable"
        else:
            fn = getattr(module, func_name, None)
            if fn is None:
                native_err = "xtdata SDK has no %s" % func_name
            else:
                try:
                    return fn(*sdk_args, **sdk_kwargs)
                except Exception as exc:
                    native_err = "%s: %s" % (exc.__class__.__name__, exc)
        if getattr(self.context_info, func_name, None) is not None:
            return ctx_call()
        raise NotImplementedError(
            "%s unavailable (native xtdata -> %s; ContextInfo has no %s)" % (func_name, native_err, func_name)
        )

    def download_history_data(self, stock_code, period, start_time="", end_time="", incrementally=None):
        def _via_context():
            kwargs = {"stock_code": stock_code, "period": period, "start_time": start_time, "end_time": end_time}
            if incrementally is not None:
                kwargs["incrementally"] = incrementally
            return self._call_context("download_history_data", **kwargs)

        sdk_kwargs = {"incrementally": incrementally} if incrementally is not None else {}
        return self._download(
            "download_history_data", (stock_code, period, start_time, end_time), sdk_kwargs, _via_context
        )

    def download_history_data2(self, stock_list, period, start_time="", end_time="", incrementally=None):
        stock_list = list(stock_list or [])

        def _via_context():
            kwargs = {"stock_list": stock_list, "period": period, "start_time": start_time, "end_time": end_time}
            if incrementally is not None:
                kwargs["incrementally"] = incrementally
            return self._call_context("download_history_data2", **kwargs)

        sdk_kwargs = {"incrementally": incrementally} if incrementally is not None else {}
        return self._download(
            "download_history_data2", (stock_list, period, start_time, end_time), sdk_kwargs, _via_context
        )

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
        """Return the holiday (non-trading) date list.

        Authoritative source is the xtdata SDK (xtdata.py line 1197). In a Big
        QMT (full terminal) process the SDK is present but cannot reach its
        quote service, and ContextInfo has no get_holidays method. In that
        case we derive the holidays from the A-share trading calendar: any
        weekday in a recent window that is NOT a trading day is a holiday.
        This is slower than the SDK (it walks the calendar) but correct.
        """
        def _via_context():
            return self._call_context("get_holidays")

        try:
            result = self._native_or_context("get_holidays", _via_context)
            if result:
                return result
        except Exception:
            pass
        # Big QMT fallback: derive holidays from the trading calendar.
        return self._holidays_from_trading_calendar()

    def _holidays_from_trading_calendar(self, years_back=1):
        """Derive holiday dates (YYYYMMDD strings) from trading dates.

        Walks business days across [today - years_back, today] and collects
        those that are absent from the A-share trading calendar. Requires
        get_trading_dates to work (it does in Big QMT via ContextInfo).
        """
        import datetime

        try:
            trading = set(str(d) for d in (self.get_trading_dates("SH", "", "", -1) or []))
        except Exception:
            return []
        today = datetime.date.today()
        start = today.replace(year=today.year - years_back, month=1, day=1)
        holidays = []
        cur = start
        one_day = datetime.timedelta(days=1)
        while cur <= today:
            if cur.weekday() < 5:  # Mon-Fri
                ymd = cur.strftime("%Y%m%d")
                if ymd not in trading:
                    holidays.append(ymd)
            cur += one_day
        return holidays

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
        # xtdata SDK 函数（SDK 893 行），ContextInfo 无此方法，走 native SDK。
        def _via_context():
            return self._raise_unavailable("get_etf_info")
        return self._native_or_context("get_etf_info", _via_context)

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

    # Well-known sector names that Big QMT's ContextInfo recognises for
    # get_stock_list_in_sector / get_sector. Used as a fallback when the full
    # sector list is not enumerable (Big QMT has no get_sector_list method and
    # the xtdata SDK's quote service is unreachable inside the full terminal).
    _FALLBACK_SECTORS = (
        "沪深A股", "沪市A股", "深市A股", "科创板", "创业板",
        "上证期权", "深证期权", "中金所",
        "沪市债券", "深市债券",
        "沪市基金", "深市基金", "沪深ETF",
    )

    def get_sector_list(self):
        """Return the list of sector names.

        Authoritative source is the xtdata SDK (xtdata.py line 784). In a Big
        QMT (full terminal) process the SDK is present but cannot reach its
        quote service, and ContextInfo has no get_sector_list method either.
        In that case we fall back to a curated list of well-known sector names
        so callers can still drive get_stock_list_in_sector(name).
        """
        def _via_context():
            return self._call_context("get_sector_list")

        try:
            result = self._native_or_context("get_sector_list", _via_context)
            if result:
                return result
        except (NotImplementedError, Exception):
            pass
        return list(self._FALLBACK_SECTORS)

    def get_sector_info(self, sector_name=""):
        # xtdata SDK 函数，ContextInfo 无此方法，走 native SDK。
        def _via_context():
            return self._raise_unavailable("get_sector_info")
        return self._native_or_context("get_sector_info", _via_context, sector_name)

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

    # ------------------------------------------------------------------
    # 龙虎榜 / 股东 / 换手率（参考 Rockyzsu/QMT 暴露的 ContextInfo 方法）
    # 签名严格按 _PyContextInfo.py 桩核对，避免参数错位。
    # ------------------------------------------------------------------

    def get_longhubang(self, stock_list=None, start_time="", end_time="", count=-1):
        # ContextInfo stub: get_longhubang(stock_list=[], startTime='', endTime='', count=-1)
        # 桩里有特殊逻辑：endTime 传 int 时当作 count + endTime=startTime + startTime='0'。
        # 我们直接按 4 参数语义透传，避免触发桩的 int 歧义分支。
        return self._call_context(
            "get_longhubang",
            list(stock_list or []),
            start_time,
            end_time,
            count,
        )

    def get_top10_share_holder(self, stock_list, data_name, start_time, end_time, report_type="report_time"):
        # ContextInfo stub: get_top10_share_holder(stock_list, data_name, start_time, end_time, report_type='report_time')
        # data_name 只接受 'holder' 或 'flow_holder'；report_type 只接受 'report_time' 或 'announce_time'。
        return self._call_context(
            "get_top10_share_holder",
            stock_list,
            data_name,
            start_time,
            end_time,
            report_type,
        )

    def get_holder_num(self, stock_list=None, start_time="", end_time="", report_type="report_time"):
        # ContextInfo stub: get_holder_num(stock_list=[], startTime='', endTime='', report_type='report_time')
        # 返回股东户数 DataFrame。
        return self._call_context(
            "get_holder_num",
            list(stock_list or []),
            start_time,
            end_time,
            report_type,
        )

    def get_turnover_rate(self, stock_code=None, start_time="19720101", end_time="22010101"):
        # ContextInfo stub: get_turnover_rate(stock_code=[], start_time='19720101', end_time='22010101')
        # 注意：start_time/end_time 必须是 8 位日期串（YYYYMMDD），否则返回空 DataFrame。
        return self._call_context(
            "get_turnover_rate",
            list(stock_code or []),
            start_time,
            end_time,
        )

    def get_industry(self, industry_name):
        # ContextInfo stub: get_industry(industry_name, real_timetag = -1)
        # 注意桩签名有第二个可选参数 real_timetag，默认 -1（最新）。
        return self._call_context("get_industry", industry_name, -1)

    def get_close_price(self, market, stock_code, real_timetag, period=86400000, divid_type=0):
        # ContextInfo stub: get_close_price(market, stockCode, realTimetag, period=86400000, dividType=0)
        return self._call_context("get_close_price", market, stock_code, real_timetag, period, divid_type)

    # ------------------------------------------------------------------
    # 期权定价（BSM）/ 隐含波动率
    # ------------------------------------------------------------------

    def bsm_price(self, opt_type, target_price, strike_price, risk_free, sigma, days, dividend=0):
        # ContextInfo stub: bsm_price(optType, targetPrice, strikePrice, riskFree, sigma, days, dividend=0)
        # opt_type: 'C'(call) / 'P'(put)。target_price 可为 list（批量）。
        return self._call_context(
            "bsm_price",
            opt_type,
            target_price,
            strike_price,
            risk_free,
            sigma,
            days,
            dividend,
        )

    def bsm_iv(self, opt_type, target_price, strike_price, option_price, risk_free, days, dividend=0):
        # ContextInfo stub: bsm_iv(optType, targetPrice, strikePrice, optionPrice, riskFree, days, dividend=0)
        return self._call_context(
            "bsm_iv",
            opt_type,
            target_price,
            strike_price,
            option_price,
            risk_free,
            days,
            dividend,
        )

    def get_option_iv(self, opt_code):
        # ContextInfo stub: get_option_iv(opt_code) — 计算单只期权的隐含波动率。
        return self._call_context("get_option_iv", opt_code)

    def get_option_detail_data(self, stockcode):
        # ContextInfo stub: get_option_detail_data(stockcode)
        return self._call_context("get_option_detail_data", stockcode)

    def get_option_undl_data(self, undl_code_ref=""):
        # ContextInfo stub: get_option_undl_data(undl_code_ref='') — 标的下所有期权。
        # 传空串返回全市场期权-标的映射 dict。
        return self._call_context("get_option_undl_data", undl_code_ref)

    def get_option_undl(self, opt_code):
        # ContextInfo stub: get_option_undl(opt_code) — 期权的标的代码。
        return self._call_context("get_option_undl", opt_code)

    # ------------------------------------------------------------------
    # 财务扩展 / 因子数据
    # ------------------------------------------------------------------

    def get_raw_financial_data(self, field_list, stock_list, start_time, end_time, report_type="report_time", data_type="dict"):
        # ContextInfo stub: get_raw_financial_data(fieldList, stockList, startDate, endDate, report_type='report_time', data_type='dict')
        # 返回原始财务数据（未做字段对齐），data_type 可为 'dict'/'frame'。
        return self._call_context(
            "get_raw_financial_data",
            field_list,
            stock_list,
            start_time,
            end_time,
            report_type,
            data_type,
        )

    def get_factor_data(self, field_list, stock_list, start_date, end_date):
        # ContextInfo stub: get_factor_data(field_list, stock_list, start_date, end_date)
        # 返回因子库数据。
        return self._call_context(
            "get_factor_data",
            field_list,
            stock_list,
            start_date,
            end_date,
        )

    # ------------------------------------------------------------------
    # 历史 ST / 指数权重
    # ------------------------------------------------------------------

    def get_his_st_data(self, stock_code):
        # ContextInfo stub: get_his_st_data(stockCode) — 历史 ST 状态。
        return self._call_context("get_his_st_data", stock_code)

    def get_his_index_data(self, stock_code):
        # ContextInfo stub: get_his_index_data(stockCode) — 历史指数权重。
        return self._call_context("get_his_index_data", stock_code)

    # ------------------------------------------------------------------
    # 期货 / 合约
    # ------------------------------------------------------------------

    def get_main_contract(self, code_market):
        # ContextInfo stub: get_main_contract(codemarket)
        return self._call_context("get_main_contract", code_market)

    def get_his_contract_list(self, market):
        # ContextInfo stub: get_his_contract_list(market)
        return self._call_context("get_his_contract_list", market)

    def get_date_location(self, date):
        # ContextInfo stub: get_date_location(date) — 日期在交易日历中的位置。
        return self._call_context("get_date_location", date)

    def get_ETF_list(self, market, stock_code, type_list=None):
        # ContextInfo stub: get_ETF_list(market, stockcode, typeList=[])
        return self._call_context("get_ETF_list", market, stock_code, list(type_list or []))

    # ------------------------------------------------------------------
    # 北向资金 / 港股通
    # ------------------------------------------------------------------

    def get_north_finance_change(self, period):
        # ContextInfo stub: get_north_finance_change(period) — 北向资金流入流出。
        return self._call_context("get_north_finance_change", period)

    def get_hkt_statistics(self, stock_code):
        # ContextInfo stub: get_hkt_statistics(stock_code) — 港股通统计。
        return self._call_context("get_hkt_statistics", stock_code)

    def get_hkt_details(self, stock_code):
        # ContextInfo stub: get_hkt_details(stock_code) — 港股通明细。
        return self._call_context("get_hkt_details", stock_code)

    # ------------------------------------------------------------------
    # 自定义板块管理（写操作，仅 ContextInfo 支持）
    # ------------------------------------------------------------------

    def create_sector(self, sector_name, stock_list):
        # ContextInfo stub: create_sector(sectorname, stocklist) — 创建/更新自定义板块。
        return self._call_context("create_sector", sector_name, list(stock_list or []))

    # ------------------------------------------------------------------
    # 基础查询辅助
    # ------------------------------------------------------------------

    def get_stock_name(self, stock):
        # ContextInfo stub: get_stock_name(stock)
        return self._call_context("get_stock_name", stock)

    def get_stock_type(self, stock):
        # ContextInfo stub: get_stock_type(stock)
        return self._call_context("get_stock_type", stock)

    def get_last_close(self, stock):
        # ContextInfo stub: get_last_close(stock)
        return self._call_context("get_last_close", stock)

    def get_last_volume(self, stock):
        # ContextInfo stub: get_last_volume(stock)
        return self._call_context("get_last_volume", stock)

    def get_open_date(self, stock):
        # ContextInfo stub: get_open_date(stock) — 上市日期。
        return self._call_context("get_open_date", stock)

    def get_contract_expire_date(self, stock):
        # ContextInfo stub: get_contract_expire_date(stock) — 到期日。
        return self._call_context("get_contract_expire_date", stock)

    def get_contract_multiplier(self, stockcode):
        # ContextInfo stub: get_contract_multiplier(stockcode) — 合约乘数。
        return self._call_context("get_contract_multiplier", stockcode)

    def get_float_caps(self, stockcode):
        # ContextInfo stub: get_float_caps(stockcode) — 流通市值。
        return self._call_context("get_float_caps", stockcode)

    def get_total_share(self, stockcode):
        # ContextInfo stub: get_total_share(stockcode) — 总股本。
        return self._call_context("get_total_share", stockcode)

    def get_turn_over_rate(self, stockcode):
        # ContextInfo stub: get_turn_over_rate(stockcode) — 换手率（单值版，区别于上面的 get_turnover_rate 区间版）。
        return self._call_context("get_turn_over_rate", stockcode)

    def get_weight_in_index(self, mtkindexcode, stockcode):
        # ContextInfo stub: get_weight_in_index(mtkindexcode, stockcode) — 指数中权重。
        return self._call_context("get_weight_in_index", mtkindexcode, stockcode)

    def get_svol(self, stock):
        # ContextInfo stub: get_svol(stock)
        return self._call_context("get_svol", stock)

    def get_bvol(self, stock):
        # ContextInfo stub: get_bvol(stock)
        return self._call_context("get_bvol", stock)

    def get_risk_free_rate(self, index=-1):
        # ContextInfo stub: get_risk_free_rate(index) — 无风险利率。
        return self._call_context("get_risk_free_rate", index)

    # ------------------------------------------------------------------
    # L2 行情（需 L2 权限）
    # ------------------------------------------------------------------

    def get_l2_quote(self, field_list=None, stock_code="", start_time="", end_time="", count=-1):
        # xtdata SDK: get_l2_quote(field_list=[], stock_code='', start_time='', end_time='', count=-1)
        # ContextInfo 无此方法；走原生 xtdata SDK，连不上则 NotImplementedError。
        return self._native_or_context(
            "get_l2_quote",
            lambda: self._raise_unavailable("get_l2_quote"),
            list(field_list or []), stock_code, start_time, end_time, count,
        )

    def get_l2_order(self, field_list=None, stock_code="", start_time="", end_time="", count=-1):
        # xtdata SDK: get_l2_order(...) — L2 逐笔委托。
        return self._native_or_context(
            "get_l2_order",
            lambda: self._raise_unavailable("get_l2_order"),
            list(field_list or []), stock_code, start_time, end_time, count,
        )

    def get_l2_transaction(self, field_list=None, stock_code="", start_time="", end_time="", count=-1):
        # xtdata SDK: get_l2_transaction(...) — L2 逐笔成交。
        return self._native_or_context(
            "get_l2_transaction",
            lambda: self._raise_unavailable("get_l2_transaction"),
            list(field_list or []), stock_code, start_time, end_time, count,
        )

    def subscribe_l2thousand(self, stock_code, gear_num=0, callback=None):
        # xtdata SDK: subscribe_l2thousand(stock_code, gear_num=0, callback=None) — 千档盘口订阅。
        # callback 在 RPC 模型下无意义（无回调通道），忽略。
        module = self._native()
        if module is not None and hasattr(module, "subscribe_l2thousand"):
            try:
                return module.subscribe_l2thousand(stock_code, gear_num, callback)
            except Exception:
                pass
        return self._raise_unavailable("subscribe_l2thousand")

    # ------------------------------------------------------------------
    # 指数权重 / 交易日历 / 交易时段 / 可转债
    # ------------------------------------------------------------------

    def get_index_weight(self, index_code):
        # xtdata SDK: get_index_weight(index_code) — 指数成分权重。
        # ContextInfo 有 get_weight_in_index(indexcode, stockcode) 但语义不同（单股权重）。
        return self._native_or_context(
            "get_index_weight",
            lambda: self._raise_unavailable("get_index_weight"),
            index_code,
        )

    def get_trading_calendar(self, market, start_time="", end_time="", tradetimes=False):
        # xtdata SDK: get_trading_calendar(market, start_time='', end_time='', tradetimes=False)
        # ContextInfo 无此方法。SDK 不可用时从 get_trading_dates 派生（不含 tradetimes 时段）。
        def _fallback():
            try:
                dates = self.get_trading_dates(market, start_time, end_time, -1) or []
                return [str(d) for d in dates]
            except Exception:
                return self._raise_unavailable("get_trading_calendar")
        return self._native_or_context(
            "get_trading_calendar", _fallback, market, start_time, end_time, tradetimes
        )

    def get_trade_times(self, stockcode):
        # xtdata SDK: get_trade_times(stockcode) — 日内交易时段。
        # 传市场（'SH'）或代码（'600000.SH'）。返回 [[开始,结束,类型], ...]。
        return self._native_or_context(
            "get_trade_times",
            lambda: self._raise_unavailable("get_trade_times"),
            stockcode,
        )

    def get_cb_info(self, stockcode):
        # xtdata SDK: get_cb_info(stockcode) — 可转债信息。
        return self._native_or_context(
            "get_cb_info",
            lambda: self._raise_unavailable("get_cb_info"),
            stockcode,
        )

    def is_stock_type(self, stock, tag):
        # xtdata SDK: is_stock_type(stock, tag) — 品种判断（tag 如 'stock'/'fund'/'bond'）。
        # ContextInfo 有 is_stock/is_fund/is_future 但签名不同，这里走 SDK。
        return self._native_or_context(
            "is_stock_type",
            lambda: self._raise_unavailable("is_stock_type"),
            stock, tag,
        )

    # ------------------------------------------------------------------
    # 板块增删（自定义板块管理）
    # ------------------------------------------------------------------

    def add_sector(self, sector_name, stock_list):
        # xtdata SDK: add_sector(sector_name, stock_list) — 向自定义板块追加股票。
        # ContextInfo 用 create_sector（覆盖式），SDK 用 add_sector（追加式）。
        module = self._native()
        if module is not None and hasattr(module, "add_sector"):
            try:
                return module.add_sector(sector_name, list(stock_list or []))
            except Exception:
                pass
        # ContextInfo fallback：create_sector 是覆盖式，语义略不同但可用。
        return self._call_context("create_sector", sector_name, list(stock_list or []))

    def remove_sector(self, sector_name):
        # xtdata SDK: remove_sector(sector_name) — 删除自定义板块。
        module = self._native()
        if module is not None and hasattr(module, "remove_sector"):
            try:
                return module.remove_sector(sector_name)
            except Exception:
                pass
        return self._raise_unavailable("remove_sector")

    # ------------------------------------------------------------------
    # 数据下载扩展
    # ------------------------------------------------------------------

    def download_cb_data(self):
        # xtdata SDK: download_cb_data() — 下载可转债数据。
        module = self._native()
        if module is not None and hasattr(module, "download_cb_data"):
            try:
                return module.download_cb_data()
            except Exception:
                pass
        return self._raise_unavailable("download_cb_data")

    def download_history_contracts(self):
        # xtdata SDK: download_history_contracts() — 下载过期合约数据。
        module = self._native()
        if module is not None and hasattr(module, "download_history_contracts"):
            try:
                return module.download_history_contracts()
            except Exception:
                pass
        return self._raise_unavailable("download_history_contracts")

    def download_index_weight(self):
        # xtdata SDK: download_index_weight() — 下载指数权重数据。
        module = self._native()
        if module is not None and hasattr(module, "download_index_weight"):
            try:
                return module.download_index_weight()
            except Exception:
                pass
        return self._raise_unavailable("download_index_weight")

    def download_sector_data(self):
        # xtdata SDK: download_sector_data() — 下载行业板块数据。
        module = self._native()
        if module is not None and hasattr(module, "download_sector_data"):
            try:
                return module.download_sector_data()
            except Exception:
                pass
        return self._raise_unavailable("download_sector_data")

    # ------------------------------------------------------------------
    # 时间戳转换（纯计算，无需 QMT，服务端本地实现）
    # ------------------------------------------------------------------

    @staticmethod
    def datetime_to_timetag(datetime_str, format="%Y%m%d%H%M%S"):
        # xtdata SDK: datetime_to_timetag(datetime, format="%Y%m%d%H%M%S")
        # 把日期时间字符串转成毫秒时间戳。纯本地计算。
        import datetime as _dt
        try:
            dt = _dt.datetime.strptime(str(datetime_str), format)
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0

    @staticmethod
    def timetag_to_datetime(timetag, format):
        # xtdata SDK: timetag_to_datetime(timetag, format) — 毫秒时间戳转字符串。
        import datetime as _dt
        try:
            dt = _dt.datetime.fromtimestamp(int(timetag) / 1000.0)
            return dt.strftime(format)
        except Exception:
            return ""

    @staticmethod
    def _raise_unavailable(method_name):
        raise NotImplementedError(
            "%s is unavailable: needs native xtdata SDK quote service "
            "(not reachable in Big QMT full terminal)" % method_name
        )
