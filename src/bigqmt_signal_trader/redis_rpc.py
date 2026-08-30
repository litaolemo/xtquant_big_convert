"""Redis Pub/Sub RPC for the Big QMT runtime.

By default the service can process selected requests directly in the Redis
listener thread. The in-memory queue and ``drain_pending`` are kept as a
runtime fallback for environments where a QMT API must run from a strategy
callback thread.
"""

import base64
import datetime as _dt
import json
import math
import queue
import threading
import time
import traceback
import uuid

from .adapters.redis_common import decode_text
from .code_utils import normalize_stock_code
from .models import AccountSnapshot, OrderRef, OrderRequest


# time.monotonic: unaffected by wall-clock jumps, so a settle deadline
# survives an NTP correction mid-session. Python 3.3+, fine on QMT's 3.6.
_monotonic = time.monotonic


RPC_REVISION = "20260715-execution-snapshot-v1"


READ_METHODS = {
    "ping",
    "get_deployment_info",
    "probe_capabilities",
    "get_ticks",
    "get_instrument",
    "get_instrument_type",
    "get_market_data",
    "get_market_data_ex",
    "get_local_data",
    "get_stock_list_in_sector",
    "get_sector_list",
    "get_sector_info",
    "get_markets",
    "get_market_last_trade_date",
    "get_divid_factors",
    "download_history_data",
    "download_history_data2",
    "get_trading_dates",
    "get_holidays",
    "download_holiday_data",
    "get_ipo_info",
    "get_etf_info",
    "download_etf_info",
    "get_option_list",
    "get_his_option_list",
    "get_his_option_list_batch",
    "get_financial_data",
    "download_financial_data",
    "download_financial_data2",
    "call_formula",
    "subscribe_formula",
    "unsubscribe_formula",
    "get_formula_result",
    "gen_factor_index",
    "get_positions",
    "get_position_statistics",
    "get_asset",
    "query_orders",
    "query_trades",
    "query_execution_snapshot",
    "query_stock_position",
    "sync_positions",
    "submit_download_history_data",
    "submit_download_history_data2",
    "get_download_status",
    "wait_download",
    # 账户 / 融资融券 / 交易扩展查询（官方全局函数 + detail types）
    "query_account_infos",
    "query_account_status",
    "query_credit_detail",
    "query_stk_compacts",
    "query_credit_subjects",
    "query_credit_slo_code",
    "query_credit_assure",
    "query_appointment_info",
    "query_smt_secu_info",
    "query_smt_secu_rate",
    "smt_appointment",
    # 官方交易查询函数（直接暴露，运行时注入的全局函数）
    "get_value_by_order_id",
    "get_last_order_id",
    "get_ipo_data",
    "get_new_purchase_limit",
    "get_history_trade_detail_data",
    "get_assure_contract",
    "get_enable_short_contract",
    "get_unclosed_compacts",
    "get_closed_compacts",
    "get_debt_contract",
    "get_option_subject_position",
    "get_comb_option",
    "get_hkt_exchange_rate",
}

ORDER_METHODS = {
    "submit_order",
    "submit_orders_batch",
    "cancel_order",
}

# Whole-quote push subscription control methods. These drive a server-side
# QuoteSubscriptionManager (reference-counted ContextInfo.subscribe_whole_quote)
# rather than a market_data read; the data itself flows over the push channel.
QUOTE_SUBSCRIPTION_METHODS = {
    "subscribe_whole_quote",
    "unsubscribe_whole_quote",
    "quote_keepalive",
}

LISTENER_DEFERRED_METHODS = {
    "sync_positions",
    # Trade-context queries route through QMT's get_trade_detail_data, which
    # returns EMPTY when called from the background RPC thread (it needs the main
    # strategy thread's context). Defer them so the adjust drain runs them on the
    # main thread -- costs up to one adjust interval (~500ms) but returns real
    # data. Asset queries use the same QMT detail API and must follow this rule.
    "get_asset",
    "get_positions",
    "get_position_statistics",
    "query_stock_position",
    "query_orders",
    "query_trades",
    "query_account_infos",
    "query_account_status",
    "query_credit_detail",
    "query_stk_compacts",
    "query_credit_subjects",
    "query_credit_slo_code",
    "query_credit_assure",
    "query_appointment_info",
    "get_ipo_data",   # 8-28: 交易类查询, 需主线程上下文 (后台线程返回空)
    "query_smt_secu_info",
    "query_smt_secu_rate",
    "get_value_by_order_id",
    "get_last_order_id",
    "get_history_trade_detail_data",
}

# Trade-context queries route through QMT's get_trade_detail_data, which
# returns EMPTY when called from the background RPC thread (it needs the main
# strategy thread's context). Defer them so the adjust drain runs them on the
# main thread -- costs up to one adjust interval (~500ms) but returns real
# data. Asset queries use the same QMT detail API and must follow this rule.
#
# NOTE: do NOT blanket-defer all READ_METHODS here. Market-data reads
# (get_full_tick, get_market_data, ...) are thread-safe in the embedded
# terminal and must stay inline for low latency; the ZMQ transport has no
# adjust-driven drain for pending requests (its drain_request_queue is a
# no-op when the router thread exists), so deferring everything would stall
# them forever. Only the trade-context methods listed above go through drain.


METHOD_ALIASES = {
    "get_full_tick": "get_ticks",
    "get_instrument_detail": "get_instrument",
    "get_instrumentdetail": "get_instrument",
    "getDividFactors": "get_divid_factors",
    "query_stock_asset": "get_asset",
    "query_stock_positions": "get_positions",
    "query_position_statistics": "get_position_statistics",
    "query_stock_orders": "query_orders",
    "query_stock_trades": "query_trades",
    "order_stock": "submit_order",
    "order_stock_async": "submit_order",
    "order_stock_batch": "submit_orders_batch",
    "cancel_order_stock": "cancel_order",
    "cancel_order_stock_sysid": "cancel_order",
}

BUY_ORDER_TYPES = {"23", "STOCK_BUY", "BUY", "B"}
SELL_ORDER_TYPES = {"24", "STOCK_SELL", "SELL", "S"}
CANCELABLE_ORDER_STATUSES = {"50", "55"}
SAFE_B64_PREFIX = "b64s:"
SAFE_B64_DIGIT_ENCODE = str.maketrans("0123456789", "!#$%&()*~?")
SAFE_B64_DIGIT_DECODE = str.maketrans("!#$%&()*~?", "0123456789")
MARKET_DATA_METHODS = {
    "get_instrument_type",
    "get_market_data",
    "get_market_data_ex",
    "get_local_data",
    "get_stock_list_in_sector",
    "get_sector_list",
    "get_sector_info",
    "get_markets",
    "get_market_last_trade_date",
    "get_divid_factors",
    "download_history_data",
    "download_history_data2",
    "get_trading_dates",
    "get_holidays",
    "download_holiday_data",
    "get_ipo_info",
    "get_etf_info",
    "download_etf_info",
    "get_option_list",
    "get_his_option_list",
    "get_his_option_list_batch",
    "get_financial_data",
    "download_financial_data",
    "download_financial_data2",
    "call_formula",
    "subscribe_formula",
    "unsubscribe_formula",
    "get_formula_result",
    "gen_factor_index",
    # 龙虎榜 / 股东 / 换手率 / 行业 / 收盘价
    "get_longhubang",
    "get_top10_share_holder",
    "get_holder_num",
    "get_turnover_rate",
    "get_industry",
    "get_close_price",
    # 期权定价 / 隐含波动率
    "bsm_price",
    "bsm_iv",
    "get_option_iv",
    "get_option_detail_data",
    "get_option_undl_data",
    "get_option_undl",
    # 财务扩展 / 因子
    "get_raw_financial_data",
    "get_factor_data",
    # 历史 ST / 指数权重
    "get_his_st_data",
    "get_his_index_data",
    # 期货 / 合约
    "get_main_contract",
    "get_his_contract_list",
    "get_date_location",
    "get_ETF_list",
    # 北向资金 / 港股通
    "get_north_finance_change",
    "get_hkt_statistics",
    "get_hkt_details",
    # 自定义板块（写）
    "create_sector",
    # 基础查询辅助
    "get_stock_name",
    "get_stock_type",
    "get_last_close",
    "get_last_volume",
    "get_open_date",
    "get_contract_expire_date",
    "get_contract_multiplier",
    "get_float_caps",
    "get_total_share",
    "get_turn_over_rate",
    "get_weight_in_index",
    "get_svol",
    "get_bvol",
    "get_risk_free_rate",
    # L2 行情（需 L2 权限 + 原生 xtdata SDK 行情服务）
    "get_l2_quote",
    "get_l2_order",
    "get_l2_transaction",
    "subscribe_l2thousand",
    # 指数权重 / 交易日历 / 交易时段 / 可转债 / 品种判断
    "get_index_weight",
    "get_trading_calendar",
    "get_trade_times",
    "get_cb_info",
    "is_stock_type",
    # 板块增删
    "add_sector",
    "remove_sector",
    # 数据下载扩展
    "download_cb_data",
    "download_history_contracts",
    "download_index_weight",
    "download_sector_data",
    # 时间戳转换（纯计算，服务端本地）
    "datetime_to_timetag",
    "timetag_to_datetime",
}

