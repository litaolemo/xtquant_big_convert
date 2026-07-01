"""Big QMT order gateway.

The passorder signature follows src/api/qmt_jq_trade.
"""

import hashlib

from ..code_utils import normalize_stock_code
from ..models import CancelResult, OrderSnapshot, OrderSubmitResult, SignalAction, TradeSnapshot
from .position_bigqmt import _attr, _full_code


PRICE_TYPE_ALIASES = {
    "LIMIT": 11,
    "FIX_PRICE": 11,
    "LATEST_PRICE": 5,
    "MARKET_PEER_PRICE_FIRST": 44,
    "MARKET_SH_CONVERT_5_LIMIT": 43,
    "MARKET_SZ_CONVERT_5_CANCEL": 47,
}


def _action_from_offset_flag(offset_flag):
    return SignalAction.BUY.value if int(offset_flag or 0) == 48 else SignalAction.SELL.value


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
        if action == SignalAction.BUY.value:
            op_type = 23
        elif action == SignalAction.SELL.value:
            op_type = 24
        else:
            raise ValueError("unsupported order action: %s" % request.action)

        user_order_id = self.build_user_order_id(request.signal_id)
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
        query = self._require_query_func()
        rows = query(account_id, self.account_type, "ORDER", strategy_name) or []
        result = []
        for row in rows:
            result.append(
                OrderSnapshot(
                    order_sys_id=str(_attr(row, ("m_strOrderSysID", "order_sys_id"), "") or ""),
                    user_order_id=str(_attr(row, ("m_strRemark", "user_order_id", "remark"), "") or ""),
                    stock_code=_full_code(
                        _attr(row, ("m_strInstrumentID", "instrument_id", "stock_code")),
                        _attr(row, ("m_strExchangeID", "exchange_id", "market")),
                    ),
                    action=_action_from_offset_flag(_attr(row, ("m_nOffsetFlag", "offset_flag"), 0)),
                    volume=int(_attr(row, ("m_nVolumeTotalOriginal", "volume"), 0) or 0),
                    traded_volume=int(_attr(row, ("m_nVolumeTraded", "traded_volume"), 0) or 0),
                    status=str(_attr(row, ("m_nOrderStatus", "status"), "") or ""),
                    price=float(_attr(row, ("m_dLimitPrice", "m_dPrice", "price"), 0.0) or 0.0),
                    strategy_name=str(_attr(row, ("m_strStrategyName", "strategy_name"), "") or ""),
                    remark=str(_attr(row, ("m_strRemark", "remark"), "") or ""),
                )
            )
        return result

    def query_trades(self, account_id, strategy_name):
        query = self._require_query_func()
        rows = []
        for detail_type in ("DEAL", "TRADE"):
            try:
                rows = query(account_id, self.account_type, detail_type, strategy_name) or []
                if rows:
                    break
            except Exception:
                rows = []
        result = []
        for row in rows:
            result.append(
                TradeSnapshot(
                    trade_id=str(_attr(row, ("m_strTradeID", "trade_id"), "") or ""),
                    order_sys_id=str(_attr(row, ("m_strOrderSysID", "order_sys_id"), "") or ""),
                    stock_code=_full_code(
                        _attr(row, ("m_strInstrumentID", "instrument_id", "stock_code")),
                        _attr(row, ("m_strExchangeID", "exchange_id", "market")),
                    ),
                    action=_action_from_offset_flag(_attr(row, ("m_nOffsetFlag", "offset_flag"), 0)),
                    volume=int(_attr(row, ("m_nVolume", "volume"), 0) or 0),
                    price=float(_attr(row, ("m_dPrice", "m_dTradePrice", "price"), 0.0) or 0.0),
                    traded_at=str(_attr(row, ("m_strTradeTime", "trade_time", "traded_at"), "") or ""),
                )
            )
        return result
