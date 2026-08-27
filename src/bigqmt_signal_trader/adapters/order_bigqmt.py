"""Big QMT order gateway.

The passorder signature follows src/api/qmt_jq_trade.
"""

import hashlib

from ..code_utils import normalize_stock_code
from ..exec_events import date_time_seconds
from ..models import CancelResult, OrderSnapshot, OrderSubmitResult, SignalAction, TradeSnapshot
from .position_bigqmt import _attr, _full_code, skip_unparsable_row


PRICE_TYPE_ALIASES = {
    "LIMIT": 11,
    "FIX_PRICE": 11,
    "LATEST_PRICE": 5,
    "MARKET_PEER_PRICE_FIRST": 44,
    "MARKET_SH_CONVERT_5_LIMIT": 43,
    "MARKET_SZ_CONVERT_5_CANCEL": 47,
}

ORDER_TYPE_ACTIONS = {
    23: "BUY",
    24: "SELL",
    27: "BUY",
    31: "SELL",
    32: "REPAY",
    33: "BUY",
    34: "SELL",
}


def _action_from_offset_flag(offset_flag):
    return SignalAction.BUY.value if int(offset_flag or 0) == 48 else SignalAction.SELL.value


# 报单时间。大 QMT 的 ORDER 行把日期和时间分成两个字段, MiniQMT 的
# XtOrder.order_time 是 Unix 秒, 所以要拼接后转换。成交那条路径早就读了
# m_strTradeTime, 委托这边一直漏掉 (issue #48)。
_ORDER_DATE_FIELDS = ("m_strInsertDate", "m_strOrderDate", "insert_date", "order_date")
_ORDER_TIME_FIELDS = ("m_strInsertTime", "m_strOrderTime", "insert_time", "order_time")

# 取不到时打印该行实际有哪些 m_*, 每进程一次。字段名无法离线核实,
# 猜一个然后静默返回 0 正是订单方向那个 bug 的成因。
_missing_order_time_reported = []


def _report_missing_order_time(row):
    if _missing_order_time_reported:
        return
    _missing_order_time_reported.append(True)
    try:
        available = sorted(n for n in dir(row) if n.startswith("m_"))
    except Exception:
        available = []
    print(
        "[bigqmt_order] order_time not found (tried %s / %s); ORDER row exposes: %s"
        % (", ".join(_ORDER_DATE_FIELDS), ", ".join(_ORDER_TIME_FIELDS),
           ", ".join(available) or "<none>")
    )


# 委托状态描述。官方字段表 (docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md):
#   m_strCancelInfo  废单原因      <- 状态 57 时柜台的拒单理由在这里
#   m_strErrorMsg    状态信息
# 柜台消息形如 "[COUNTER] 资金可用余额不足，尚需[4789.630]"; 两个字段都空过,
# 客户端就只能看到一个没有原因的失败 (issue #60)。废单原因优先, 它更具体。
_STATUS_MSG_FIELDS = (
    "m_strCancelInfo",
    "m_strErrorMsg",
    "m_strStatusMsg",
    "status_msg",
    "error_msg",
)


def _status_message(row):
    for name in _STATUS_MSG_FIELDS:
        value = _attr(row, (name,))
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _order_time_seconds(row):
    """把 ORDER 行的报单日期+时间转成 Unix 秒, 拿不到返回 0。

    容忍几种实际会遇到的写法: 日期 '20260819' 或 '2026-08-19',
    时间 '093015'、'09:30:15' 或 '09:30:15.123'。已经是数字时间戳的直接用
    (毫秒会被归一到秒)。
    """
    raw_time = _attr(row, _ORDER_TIME_FIELDS)
    raw_date = _attr(row, _ORDER_DATE_FIELDS)
    if raw_time is None and raw_date is None:
        _report_missing_order_time(row)
        return 0

    # 已是数字: 当成时间戳 (>1e11 视为毫秒)。
    if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
        value = float(raw_time)
        if value > 1e11:
            value /= 1000.0
        if value > 1e8:      # 像时间戳而不是 093015 这种时分秒
            return int(value)

    date_text = "".join(ch for ch in str(raw_date or "") if ch.isdigit())
    time_text = "".join(ch for ch in str(raw_time or "") if ch.isdigit())
    if not date_text or len(date_text) < 8:
        return 0
    time_text = (time_text + "000000")[:6]   # 补齐到 HHMMSS, 丢掉毫秒
    try:
        import time as _time

        parsed = _time.strptime(date_text[:8] + time_text, "%Y%m%d%H%M%S")
        return int(_time.mktime(parsed))
    except Exception:
        return 0