# Keep READ_METHODS in sync with MARKET_DATA_METHODS: every market-data method
# forwarded to the adapter is also callable over RPC. (create_sector is a write
# op — creates/updates a custom sector — but it is harmless to expose; trading
# order writes stay gated behind ORDER_METHODS + allow_order_methods.)
READ_METHODS |= MARKET_DATA_METHODS
READ_METHODS |= QUOTE_SUBSCRIPTION_METHODS


def _maybe_scalar(value):
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            return value
    return value


def _is_redis_timeout(exc):
    name = exc.__class__.__name__.lower()
    module = getattr(exc.__class__, "__module__", "")
    text = str(exc).lower()
    return ("redis" in module and "timeout" in name) or "timeout reading from socket" in text


def to_jsonable(value):
    value = _maybe_scalar(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "isoformat") and value.__class__.__module__.startswith("pandas"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if hasattr(value, "to_dict") and hasattr(value, "columns") and hasattr(value, "index"):
        try:
            frame = value.reset_index()
            return {
                "__bigqmt_type__": "DataFrame",
                "columns": [str(col) for col in frame.columns],
                "records": to_jsonable(frame.to_dict("records")),
            }
        except Exception:
            return str(value)
    if hasattr(value, "to_dict") and hasattr(value, "index") and not isinstance(value, dict):
        try:
            return {
                "__bigqmt_type__": "Series",
                "data": to_jsonable(value.to_dict()),
            }
        except Exception:
            return str(value)
    # pandas Panel (3-D). QMT ships pandas 0.22, where get_financial_data with
    # several stocks AND several dates still returns one. A Panel has no
    # .columns and no .index, so it falls past the DataFrame and Series
    # branches all the way to the __dict__ fallback -- and vars(panel) is
    # {'_data': ..., 'is_copy': None}, which the underscore filter reduces to
    # {'is_copy': None}. That is exactly what callers got back (issue #115):
    # not an error, just the one public attribute the object happened to have.
    #
    # pandas removed Panel in 1.0, so the client cannot rebuild one even if we
    # sent the axes. Send a DataFrame per item instead; it arrives as a dict.
    if (hasattr(value, "major_axis") and hasattr(value, "minor_axis")
            and hasattr(value, "items") and not isinstance(value, dict)):
        try:
            labels = [str(item) for item in value.items]
            return {
                "__bigqmt_type__": "Panel",
                "items": labels,
                "major_axis": [str(item) for item in value.major_axis],
                "minor_axis": [str(item) for item in value.minor_axis],
                "data": {str(item): to_jsonable(value[item]) for item in value.items},
            }
        except Exception:
            return str(value)
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return to_jsonable(value.tolist())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    if hasattr(value, "__dict__"):
        return {
            key: to_jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


class OrderSettlement(object):
    """One order awaiting its order_sys_id.

    Why this cannot simply run on a background thread: get_trade_detail_data
    returns EMPTY off the main strategy thread (see LISTENER_DEFERRED_METHODS),
    so a background poll would find nothing and report every order as silently
    rejected. The retries have to happen on the adjust thread -- just without
    holding it.

    server_error is carried here rather than on handlers._last_server_error:
    that slot belongs to whichever request is in flight, and settling writes to
    it long after this request left the handler (issue #43).
    """

    __slots__ = ("order_request", "result", "deadline", "attempts", "server_error",
                 "request", "response")

    def __init__(self, order_request, result, deadline):
        self.order_request = order_request
        self.result = result
        self.deadline = deadline
        self.attempts = 0
        self.server_error = ""
        self.request = None
        self.response = None


class BigQmtRpcHandlers:
    """Whitelisted RPC method handlers backed by replaceable adapters."""

    def __init__(
        self,
        account_id,
        market_data,
        position_provider,
        order_gateway=None,
        position_sync_sink=None,
        allow_order_methods=False,
        allowed_methods=None,
        qmt_api=None,
        settle_orders_inline=False,
        order_settle_timeout_seconds=3.0,
        quote_subscription_manager=None,
    ):
        self.account_id = str(account_id or "")
        self.market_data = market_data
        self.position_provider = position_provider
        self.order_gateway = order_gateway
        self.position_sync_sink = position_sync_sink
        self.allow_order_methods = bool(allow_order_methods)
        self.quote_subscription_manager = quote_subscription_manager
        # QMT runtime-injected global functions (passorder/get_trade_detail_data/
        # 融资融券查询等)。由 strategy._build_config 解析注入。
        self.qmt_api = dict(qmt_api or {})
        self._submit_journal = {}
        # Order settlement. Async by default: blocking here holds the QMT main
        # strategy thread, which serializes every other request behind it and
        # caps throughput at ~2 orders/sec (issue #44).
        self._pending_settlement = None
        self.settle_orders_inline = bool(settle_orders_inline)
        self.order_settle_timeout_seconds = float(order_settle_timeout_seconds)
        # Server-side diagnostic for silent failures (e.g. passorder submitted
        # but order not found in system). Surfaced to client via server_error.
        self._last_server_error = ""
        if allowed_methods is None:
            allowed = set(READ_METHODS)
            if self.allow_order_methods:
                allowed.update(ORDER_METHODS)
            self.allowed_methods = allowed
        else:
            self.allowed_methods = {str(method) for method in allowed_methods}

    def _request_account_id(self, params):
        params = params or {}
        account = params.get("account")
        if isinstance(account, dict):
            account = account.get("account_id") or account.get("accountID") or account.get("id")
        account_id = str(params.get("account_id") or account or self.account_id or "")
        if not account_id:
            raise ValueError("account_id is required")
        return account_id

    def _canonical_method(self, method):
        return METHOD_ALIASES.get(method, method)

    def handle(self, method, params=None):
        requested_method = str(method or "").strip()
        method = self._canonical_method(requested_method)
        params = dict(params or {})
        # Clear the diagnostic slot per request. It is instance state read by
        # EVERY response (see _build_response), so without this a single failed
        # submit_order stamps its server_error onto every later ping/query until
        # the next order runs -- reporting a stale failure on requests that
        # succeeded (issue #43).
        self._last_server_error = ""
        self._pending_settlement = None
        if not requested_method:
            raise ValueError("method is required")
        if method not in self.allowed_methods:
            raise ValueError("rpc method is not allowed: %s" % requested_method)
        if method in ORDER_METHODS and not self.allow_order_methods:
            raise PermissionError("order rpc methods are disabled")
        handler = getattr(self, "_handle_%s" % method, None)
        if handler is None and method in MARKET_DATA_METHODS:
            return self._handle_market_data_method(method, params)
        elif handler is None:
            raise ValueError("rpc method is not implemented: %s" % requested_method)
        return handler(params)

    def _handle_ping(self, params):
        return {
            "pong": True,
            "account_id": self.account_id,
            "allow_order_methods": bool(self.allow_order_methods),
            "rpc_revision": RPC_REVISION,
            "version": _deployed_version(),
            "server_time": _dt.datetime.now(),
        }

    def _handle_get_deployment_info(self, params):
        """Where this bridge is running from, and which build it is.

        Deploying into QMT is a file copy, and QMT keeps modules in sys.modules
        across strategy re-runs -- so "the copy never happened" and "the copy
        landed but was not picked up" are indistinguishable from the client
        side. This lets the client ask instead of guessing, and gives a sync
        tool somewhere to copy to without the trading process rewriting its own
        code.
        """
        import os
        import sys as _sys

        info = {
            "version": "",
            "package_dir": "",
            "qmt_python_dir": "",
            "strategy_dir": "",
            "python_version": "",
            "rpc_revision": RPC_REVISION,
        }
        try:
            info["python_version"] = ".".join(
                str(part) for part in _sys.version_info[:3])
        except Exception:
            pass
        try:
            # Submodule: the QMT sandbox never execs the root package.
            from bigqmt_signal_trader.version import deployment_report

            version, package_dir = deployment_report()
            info["version"] = version
            info["package_dir"] = package_dir
            # The QMT python directory is the package's parent: that is where a
            # sync tool writes, and where the top-level modules live.
            info["qmt_python_dir"] = os.path.dirname(package_dir)
        except Exception as exc:
            info["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        try:
            strategy = _sys.modules.get("bigqmt_signal_trader_strategy")
            path = getattr(strategy, "__file__", "")
            if path:
                info["strategy_dir"] = os.path.dirname(os.path.abspath(path))
        except Exception:
            pass
        return info

    # probe 时检查的关键 QMT 运行时全局函数（存在与否决定对应能力可用性）。
    _PROBE_QMT_GLOBALS = (
        "passorder", "cancel", "get_trade_detail_data",
        "download_history_data", "download_history_data2", "down_history_data",
        "get_history_trade_detail_data", "get_value_by_order_id", "get_last_order_id",
        "get_ipo_data", "get_new_purchase_limit",
        "get_assure_contract", "get_enable_short_contract",
        "get_unclosed_compacts", "get_closed_compacts", "get_debt_contract",
        "get_option_subject_position", "get_comb_option", "get_hkt_exchange_rate",
    )

    # probe 时抽查的 ContextInfo 方法。
    _PROBE_CONTEXT_METHODS = (
        "get_full_tick", "get_market_data_ex", "get_market_data", "get_local_data",
        "subscribe_quote", "subscribe_whole_quote", "unsubscribe_quote",
        "get_trading_dates", "get_financial_data", "get_stock_list_in_sector",
        "do_back_test", "get_trade_detail_data",
    )

    def _handle_probe_capabilities(self, params):
        """只读能力探测：当前部署暴露了哪些 QMT callable。

        部署后跑一次就能回答「这台券商 QMT 缺什么」——只读调用，不触发任何
        委托。返回三部分：运行时全局函数绑定情况、ContextInfo 方法存在性、
        信用接口只读探测（调用一次看是否报错/返回行数）。
        """
        info = {
            "account_id": self.account_id,
            "allow_order_methods": self.allow_order_methods,
            "settle_orders_inline": self.settle_orders_inline,
            "order_settle_timeout_seconds": self.order_settle_timeout_seconds,
            "qmt_globals": {},
            "contextinfo_methods": {},
            "credit_probe": {},
            "server_time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        for name in self._PROBE_QMT_GLOBALS:
            info["qmt_globals"][name] = callable(self.qmt_api.get(name))
        context_info = getattr(self.market_data, "context_info", None)
        for name in self._PROBE_CONTEXT_METHODS:
            info["contextinfo_methods"][name] = callable(getattr(context_info, name, None))
        # 信用接口只读探测：不存在的全局直接标 unavailable；存在的真调一次，
        # 记录是否报错和返回行数（担保品/融券标的可能很多行，只计数）。
        for name in ("get_assure_contract", "get_enable_short_contract",
                     "get_unclosed_compacts", "get_debt_contract"):
            func = self.qmt_api.get(name)
            if not callable(func):
                info["credit_probe"][name] = {"available": False}
                continue
            try:
                rows = func(self.account_id) or []
                info["credit_probe"][name] = {
                    "available": True, "ok": True, "rows": len(rows),
                }
            except Exception as exc:
                info["credit_probe"][name] = {
                    "available": True, "ok": False,
                    "error": "%s: %s" % (exc.__class__.__name__, exc),
                }
        return info

    # ------------------------------------------------------------------
    # 全推行情订阅控制（引用计数共享 ContextInfo.subscribe_whole_quote）。
    # 数据本身走推送通道；这里只负责订阅生命周期 + 心跳。
    # ------------------------------------------------------------------

    def _require_quote_manager(self):
        manager = self.quote_subscription_manager
        if manager is None:
            raise RuntimeError("whole-quote push subscription is not configured on this server")
        return manager

    @staticmethod
    def _quote_params(params, require_codes=False):
        params = params or {}
        client_id = str(params.get("client_id") or "").strip()
        sub_id = str(params.get("sub_id") or "").strip()
        if not client_id:
            raise ValueError("client_id is required")
        if not sub_id:
            raise ValueError("sub_id is required")
        codes = [str(c) for c in (params.get("codes") or []) if str(c or "").strip()]
        if require_codes and not codes:
            raise ValueError("codes is required")
        return client_id, sub_id, codes

    def _handle_subscribe_whole_quote(self, params):
        manager = self._require_quote_manager()
        client_id, sub_id, codes = self._quote_params(params, require_codes=True)
        return manager.subscribe(client_id, sub_id, codes)

    def _handle_unsubscribe_whole_quote(self, params):
        manager = self._require_quote_manager()
        client_id, sub_id, _codes = self._quote_params(params)
        manager.unsubscribe(client_id, sub_id)
        return {}

    def _handle_quote_keepalive(self, params):
        manager = self._require_quote_manager()
        client_id, sub_id, _codes = self._quote_params(params)
        manager.keepalive(client_id, sub_id)
        return {}

    def _download_job_redis(self):
        redis_client = getattr(self, "download_job_redis_client", None)
        if redis_client is None:
            raise RuntimeError("download jobs require a Redis client")
        return redis_client

    def _handle_submit_download_history_data2(self, params):
        from .download_jobs import submit_download_job

        stock_list = params.get("stock_list") or params.get("stock_code") or []
        if isinstance(stock_list, str):
            stock_list = [stock_list]
        return submit_download_job(
            self._download_job_redis(),
            self.account_id,
            stock_list,
            params.get("period"),
            method="download_history_data2",
            start_time=params.get("start_time", ""),
            end_time=params.get("end_time", ""),
            incrementally=params.get("incrementally"),
            chunk_size=int(params.get("chunk_size") or getattr(self, "download_job_chunk_size", 10)),
            job_ttl_seconds=int(params.get("job_ttl_seconds") or getattr(self, "download_job_ttl_seconds", 3600)),
        )

    def _handle_submit_download_history_data(self, params):
        stock_code = params.get("stock_code") or params.get("code")
        next_params = dict(params or {})
        next_params["stock_list"] = [stock_code] if stock_code else []
        return self._handle_submit_download_history_data2(next_params)

    def _handle_get_download_status(self, params):
        from .download_jobs import read_download_status

        job_id = params.get("job_id")
        if not job_id:
            raise ValueError("job_id is required")
        status = read_download_status(self._download_job_redis(), self.account_id, job_id)
        if status is None:
            raise KeyError("download job not found or expired: %s" % job_id)
        return status

    def _handle_wait_download(self, params):
        from .download_jobs import wait_download_job

        job_id = params.get("job_id")
        if not job_id:
            raise ValueError("job_id is required")
        return wait_download_job(
            self._download_job_redis(),
            self.account_id,
            job_id,
            wait_seconds=float(params.get("wait_seconds", 600.0)),
            poll_interval_seconds=float(params.get("poll_interval_seconds", 0.5)),
        )


    def _handle_get_ticks(self, params):
        codes = params.get("codes")
        if isinstance(codes, str):
            codes = [codes]
        if not codes:
            code = params.get("code")
            codes = [code] if code else []
        if not codes:
            raise ValueError("codes or code is required")
        types = params.get("types")
        if isinstance(types, str):
            types = [types]
        if not types:
            # Pass one argument when nothing was asked for, so a market-data
            # provider whose get_ticks takes only `codes` keeps working.
            return self.market_data.get_ticks(codes)
        return self.market_data.get_ticks(codes, types=list(types))

    def _handle_get_instrument(self, params):
        code = params.get("code")
        if not code:
            raise ValueError("code is required")
        return self.market_data.get_instrument(code)

    def _handle_market_data_method(self, method, params):
        handler = getattr(self.market_data, method, None)
        if handler is None:
            raise NotImplementedError("market data method is not available: %s" % method)
        return handler(**dict(params or {}))

    def _handle_get_positions(self, params):
        return self.position_provider.get_positions(self._request_account_id(params))

    def _handle_get_position_statistics(self, params):
        return self.position_provider.get_position_statistics(self._request_account_id(params))

    def _handle_query_stock_position(self, params):
        stock_code = str(params.get("stock_code") or params.get("code") or "").strip()
        if not stock_code:
            raise ValueError("stock_code is required")
        normalized_code = normalize_stock_code(stock_code)
        positions = self.position_provider.get_positions(self._request_account_id(params))
        return positions.get(normalized_code)

    def _handle_get_asset(self, params):
        return self.position_provider.get_asset(self._request_account_id(params))

    def _handle_query_orders(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        # strategy_name filters orders by the name used in passorder. An empty
        # string returns ALL orders for the account (verified via diagnostic:
        # st="" -> 9 orders, st="bigqmt_signal_trader" -> 0). Default to ""
        # so callers see every order unless they explicitly filter.
        orders = self.order_gateway.query_orders(
            self._request_account_id(params),
            str(params.get("strategy_name") or ""),
        )
        if _bool_value(params.get("cancelable_only"), False):
            return [
                order
                for order in orders
                if str(getattr(order, "status", "") or "") in CANCELABLE_ORDER_STATUSES
            ]
        return orders

    def _handle_query_trades(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        # Empty strategy_name returns ALL deals for the account (see query_orders
        # note). Default "" so callers see every trade unless they filter.
        strategy_name = params.get("strategy_name")
        if strategy_name is None:
            strategy_name = ""
        return self.order_gateway.query_trades(
            self._request_account_id(params),
            str(strategy_name),
        )

    def _handle_query_execution_snapshot(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        account_id = self._request_account_id(params)
        order_name = params.get("order_strategy_name")
        if order_name is None:
            order_name = params.get("strategy_name", "bigqmt_signal_trader")
        trade_name = params.get("trade_strategy_name")
        if trade_name is None:
            trade_name = ""
        return {
            "account_id": account_id,
            "server_time": _dt.datetime.now(),
            "rpc_revision": RPC_REVISION,
            "orders": self.order_gateway.query_orders(account_id, str(order_name)),
            "trades": self.order_gateway.query_trades(account_id, str(trade_name)),
        }

    def _handle_sync_positions(self, params):
        account_id = self._request_account_id(params)
        snapshot = AccountSnapshot(
            account_id=account_id,
            asset=self.position_provider.get_asset(account_id),
            positions=self.position_provider.get_positions(account_id),
            reason=str(params.get("reason") or "rpc"),
            updated_at=_dt.datetime.now(),
        )
        if self.position_sync_sink is not None:
            self.position_sync_sink.publish(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # 账户 / 融资融券 / 交易扩展查询
    # 这些是 Big QMT 运行时注入的全局函数（同 passorder），不在 ContextInfo 桩里。
    # 函数名严格按官方文档（trading_function.html），通过 self.qmt_api 调用。
    # 无该权限/函数未注入时降级为空列表。
    # ------------------------------------------------------------------

    def _call_qmt_global(self, func_name, *args, **kwargs):
        """Call a QMT runtime-injected global function, returning [] on failure.

        These functions (get_assure_contract / get_unclosed_compacts / ...)
        are injected by QMT into the process global namespace, same as
        passorder. When unavailable (no margin account, function not bound)
        we degrade to [] rather than crashing the RPC.
        """
        func = self.qmt_api.get(func_name)
        if func is None:
            return []
        try:
            return _normalize_detail_rows(func(*args, **kwargs))
        except Exception:
            return []

    def _call_qmt_mapping(self, func_name, *args, **kwargs):
        """Same as _call_qmt_global, for the QMT globals that answer with a
        mapping rather than a row list -- get_ipo_data, get_new_purchase_limit.

        The row normaliser would iterate such a dict by key and throw the values
        away, so these keep their shape and only have their values made
        JSON-safe.
        """
        func = self.qmt_api.get(func_name)
        if func is None:
            return {}
        try:
            data = func(*args, **kwargs)
        except Exception:
            return {}
        if isinstance(data, dict):
            return dict((str(key), _normalize_mapping_value(value))
                        for key, value in data.items())
        # Some brokers hand back rows even here; normalise rather than drop.
        return _normalize_detail_rows(data)

    def _configured_account_type(self):
        return str(
            getattr(self.order_gateway, "account_type", "CREDIT") or "CREDIT"
        ).strip().upper()

    def _query_trade_detail(self, params, detail_type, strategy_name=""):
        """get_trade_detail_data with one of the 6 official detail types.

        Official strDatatype values: ACCOUNT / POSITION / POSITION_STATISTICS /
        ORDER / DEAL / TASK. Other strings (CREDIT etc.) are NOT supported by
        this API — use the dedicated functions below for margin queries.
        """
        account_id = self._request_account_id(params)
        gateway = self.order_gateway
        if gateway is None or gateway.get_trade_detail_data is None:
            return []
        try:
            rows = gateway.get_trade_detail_data(account_id, gateway.account_type, detail_type, strategy_name)
            return _normalize_detail_rows(rows)
        except Exception:
            return []

    def _handle_query_account_infos(self, params):
        # 账户信息 — get_trade_detail_data(ACCOUNT)
        return self._query_trade_detail(params, "ACCOUNT")

    def _handle_query_account_status(self, params):
        # 账户状态 — 用 TASK detail type 近似（委托任务状态）
        return self._query_trade_detail(params, "TASK")

    def _handle_query_credit_detail(self, params):
        # 融资融券账户明细 — 官方独立函数 get_debt_contract
        return self._call_qmt_global("get_debt_contract", self._request_account_id(params))

    def _handle_query_stk_compacts(self, params):
        # 未平仓合约（负债）— 官方 get_unclosed_compacts
        return self._call_qmt_global(
            "get_unclosed_compacts",
            self._request_account_id(params),
            self._configured_account_type(),
        )

    def _handle_query_credit_subjects(self, params):
        # 融资标的（担保品）— 官方 get_assure_contract
        return self._call_qmt_global("get_assure_contract", self._request_account_id(params))

    def _handle_query_credit_slo_code(self, params):
        # 融券标的 — 官方 get_enable_short_contract
        return self._call_qmt_global("get_enable_short_contract", self._request_account_id(params))

    def _handle_query_credit_assure(self, params):
        # 担保品合约 — 同 query_credit_subjects（get_assure_contract）
        return self._call_qmt_global("get_assure_contract", self._request_account_id(params))

    def _handle_query_appointment_info(self, params):
        # 新股数据 — 官方 get_ipo_data(type)
        # 8-28 修复: 原实现把 account_id 当第一个参数传给 get_ipo_data (期望 type),
        # 导致返回 [{}]. 改为透传 type 参数 ("STOCK"/"BOND"/缺省全部).
        return self._call_qmt_global(
            "get_ipo_data", str(params.get("type") or params.get("stock_type") or ""))

    def _handle_query_smt_secu_info(self, params):
        # 期权标的持仓 — 官方 get_option_subject_position
        return self._call_qmt_global("get_option_subject_position", self._request_account_id(params))

    def _handle_query_smt_secu_rate(self, params):
        # 组合期权 — 官方 get_comb_option
        return self._call_qmt_global("get_comb_option", self._request_account_id(params))

    def _handle_smt_appointment(self, params):
        # SMB/预约打新属于交易类，需要下单通道；当前不支持。
        raise NotImplementedError("smt_appointment is not supported via Big QMT RPC")

    # 官方交易查询函数（直接暴露）
    def _handle_get_value_by_order_id(self, params):
        order_id = str(params.get("order_id") or params.get("order_sysid") or "")
        if not order_id:
            raise ValueError("order_id is required")
        return self._call_qmt_global("get_value_by_order_id", order_id)

    def _handle_get_last_order_id(self, params):
        return self._call_qmt_global("get_last_order_id", self._request_account_id(params))

    def _handle_get_ipo_data(self, params):
        # get_ipo_data answers with a dict KEYED BY SUBSCRIPTION CODE, not with
        # detail rows. Sending it through _normalize_detail_rows iterates the
        # dict, i.e. its keys, and attribute-scrapes each code string -- so
        # {"730001": {...}, "001234": {...}} came out as [{}, {}]: codes, issue
        # prices and quantities all gone. #96 fixed the `type` argument but the
        # response was still being destroyed here.
        return self._call_qmt_mapping(
            "get_ipo_data", str(params.get("type") or params.get("stock_type") or ""))

    def _handle_get_new_purchase_limit(self, params):
        # Documented as returning a dict of 板块 -> 额度, so it has the same
        # shape problem get_ipo_data had (6.10 in the API reference).
        return self._call_qmt_mapping(
            "get_new_purchase_limit", self._request_account_id(params))

    def _handle_get_history_trade_detail_data(self, params):
        account_id = self._request_account_id(params)
        detail_type = str(params.get("detail_type") or params.get("datatype") or "DEAL")
        start_date = str(params.get("start_date") or params.get("start_time") or "")
        end_date = str(params.get("end_date") or params.get("end_time") or "")
        result = self._call_qmt_global(
            "get_history_trade_detail_data", account_id, detail_type, start_date, end_date
        )
        return result

    def _handle_get_assure_contract(self, params):
        return self._call_qmt_global("get_assure_contract", self._request_account_id(params))

    def _handle_get_enable_short_contract(self, params):
        return self._call_qmt_global("get_enable_short_contract", self._request_account_id(params))

    def _handle_get_unclosed_compacts(self, params):
        return self._call_qmt_global(
            "get_unclosed_compacts",
            self._request_account_id(params),
            self._configured_account_type(),
        )

    def _handle_get_closed_compacts(self, params):
        return self._call_qmt_global(
            "get_closed_compacts",
            self._request_account_id(params),
            self._configured_account_type(),
        )

    def _handle_get_debt_contract(self, params):
        return self._call_qmt_global("get_debt_contract", self._request_account_id(params))

    def _handle_get_option_subject_position(self, params):
        return self._call_qmt_global("get_option_subject_position", self._request_account_id(params))

    def _handle_get_comb_option(self, params):
        return self._call_qmt_global("get_comb_option", self._request_account_id(params))

    def _handle_get_hkt_exchange_rate(self, params):
        return self._call_qmt_global("get_hkt_exchange_rate")

    def _handle_download_history_data(self, params):
        """download_history_data is a QMT global function (issue #32).

        It is NOT a ContextInfo method — the adapter's _call_context path
        always raised NotImplementedError. Now route through qmt_api (the
        injected global), falling back to the adapter (which tries native
        xtdata SDK then ContextInfo).
        """
        func = self.qmt_api.get("download_history_data")
        # Some QMT builds only expose down_history_data (same signature, 4 args).
        if func is None:
            func = self.qmt_api.get("down_history_data")
        if func is not None:
            try:
                stock_code = str(params.get("stock_code") or "")
                period = str(params.get("period") or "1d")
                start_time = str(params.get("start_time") or "")
                end_time = str(params.get("end_time") or "")
                result = func(stock_code, period, start_time, end_time)
                return bool(result) if result is not None else True
            except Exception as exc:
                raise RuntimeError("download_history_data failed: %s" % exc)
        # Fallback: adapter tries native xtdata SDK then ContextInfo.
        # If the adapter lacks the method, return False (not crash).
        try:
            return self._handle_market_data_method("download_history_data", params)
        except (NotImplementedError, AttributeError):
            return False

    def _handle_download_history_data2(self, params):
        """download_history_data2 is a QMT global function (issue #32).

        Native signature includes an optional callback for progress; the QMT
        global may require it, so pass a no-op when the client didn't.

        Some QMT builds expose only the single-stock download globals
        (``download_history_data`` / ``down_history_data``) — fall back to a
        per-code loop with those. Without this the RPC returned False and
        nothing was downloaded, so reads only ever saw the latest day
        (issue #54).
        """
        stock_list = list(params.get("stock_list") or [])
        period = str(params.get("period") or "1d")
        start_time = str(params.get("start_time") or "")
        end_time = str(params.get("end_time") or "")
        func = self.qmt_api.get("download_history_data2")
        if func is not None:
            try:
                # Try with a no-op callback first (some QMT builds require it);
                # fall back to 4-arg call if that raises TypeError.
                try:
                    result = func(stock_list, period, start_time, end_time, lambda data: None)
                except TypeError:
                    result = func(stock_list, period, start_time, end_time)
                return bool(result) if result is not None else True
            except Exception as exc:
                raise RuntimeError("download_history_data2 failed: %s" % exc)
        single = self.qmt_api.get("download_history_data") or self.qmt_api.get("down_history_data")
        if single is not None:
            try:
                result = None
                for code in stock_list:
                    result = single(code, period, start_time, end_time)
                return bool(result) if result is not None else True
            except Exception as exc:
                raise RuntimeError("download_history_data2 per-code fallback failed: %s" % exc)
        try:
            return self._handle_market_data_method("download_history_data2", params)
        except (NotImplementedError, AttributeError):
            return False

    def _order_action_from_params(self, params):
        action = str(params.get("action") or "").upper()
        if action:
            return action
        raw = params.get("order_type")
        order_type = str(raw or "").upper()
        if order_type in BUY_ORDER_TYPES:
            return "BUY"
        if order_type in SELL_ORDER_TYPES:
            return "SELL"
        # Credit operations carry their side in the type itself (issue #103).
        # 直接还款 moves cash rather than securities and has no side, so it
        # still needs an explicit action rather than being guessed at.
        credit = _credit_action_of(raw)
        if credit:
            return credit
        if _credit_optype_of(raw) is not None:
            raise ValueError(
                "order_type %s has no implicit buy/sell side; pass action "
                "explicitly" % raw)
        if raw in (None, ""):
            raise ValueError("action or order_type is required")
        # An order_type WAS supplied and was not recognised. Saying "required"
        # here sent a reporter looking at their own call for twenty minutes
        # (issue #92): the real answer is almost always that the package
        # deployed inside QMT predates the type they are using, and a
        # client-side pip upgrade cannot fix that -- this code runs in QMT.
        raise ValueError(
            # ASCII only: this text is written to QMT's own log, which drops
            # non-ASCII characters (a Chinese install path came back mangled).
            "order_type %r is not recognised by the package deployed in QMT "
            "(%s). Credit order types (27-32, and 40-45 special) need 0.3.1 "
            "or newer HERE, in the QMT python directory -- upgrading the "
            "client with pip does not change this file. Run "
            "xt_trader.sync_deployment(), restart the strategy, then check "
            "xtdata.get_deployment_info()." % (raw, self._deployed_version()))

    @staticmethod
    def _deployed_version():
        """Never raises: this only ever runs while building an error message."""
        try:
            from bigqmt_signal_trader.version import __version__

            return __version__
        except Exception:
            return "unknown version"

    def _credit_order_type_from_params(self, params):
        """The MiniQMT order_type to forward, when it is a credit operation."""
        raw = params.get("order_type")
        return raw if _credit_optype_of(raw) is not None else None

    def _handle_submit_order(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        price = params.get("price")
        signal_id = str(params.get("signal_id") or "rpc-%s" % uuid.uuid4().hex)
        order_tag = str(params.get("remark") or params.get("order_remark") or "").strip()
        if not order_tag:
            order_tag = "bqrpc:%s" % signal_id
        request = OrderRequest(
            signal_id=signal_id,
            account_id=self._request_account_id(params),
            action=self._order_action_from_params(params),
            stock_code=str(params.get("stock_code") or ""),
            volume=int(params.get("volume") or params.get("order_volume") or 0),
            price=float(price if price not in (None, "") else 0),
            price_type=params.get("price_type") or "LIMIT",
            strategy_name=str(params.get("strategy_name") or "bigqmt_rpc"),
            remark=order_tag,
            order_type=self._credit_order_type_from_params(params),
        )
        if request.action not in ("BUY", "SELL"):
            raise ValueError("action must be BUY or SELL")
        if not request.stock_code:
            raise ValueError("stock_code is required")
        if request.volume <= 0:
            raise ValueError("volume must be positive")

        try:
            from .exec_events import remember_order_identity

            remember_order_identity(
                getattr(self, "download_job_redis_client", None),
                request.account_id,
                request.remark,
                strategy_name=request.strategy_name,
                stock_code=request.stock_code,
            )
        except Exception:
            pass

        result = self.order_gateway.submit(request)

        # 委托后校验：确认委托是否真的进了系统。passorder 调用成功但委托没进
        # 系统时（静默失败），记录 server_error 让客户端知道。匹配严格按
        # user_order_id(remark) 精确比对，不做 stock_code+action 的模糊兜底。
        # QMT 的委托号是异步分配的（passorder 无返回值），这里按唯一
        # user_order_id(remark) 精确匹配并回填 order_sys_id，避免客户端把
        # 「已提交但暂无委托号」误判为下单失败（issue #38）。
        self._last_server_error = ""

        # Async callers opt out of waiting for the order id. MiniQMT's
        # order_stock_async returns a seq immediately and delivers the id through
        # order_callback, so holding the reply until settlement is exactly the
        # latency the async API exists to avoid (issue #50). The order_callback
        # push already carries order_sys_id, so nothing is lost -- only the
        # post-submit "did it land?" check is skipped, and a silent rejection
        # surfaces as the absence of that push rather than as server_error.
        if not _bool_value(params.get("wait_settlement"), True):
            return result

        if self.settle_orders_inline:
            # Opt-out: block here the way this used to. Kept only for runtimes
            # with no adjust drain to retry on.
            try:
                import time as _time
                _time.sleep(self.order_settle_timeout_seconds)
                self._apply_order_lookup(
                    OrderSettlement(request, result, 0.0), final=True, inline=True)
            except Exception:
                pass
            return result
        # Hand the settlement to the caller rather than raising: handle() stays
        # a plain function for anyone driving handlers directly, and only the
        # service defers its reply.
        self._pending_settlement = OrderSettlement(
            request, result, _monotonic() + self.order_settle_timeout_seconds
        )
        return result

    def take_pending_settlement(self):
        """Pop the settlement the last submit_order registered, if any."""
        settlement = self._pending_settlement
        self._pending_settlement = None
        return settlement

    def _apply_order_lookup(self, settlement, final=False, inline=False):
        """Look the order up by remark. True when settled, False to retry.

        MUST run on the main strategy thread -- get_trade_detail_data returns
        empty anywhere else.
        """
        request = settlement.order_request
        settlement.attempts += 1
        try:
            orders = self.order_gateway.query_orders(request.account_id, "") or []
            by_remark = [
                o for o in orders
                if str(getattr(o, "user_order_id", "") or "").strip() == request.remark.strip()
            ]
            if by_remark:
                sysid = str(getattr(by_remark[0], "order_sys_id", "") or "")
                if sysid:
                    try:
                        settlement.result.order_sys_id = sysid
                    except Exception:
                        pass
                return True
            if not final:
                # Not there yet. QMT assigns the id asynchronously, so an early
                # miss is normal -- only a miss at the deadline is a real one.
                return False
            # Deadline reached with no remark match -> not in the system. Do NOT
            # fall back to matching stock_code+action: order_tag is a unique id
            # we generated, so a miss is always a real miss, while an unrelated
            # order on the same stock and side (a manual one, or an earlier
            # unfilled order) would silently suppress this warning and leave
            # order_sys_id unfilled with no signal at all (issue #41).
            message = (
                "passorder submitted but order not found in system "
                "(stock=%s action=%s price=%.2f volume=%d, %d lookup(s)). "
                "QMT may have silently rejected it (check price range / permissions)."
                % (request.stock_code, request.action, request.price,
                   request.volume, settlement.attempts)
            )
            settlement.server_error = message
            if inline:
                self._last_server_error = message
            return True
        except Exception:
            # A failed lookup must not lose the order -- it is already submitted.
            return True

    def _handle_submit_orders_batch(self, params):
        orders = params.get("orders") or []
        if not isinstance(orders, list) or not orders:
            raise ValueError("orders must be a non-empty list")
        if len(orders) > 500:
            raise ValueError("orders exceeds batch limit 500")
        batch_id = str(params.get("batch_id") or uuid.uuid4().hex)
        account_id = self._request_account_id(params)
        strategy_name = str(
            params.get("strategy_name")
            or (orders[0] or {}).get("strategy_name")
            or "bigqmt_rpc"
        )
        existing_by_tag = {}
        lookup_ok = True
        requires_lookup = any(bool((item or {}).get("require_idempotency_check")) for item in orders)
        if requires_lookup:
            try:
                identity_query = getattr(self.order_gateway, "query_submission_identities_strict", None)
                if callable(identity_query):
                    existing, trades = identity_query(account_id, strategy_name)
                else:
                    query = getattr(self.order_gateway, "query_orders_strict", None)
                    existing = query(account_id, strategy_name) if callable(query) else self.order_gateway.query_orders(account_id, strategy_name)
                    trades = []
                existing_by_tag = {
                    str(getattr(order, "user_order_id", "") or ""): order
                    for order in existing or []
                    if str(getattr(order, "user_order_id", "") or "")
                }
                for trade in trades or []:
                    tag = str(getattr(trade, "user_order_id", "") or "")
                    if tag and tag not in existing_by_tag:
                        existing_by_tag[tag] = trade
            except Exception:
                lookup_ok = False
        results = []
        for index, item in enumerate(orders):
            item = dict(item or {})
            order_tag = str(item.get("order_remark") or item.get("remark") or item.get("signal_id") or "")
            if not order_tag:
                results.append({
                    "index": index,
                    "batch_id": batch_id,
                    "success": False,
                    "accepted": False,
                    "explicit_failure": True,
                    "code": -3,
                    "error": "ORDER_TAG_REQUIRED",
                    "user_order_id": "",
                })
                continue
            known = existing_by_tag.get(order_tag)
            journal_key = (account_id, strategy_name, order_tag)
            journal = self._submit_journal.get(journal_key)
            if known is not None or journal is not None:
                results.append({
                    "index": index,
                    "batch_id": batch_id,
                    "success": True,
                    "accepted": True,
                    "confirmed": known is not None,
                    "idempotent": True,
                    "code": 0,
                    "order_sys_id": str(getattr(known, "order_sys_id", "") or (journal or {}).get("order_sys_id") or ""),
                    "user_order_id": order_tag,
                })
                continue
            if bool(item.get("require_idempotency_check")) and not lookup_ok:
                results.append({
                    "index": index,
                    "batch_id": batch_id,
                    "success": False,
                    "accepted": False,
                    "explicit_failure": False,
                    "code": -2,
                    "error": "IDEMPOTENCY_CHECK_UNAVAILABLE",
                    "user_order_id": order_tag,
                })
                continue
            try:
                result = self._handle_submit_order(item)
                response = {
                    "index": index,
                    "batch_id": batch_id,
                    "success": True,
                    "accepted": True,
                    "confirmed": False,
                    "idempotent": False,
                    "code": 0,
                    "order_sys_id": str(getattr(result, "order_sys_id", None) or ""),
                    "user_order_id": str(getattr(result, "user_order_id", None) or ""),
                }
                if order_tag:
                    self._submit_journal[journal_key] = dict(response)
                results.append(response)
            except Exception as exc:
                results.append({
                    "index": index,
                    "batch_id": batch_id,
                    "success": False,
                    "accepted": False,
                    "explicit_failure": True,
                    "code": -1,
                    "error": "%s: %s" % (exc.__class__.__name__, exc),
                    "user_order_id": order_tag,
                })
        return results

    def _handle_cancel_order(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        order_sys_id = str(params.get("order_sys_id") or params.get("order_sysid") or params.get("order_id") or "")
        if not order_sys_id:
            raise ValueError("order_sys_id or order_id is required")
        return self.order_gateway.cancel(
            OrderRef(order_sys_id=order_sys_id, user_order_id=str(params.get("user_order_id") or ""))
        )


def _bool_value(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _credit_action_of(order_type):
    try:
        from bigqmt_signal_trader.adapters.order_bigqmt import credit_action_of

        return credit_action_of(order_type)
    except Exception:
        return None


def _credit_optype_of(order_type):
    try:
        from bigqmt_signal_trader.adapters.order_bigqmt import credit_optype_of

        return credit_optype_of(order_type)
    except Exception:
        return None


def _deployed_version():
    """Version of the bridge actually running here, or "" if unknown."""
    try:
        from bigqmt_signal_trader.version import __version__

        return __version__
    except Exception:
        return ""


def _normalize_mapping_value(value):
    """Make one mapping value JSON-safe without flattening its shape."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return dict((str(k), _normalize_mapping_value(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [_normalize_mapping_value(v) for v in value]
    rows = _normalize_detail_rows([value])
    return rows[0] if rows else {}


def _normalize_detail_rows(rows):
    """Convert get_trade_detail_data row objects into JSON-serializable dicts.

    QMT returns objects with m_strXxx / m_nXxx / m_dXxx attributes. We map
    each to its public attributes so the result survives JSON encoding.
    """
    if not rows:
        return []
    result = []
    for row in rows:
        if isinstance(row, dict):
            result.append(row)
            continue
        item = {}
        for name in dir(row):
            if name.startswith("_"):
                continue
            try:
                value = getattr(row, name)
            except Exception:
                continue
            if callable(value):
                continue
            item[name] = value
        result.append(item)
    return result


def encode_rpc_request_payload(request):
    """Encode request JSON so patched QMT Redis clients do not inspect stock-code text."""

    raw = json.dumps(request, ensure_ascii=False).encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii").translate(SAFE_B64_DIGIT_ENCODE)
    return SAFE_B64_PREFIX + encoded


def decode_rpc_request_payload(text):
    text = str(text)
    if not text.startswith(SAFE_B64_PREFIX):
        return text
    encoded = text[len(SAFE_B64_PREFIX):].translate(SAFE_B64_DIGIT_DECODE)
    return base64.b64decode(encoded.encode("ascii")).decode("utf-8")


class RedisPubSubRpcService:
    """Receive RPC requests from Redis and write responses back to Redis."""

    def __init__(
        self,
        redis_client,
        handlers,
        account_id="",
        response_redis_client=None,
        request_channel_template="bigqmt:rpc:req:{account_id}",
        request_queue_template="bigqmt:rpc:queue:{account_id}",
        response_channel_template="bigqmt:rpc:resp:{account_id}:{request_id}",
        response_list_template="bigqmt:rpc:respq:{account_id}:{request_id}",
        response_key_template="bigqmt:rpc:resp:{account_id}:{request_id}",
        response_ttl_seconds=60,
        max_queue_size=200,
        process_in_listener=False,
        listener_methods=None,
        background_threads=True,
        queue_poll_interval_seconds=0.02,
        debug_log_limit=0,
        print_prefix="[bigqmt_rpc]",
        transport=None,
    ):
        self.listen_redis = redis_client
        self.redis = response_redis_client or redis_client
        self.handlers = handlers
        self.account_id = str(account_id or "")
        self.request_channel_template = request_channel_template
        self.request_queue_template = request_queue_template
        self.response_channel_template = response_channel_template
        self.response_list_template = response_list_template
        self.response_key_template = response_key_template
        self.response_ttl_seconds = int(response_ttl_seconds)
        self.process_in_listener = bool(process_in_listener)
        self.background_threads = bool(background_threads)
        if listener_methods is None:
            listener_methods = ("ping",)
        self.listener_methods = self._expand_listener_methods(listener_methods)
        self.queue_poll_interval_seconds = max(0.001, float(queue_poll_interval_seconds))
        self.debug_log_limit = int(debug_log_limit)
        self._received_count = 0
        self._processed_count = 0
        self._published_count = 0
        self._deferred_count = 0
        self.print_prefix = print_prefix
        self.pending = queue.Queue(maxsize=int(max_queue_size))
        # Orders whose reply is waiting on QMT assigning an order id. Unbounded
        # on purpose: every entry is an order that already reached the broker,
        # so dropping one would strand a live order with no reply.
        self._pending_settlements = queue.Queue()
        self._running = threading.Event()
        self._thread = None
        self._queue_thread = None
        self._pubsub = None
        # Transport owns the wire. Default to a RedisTransport built from the
        # same clients/templates so behavior is unchanged. An explicit
        # ``transport`` (e.g. ZmqTransport) overrides the Redis path entirely.
        if transport is None:
            from .transports.redis_transport import RedisTransport

            transport = RedisTransport(
                redis_client,
                account_id=self.account_id,
                response_redis_client=response_redis_client,
                request_channel_template=request_channel_template,
                request_queue_template=request_queue_template,
                response_channel_template=response_channel_template,
                response_list_template=response_list_template,
                response_key_template=response_key_template,
                response_ttl_seconds=response_ttl_seconds,
                queue_poll_interval_seconds=queue_poll_interval_seconds,
                debug_log_limit=debug_log_limit,
                print_prefix=print_prefix,
            )
        self._transport = transport
        # Route inbound raw payloads through the service's dispatch (which
        # applies the inline-vs-deferred fork) instead of transport.deliver().
        self._transport.on_raw_payload = self._handle_received_payload

    @property
    def request_channel(self):
        return self.request_channel_template.format(account_id=self.account_id)

    @property
    def request_queue(self):
        return self.request_queue_template.format(account_id=self.account_id)

    def start(self):
        self._running.set()
        # Delegate thread lifecycle to the transport. The transport invokes the
        # on_request callback with a decoded request dict; enqueue_payload routes
        # it through the inline-vs-deferred fork and publishes the response
        # itself (returns None so the transport's deliver() does not double-send).
        # RedisTransport additionally routes raw bytes through on_raw_payload
        # (set in __init__) for its own receive loops.
        self._transport.start_receiving(
            self.enqueue_payload,
            background_threads=self.background_threads,
        )
        # Mirror transport threads onto the service for stop()/diagnostics.
        self._thread = getattr(self._transport, "_thread", None)
        self._queue_thread = getattr(self._transport, "_queue_thread", None)
        if not self.background_threads:
            print("%s started queue=%s background_threads=False" % (self.print_prefix, self.request_queue))
            return
        print("%s started channel=%s queue=%s" % (self.print_prefix, self.request_channel, self.request_queue))

    def stop(self):
        self._running.clear()
        try:
            self._transport.stop()
        except Exception:
            pass
        # The transport owns the threads now; keep the attributes for back-compat.
        self._thread = None
        self._queue_thread = None
        self._pubsub = None

    def _listen_loop(self):
        while self._running.is_set():
            try:
                pubsub = self.listen_redis.pubsub(ignore_subscribe_messages=True)
                self._pubsub = pubsub
                pubsub.subscribe(self.request_channel)
                if self.debug_log_limit > 0:
                    print("%s subscribed channel=%s" % (self.print_prefix, self.request_channel))
                while self._running.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if not self._running.is_set():
                        break
                    if not message or message.get("type") != "message":
                        continue
                    self._handle_received_payload(message.get("data"), "pubsub")
            except Exception:
                print("%s listener failed:\n%s" % (self.print_prefix, traceback.format_exc()))
                time.sleep(1.0)
            finally:
                try:
                    if self._pubsub is not None:
                        self._pubsub.close()
                except Exception:
                    pass
                self._pubsub = None

    def _queue_loop(self):
        while self._running.is_set():
            try:
                if self.debug_log_limit > 0:
                    print("%s queue polling key=%s" % (self.print_prefix, self.request_queue))
                while self._running.is_set():
                    # brpop blocks server-side until an item arrives (or the
                    # short timeout fires), so a request is picked up within
                    # ~1ms of being pushed instead of waiting up to
                    # queue_poll_interval_seconds. The 1s ceiling lets us
                    # re-check _running for a clean shutdown.
                    item = self.listen_redis.brpop(self.request_queue, timeout=1)
                    if not self._running.is_set():
                        break
                    if not item:
                        continue
                    raw = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
                    self._handle_received_payload(raw, "queue")
            except Exception:
                print("%s queue listener failed:\n%s" % (self.print_prefix, traceback.format_exc()))
                time.sleep(1.0)

    def _handle_received_payload(self, raw_payload, source):
        self._received_count += 1
        if self._received_count <= self.debug_log_limit:
            try:
                preview = self._loads(raw_payload)
                method = str(preview.get("method") or "")
                print(
                    "%s received source=%s method=%s inline=%s"
                    % (self.print_prefix, source, method, self._should_process_in_listener(preview))
                )
                self.enqueue_payload(preview)
                return
            except Exception:
                print("%s receive preview failed:\n%s" % (self.print_prefix, traceback.format_exc()))
        self.enqueue_payload(raw_payload)

    def enqueue_payload(self, raw_payload):
        payload = self._loads(raw_payload)
        if self._should_process_in_listener(payload):
            self.process_request(payload)
            return
        self._deferred_count += 1
        if self._deferred_count <= self.debug_log_limit:
            print(
                "%s deferred method=%s pending_before=%s"
                % (self.print_prefix, payload.get("method"), self.pending.qsize())
            )
        try:
            self.pending.put_nowait(payload)
        except queue.Full:
            # A full pending queue (client polling storm) must not raise into the
            # adjust thread — QMT stops the strategy on a callback raise. Drop the
            # oldest request and keep the newest instead of crashing.
            try:
                self.pending.get_nowait()
            except Exception:
                pass
            try:
                self.pending.put_nowait(payload)
            except Exception:
                pass

    def _should_process_in_listener(self, payload):
        if not self.process_in_listener:
            return False
        method = str((payload or {}).get("method") or "")
        if method in self.listener_methods:
            return True
        canonical = getattr(self.handlers, "_canonical_method", lambda value: value)(method)
        return canonical in self.listener_methods

    def _expand_listener_methods(self, listener_methods):
        methods = set()
        for method in listener_methods or ():
            method = str(method)
            if method in ("*", "all", "read", "readonly"):
                methods.update(READ_METHODS - LISTENER_DEFERRED_METHODS)
            else:
                methods.add(method)
                canonical = getattr(self.handlers, "_canonical_method", lambda value: value)(method)
                methods.add(canonical)
        return methods

    def _loads(self, raw_payload):
        if isinstance(raw_payload, dict):
            return dict(raw_payload)
        text = decode_text(raw_payload)
        text = decode_rpc_request_payload(text)
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("rpc payload must be a json object")
        return payload

    def settle_pending_orders(self, max_items=100):
        """Retry parked order lookups. MUST be called from the adjust thread.

        A queue rather than a list because rpc_listener_methods is configurable:
        if submit_order is ever put in it, the producer becomes the listener
        thread while this consumer stays on adjust.

        Unsettled entries go back on the queue, so each order costs one lookup
        per adjust tick until it resolves or its deadline passes.
        """
        settled = 0
        # Snapshot the size first. Unsettled entries go back on the same queue,
        # so draining until empty would keep re-picking them and spin one adjust
        # tick into many lookups per order.
        batch = min(int(max_items), self._pending_settlements.qsize())
        for _ in range(batch):
            try:
                settlement = self._pending_settlements.get_nowait()
            except queue.Empty:
                break
            expired = _monotonic() >= settlement.deadline
            try:
                done = self.handlers._apply_order_lookup(settlement, final=expired)
            except Exception:
                done = True  # never strand a submitted order in the queue
            if not done:
                self._pending_settlements.put(settlement)
                continue
            response = settlement.response
            response["data"] = to_jsonable(settlement.result)
            response["ok"] = True
            if settlement.server_error:
                response["server_error"] = settlement.server_error
            response["handled_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                self._publish_response(settlement.request, response)
            except Exception:
                pass
            settled += 1
        return settled

    def pending_settlement_count(self):
        return self._pending_settlements.qsize()

    def drain_pending(self, max_items=20):
        # Settle carry-overs from earlier ticks before taking on new work.
        self.settle_pending_orders()
        processed = 0
        for _ in range(int(max_items)):
            try:
                request = self.pending.get_nowait()
            except queue.Empty:
                break
            if self._processed_count < self.debug_log_limit:
                print(
                    "%s draining method=%s pending_after=%s"
                    % (self.print_prefix, request.get("method"), self.pending.qsize())
                )
            self.process_request(request)
            processed += 1
        # Settle again so an order submitted in THIS drain still replies on this
        # tick. One lookup, no sleep -- if QMT has not assigned the id yet we
        # simply retry next tick rather than holding the thread (issue #44).
        self.settle_pending_orders()
        return processed

    def drain_request_queue(self, max_items=20):
        # Delegate to the transport when it owns the wire directly; for Redis
        # the transport's drain drives _handle_received_payload (which honors
        # the inline-vs-deferred fork), matching the original semantics.
        transport_drain = getattr(self._transport, "drain_request_queue", None)
        if transport_drain is not None and not isinstance(self._transport, type(None)):
            return transport_drain(max_items=max_items)
        processed = 0
        for _ in range(int(max_items)):
            item = self.listen_redis.lpop(self.request_queue)
            if not item:
                break
            self.process_request(self._loads(item))
            processed += 1
        return processed

    def process_request(self, request):
        request = dict(request or {})
        request_id = str(request.get("request_id") or request.get("id") or uuid.uuid4().hex)
        account_id = str(request.get("account_id") or self.account_id or "")
        method = str(request.get("method") or "")
        response = {
            "schema_version": 1,
            "request_id": request_id,
            "account_id": account_id,
            "method": method,
            "ok": False,
            "data": None,
            "error": "",
            # server_error carries QMT-side diagnostic info (e.g. passorder
            # submitted but order not found in system, get_trade_detail_data
            # returned empty) that doesn't raise an exception but indicates a
            # problem. Lets clients see why an operation silently failed.
            "server_error": "",
            "handled_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            if self.account_id and account_id and account_id != self.account_id:
                raise PermissionError("account_id mismatch")
            result = self.handlers.handle(method, request.get("params") or {})
            response["data"] = to_jsonable(result)
            response["ok"] = True
            # Surface server-side diagnostics when the handler recorded one.
            server_error = getattr(self.handlers, "_last_server_error", None)
            if server_error:
                response["server_error"] = str(server_error)
            # passorder already ran, but QMT assigns the order id asynchronously.
            # Park the reply instead of sleeping on this thread; a later adjust
            # tick settles and publishes it (issue #44).
            take = getattr(self.handlers, "take_pending_settlement", None)
            settlement = take() if callable(take) else None
            if settlement is not None:
                settlement.request = request
                settlement.response = response
                self._pending_settlements.put(settlement)
                self._deferred_count += 1
                return response
        except Exception as exc:
            response["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        try:
            self._publish_response(request, response)
        except Exception:
            # A response-publish failure (e.g. redis outage) must not propagate
            # to the adjust thread — QMT stops the strategy on a callback raise.
            # The request already ran; the client will just see a timeout.
            import traceback as _tb
            try:
                from .logging_setup import get_logger
                get_logger("rpc").error(
                    "publish response failed method=%s:\n%s", method, _tb.format_exc()
                )
            except Exception:
                pass
        self._processed_count += 1
        if self._processed_count <= self.debug_log_limit:
            print("%s responded method=%s ok=%s" % (self.print_prefix, method, response["ok"]))
        return response

    def _format_response_target(self, template, account_id, request_id):
        if not template:
            return ""
        return template.format(account_id=account_id, request_id=request_id)

    def _publish_response(self, request, response):
        # Delegate to the transport (RedisTransport fans out to key/list/channel;
        # ZMQ/MySQL transports use their native reply path).
        self._transport.send_response(request, response)
        self._published_count = getattr(self._transport, "_published_count", self._published_count)

    def _response_clients(self):
        clients = [self.redis]
        if self.listen_redis is not self.redis:
            clients.append(self.listen_redis)
        return clients

    def _write_response_key(self, response_key, ttl_seconds, payload):
        first_error = None
        wrote = 0
        for client in self._response_clients():
            try:
                if ttl_seconds > 0:
                    client.setex(response_key, ttl_seconds, payload)
                else:
                    client.set(response_key, payload)
                wrote += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if wrote <= 0 and first_error is not None:
            raise first_error
        return wrote

    def _push_response_list(self, response_list, ttl_seconds, payload):
        first_error = None
        pushed = 0
        for client in self._response_clients():
            try:
                client.rpush(response_list, payload)
                if ttl_seconds > 0:
                    client.expire(response_list, ttl_seconds)
                pushed += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if pushed <= 0 and first_error is not None:
            raise first_error
        return pushed

    def _publish_response_channel(self, response_channel, payload):
        first_error = None
        receivers = 0
        published = 0
        for client in self._response_clients():
            try:
                receivers += int(client.publish(response_channel, payload) or 0)
                published += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if published <= 0 and first_error is not None:
            raise first_error
        self._published_count += 1
        if self._published_count <= self.debug_log_limit:
            print("%s published response receivers=%s" % (self.print_prefix, receivers))
        return receivers


def call_redis_rpc(
    redis_client,
    account_id,
    method,
    params=None,
    request_channel_template="bigqmt:rpc:req:{account_id}",
    request_queue_template="bigqmt:rpc:queue:{account_id}",
    response_channel_template="bigqmt:rpc:resp:{account_id}:{request_id}",
    response_list_template="bigqmt:rpc:respq:{account_id}:{request_id}",
    response_key_template="bigqmt:rpc:resp:{account_id}:{request_id}",
    timeout_seconds=3.0,
    ttl_seconds=60,
    transport="queue",
):
    """Small external client helper for tests and admin scripts."""

    request_id = uuid.uuid4().hex
    request_channel = request_channel_template.format(account_id=account_id)
    request_queue = request_queue_template.format(account_id=account_id)
    response_channel = response_channel_template.format(account_id=account_id, request_id=request_id)
    response_list = response_list_template.format(account_id=account_id, request_id=request_id)
    response_key = response_key_template.format(account_id=account_id, request_id=request_id)
    request = {
        "schema_version": 1,
        "request_id": request_id,
        "account_id": account_id,
        "method": method,
        "params": params or {},
        "reply_channel": response_channel,
        "reply_list": response_list,
        "reply_key": response_key,
        "ttl_seconds": ttl_seconds,
    }
    payload = encode_rpc_request_payload(request)
    if str(transport or "queue").lower() in ("queue", "list", "blpop"):
        redis_client.rpush(request_queue, payload)
        redis_client.expire(request_queue, max(60, int(ttl_seconds)))
        deadline = time.time() + float(timeout_seconds)
        while True:
            raw_response = redis_client.get(response_key)
            if raw_response:
                return json.loads(decode_text(raw_response))
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            wait_timeout = max(1, int(min(remaining, 1.0) + 0.999))
            try:
                item = redis_client.blpop(response_list, timeout=wait_timeout)
            except Exception as exc:
                if _is_redis_timeout(exc):
                    continue
                raise
            if item:
                raw_response = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
                try:
                    redis_client.delete(response_list)
                except Exception:
                    pass
                return json.loads(decode_text(raw_response))
        raw_response = redis_client.get(response_key)
        if raw_response:
            return json.loads(decode_text(raw_response))
        raise TimeoutError(
            "redis rpc timeout: %s account_id=%s request_queue=%s" % (method, account_id, request_queue)
        )

    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    try:
        pubsub.subscribe(response_channel)
        redis_client.publish(request_channel, payload)
        deadline = time.time() + float(timeout_seconds)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            message = pubsub.get_message(timeout=remaining)
            if not message or message.get("type") != "message":
                continue
            response = json.loads(decode_text(message.get("data")))
            if response.get("request_id") == request_id:
                return response
        raw_response = redis_client.get(response_key)
        if raw_response:
            return json.loads(decode_text(raw_response))
        raise TimeoutError("redis rpc timeout: %s" % method)
    finally:
        try:
            pubsub.close()
        except Exception:
            pass