def _price_type_value(value, default):
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        text = str(value).strip().upper()
        return int(PRICE_TYPE_ALIASES.get(text, default))


class BigQmtOrderGateway:
    def __init__(
        self,
        context_info,
        account_id="",
        passorder_func=None,
        cancel_func=None,
        get_trade_detail_data_func=None,
        account_type="STOCK",
        combo_type=1101,
        price_type=11,
        quick_trade=2,
    ):
        self.context_info = context_info
        self.account_id = account_id
        self.passorder = passorder_func
        self.cancel_func = cancel_func
        self.get_trade_detail_data = get_trade_detail_data_func
        self.account_type = account_type
        self.combo_type = combo_type
        self.price_type = price_type
        self.quick_trade = quick_trade

    def _require_passorder(self):
        if self.passorder is None:
            raise RuntimeError("passorder is not available in Big QMT runtime")
        return self.passorder

    def _require_cancel(self):
        if self.cancel_func is None:
            raise RuntimeError("cancel is not available in Big QMT runtime")
        return self.cancel_func

    def _require_query_func(self):
        if self.get_trade_detail_data is None:
            raise RuntimeError("get_trade_detail_data is not available in Big QMT runtime")
        return self.get_trade_detail_data

    @staticmethod
    def build_user_order_id(signal_id):
        text = str(signal_id or "")
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        return "bq:%s:%s" % (digest, text[:30])

    def submit(self, request):
        passorder = self._require_passorder()
        action = str(request.action).upper()
        explicit_order_type = getattr(request, "order_type", None)
        if explicit_order_type is not None:
            op_type = int(explicit_order_type)
            if ORDER_TYPE_ACTIONS.get(op_type) != action:
                raise ValueError("unsupported order_type or action mismatch: %s/%s" % (op_type, action))
        elif action == SignalAction.BUY.value:
            op_type = 23
        elif action == SignalAction.SELL.value:
            op_type = 24
        else:
            raise ValueError("unsupported order action: %s" % request.action)

        user_order_id = str(request.remark or "").strip() or self.build_user_order_id(request.signal_id)
        account_id = request.account_id or self.account_id
        passorder(
            op_type,
            self.combo_type,
            account_id,
            normalize_stock_code(request.stock_code),
            _price_type_value(request.price_type, self.price_type),
            float(request.price),
            int(request.volume),
            request.strategy_name,
            self.quick_trade,
            user_order_id,
            self.context_info,
        )
        return OrderSubmitResult(
            status="SUBMITTED",
            user_order_id=user_order_id,
            order_sys_id=None,
            message="passorder submitted",
        )

    def cancel(self, order_ref):
        cancel_func = self._require_cancel()
        ok = cancel_func(order_ref.order_sys_id, self.account_id, self.account_type, self.context_info)
        return CancelResult(success=bool(ok), message="" if ok else "cancel returned false")

    def query_orders(self, account_id, strategy_name):
        try:
            return self.query_orders_strict(account_id, strategy_name)
        except Exception:
            return []

    def query_orders_strict(self, account_id, strategy_name):
        query = self._require_query_func()
        rows = query(account_id, self.account_type, "ORDER", strategy_name) or []
        result = []
        for row in rows:
            try:
                stock_code = _full_code(
                    _attr(row, ("m_strInstrumentID", "instrument_id", "stock_code")),
                    _attr(row, ("m_strExchangeID", "exchange_id", "market")),
                )
                order_type = int(
                    _attr(row, ("m_nOpType", "op_type", "order_type"), 0) or 0
                )
                action = ORDER_TYPE_ACTIONS.get(
                    order_type,
                    _action_from_offset_flag(
                        _attr(row, ("m_nOffsetFlag", "offset_flag"), 0)
                    ),
                )
            except Exception as exc:
                skip_unparsable_row("ORDER", row, exc)
                continue
            result.append(
                OrderSnapshot(
                    order_sys_id=str(_attr(row, ("m_strOrderSysID", "order_sys_id"), "") or ""),
                    user_order_id=str(_attr(row, ("m_strRemark", "user_order_id", "remark"), "") or ""),
                    stock_code=stock_code,
                    action=action,
                    volume=int(_attr(row, ("m_nVolumeTotalOriginal", "volume"), 0) or 0),
                    traded_volume=int(_attr(row, ("m_nVolumeTraded", "traded_volume"), 0) or 0),
                    status=str(_attr(row, ("m_nOrderStatus", "status"), "") or ""),
                    price=float(_attr(row, ("m_dLimitPrice", "m_dPrice", "price"), 0.0) or 0.0),
                    strategy_name=str(_attr(row, ("m_strStrategyName", "strategy_name"), "") or ""),
                    remark=str(_attr(row, ("m_strRemark", "remark"), "") or ""),
                    order_time=_order_time_seconds(row),
                    status_msg=_status_message(row),
                    traded_price=float(
                        _attr(row, ("m_dTradedPrice", "traded_price", "avg_traded_price"), 0.0) or 0.0
                    ),
                    order_type=order_type or None,
                )
            )
        return result

    def query_trades(self, account_id, strategy_name):
        try:
            return self.query_trades_strict(account_id, strategy_name)
        except Exception:
            return []

    def query_trades_strict(self, account_id, strategy_name):
        query = self._require_query_func()
        rows = []
        last_error = None
        for detail_type in ("DEAL", "TRADE"):
            try:
                if str(strategy_name or "").strip():
                    rows = query(account_id, self.account_type, detail_type, strategy_name) or []
                else:
                    rows = query(account_id, self.account_type, detail_type) or []
                if rows:
                    break
            except Exception as exc:
                last_error = exc
        if not rows and last_error is not None:
            raise last_error
        result = []
        for row in rows:
            traded_at_raw = _attr(row, ("m_strTradeTime", "trade_time", "traded_at"), "")
            try:
                stock_code = _full_code(
                    _attr(row, ("m_strInstrumentID", "instrument_id", "stock_code")),
                    _attr(row, ("m_strExchangeID", "exchange_id", "market")),
                )
            except Exception as exc:
                skip_unparsable_row("DEAL", row, exc)
                continue
            result.append(
                TradeSnapshot(
                    trade_id=str(_attr(row, ("m_strTradeID", "trade_id"), "") or ""),
                    order_sys_id=str(_attr(row, ("m_strOrderSysID", "order_sys_id"), "") or ""),
                    stock_code=stock_code,
                    action=_action_from_offset_flag(_attr(row, ("m_nOffsetFlag", "offset_flag"), 0)),
                    volume=int(_attr(row, ("m_nVolume", "volume"), 0) or 0),
                    price=float(_attr(row, ("m_dPrice", "m_dTradePrice", "price"), 0.0) or 0.0),
                    traded_at=str(traded_at_raw or ""),
                    user_order_id=str(_attr(row, ("m_strRemark", "user_order_id", "remark"), "") or ""),
                    # 官方 Deal 字段: m_dTradeAmount 成交额; m_strTradeDate+
                    # m_strTradeTime 合成 Unix 秒; 策略名来自查询过滤参数。
                    amount=float(_attr(row, ("m_dTradeAmount", "amount"), 0.0) or 0.0),
                    strategy_name=str(strategy_name or ""),
                    traded_time=date_time_seconds(
                        _attr(row, ("m_strTradeDate", "trade_date", "m_strDealDate")),
                        traded_at_raw,
                    ),
                )
            )
        return result

    def query_submission_identities_strict(self, account_id, strategy_name):
        orders = self.query_orders_strict(account_id, strategy_name)
        trades = self.query_trades_strict(account_id, strategy_name)
        return orders, trades
