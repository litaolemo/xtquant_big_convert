import json
import time
import threading
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.exec_events import (
    enrich_order_identity,
    format_raw_snapshot,
    normalize_cancel_error_event,
    normalize_order_error_event,
    normalize_order_event,
    remember_order_identity,
    normalize_trade_event,
    order_channel,
    order_error_channel,
    cancel_error_channel,
    publish_order_event,
    publish_trade_event,
    raw_field_snapshot,
    trade_channel,
)
from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader, XtQuantTraderCallback


class FakeDeal:
    m_strAccountID = "acct"
    m_strInstrumentID = "600000.SH"
    m_dPrice = 10.5
    m_nVolume = 100
    m_strTradeID = "T1"
    m_strOrderSysID = "O1"
    m_strTradeTime = "2026-07-02 10:00:00"
    m_nDirection = 48
    m_dTradeAmount = 1050.0
    m_dComssion = 0.5


class FakeDealWithOfficialDateTime:
    """官方 Deal 字段: m_strTradeDate + m_strTradeTime 分离 (格式未注明,
    按 QMT 惯例 'YYYYMMDD' / 'HHMMSS' 构造)。"""

    m_strAccountID = "acct"
    m_strInstrumentID = "600000.SH"
    m_dPrice = 10.5
    m_nVolume = 100
    m_strTradeID = "T2"
    m_strOrderSysID = "O2"
    m_strTradeDate = "20260702"
    m_strTradeTime = "100001"
    m_nDirection = 48
    m_dTradeAmount = 1050.0


class FakeOrder:
    m_strAccountID = "acct"
    m_strInstrumentID = "000001.SZ"
    m_nOrderStatus = 50
    m_nVolumeTotal = 200
    m_nVolumeTraded = 50
    m_dLimitPrice = 9.9
    m_dTradedPrice = 9.8
    m_strOrderSysID = "O2"
    m_nDirection = 49
    strategyName = "s1"
    m_strRemark = "remark-1"
    m_strOptName = "限价买入"


class FakeOrderWithInsertDateTime:
    """官方 Order 字段: m_strInsertDate + m_strInsertTime (报单日期/时间)。"""

    m_strAccountID = "acct"
    m_strInstrumentID = "000001.SZ"
    m_nOrderStatus = 50
    m_nVolumeTotal = 200
    m_nVolumeTraded = 50
    m_dLimitPrice = 9.9
    m_strOrderSysID = "O3"
    m_nDirection = 49
    strategyName = "s2"
    m_strRemark = "remark-2"
    m_strInsertDate = "20260702"
    m_strInsertTime = "093000"


class FakeOrderBareCodeLive:
    """实盘 order_callback 观察到的形态: m_strInstrumentID 只带 6 位裸代码，
    交易所在 m_strExchangeID；全部成交后 m_nVolumeTotal(剩余)=0 而
    m_nVolumeTotalOriginal(原始委托量)=100。"""

    m_strAccountID = "acct"
    m_strInstrumentID = "600000"
    m_strExchangeID = "SH"
    m_nOrderStatus = 56
    m_nVolumeTotalOriginal = 100
    m_nVolumeTotal = 0
    m_nVolumeTraded = 100
    m_dLimitPrice = 9.14
    m_strOrderSysID = "635005224"
    m_nDirection = 48
    m_strRemark = "live-remark-1"


class FakeDealBareCodeLive:
    """实盘 deal_callback 观察到的形态: 裸代码 + m_strExchangeID，
    m_strTradeDate/m_strTradeTime 为 YYYYMMDD/HHMMSS。"""

    m_strAccountID = "acct"
    m_strInstrumentID = "600000"
    m_strExchangeID = "SH"
    m_dPrice = 9.06
    m_nVolume = 100
    m_strTradeID = "23808292"
    m_strOrderSysID = "635005224"
    m_strTradeDate = "20260821"
    m_strTradeTime = "100952"
    m_nDirection = 48
    m_dTradeAmount = 906.0


class FakeRedis:
    def __init__(self):
        self.xadds = []
        self.pubs = []
        self.kv = {}

    def xadd(self, key, fields, maxlen=None, approximate=None):
        self.xadds.append((key, fields))
        return b"1-0"

    def publish(self, key, value):
        self.pubs.append((key, value))
        return 1

    def setex(self, key, _ttl, value):
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)


class RecordingCallback(XtQuantTraderCallback):
    def __init__(self):
        self.orders = []
        self.trades = []
        self.order_errors = []
        self.cancel_errors = []
        self.async_responses = []
        self.cancel_async_responses = []
        self.account_statuses = []

    def on_stock_order(self, order):
        self.orders.append(order)

    def on_stock_trade(self, trade):
        self.trades.append(trade)

    def on_order_error(self, order_error):
        self.order_errors.append(order_error)

    def on_cancel_error(self, cancel_error):
        self.cancel_errors.append(cancel_error)

    def on_order_stock_async_response(self, response):
        self.async_responses.append(response)

    def on_cancel_order_stock_async_response(self, response):
        self.cancel_async_responses.append(response)

    def on_account_status(self, status):
        self.account_statuses.append(status)


class ExecEventsServerTest(unittest.TestCase):
    def test_normalize_trade_event_maps_thinktrader_fields(self):
        ev = normalize_trade_event(FakeDeal(), "acct")

        self.assertEqual(ev["event_type"], "trade")
        self.assertEqual(ev["stock_code"], "600000.SH")
        self.assertEqual(ev["trade_id"], "T1")
        self.assertEqual(ev["order_sys_id"], "O1")
        self.assertEqual(ev["volume"], 100)
        self.assertEqual(ev["price"], 10.5)
        self.assertEqual(ev["action"], "BUY")  # m_nDirection 48 -> buy
        self.assertEqual(ev["traded_at"], "2026-07-02 10:00:00")
        self.assertEqual(ev["commission"], 0.5)

    def test_normalize_trade_event_emits_real_traded_time(self):
        # 官方 Deal 字段 m_strTradeDate + m_strTradeTime -> 真实成交 Unix 秒。
        ev = normalize_trade_event(FakeDealWithOfficialDateTime(), "acct")
        expected = int(time.mktime(time.strptime("20260702100001", "%Y%m%d%H%M%S")))
        self.assertEqual(ev["traded_time"], expected)

    def test_normalize_trade_event_parses_datetime_shaped_trade_time(self):
        # m_strTradeTime 也可能是 'YYYY-MM-DD HH:MM:SS' 完整日期时间 (无 TradeDate)。
        ev = normalize_trade_event(FakeDeal(), "acct")
        expected = int(time.mktime(time.strptime("2026-07-02 10:00:00", "%Y-%m-%d %H:%M:%S")))
        self.assertEqual(ev.get("traded_time"), expected)

    def test_normalize_order_event_maps_thinktrader_fields(self):
        ev = normalize_order_event(FakeOrder(), "acct")

        self.assertEqual(ev["event_type"], "order")
        self.assertEqual(ev["stock_code"], "000001.SZ")
        self.assertEqual(ev["order_sys_id"], "O2")
        self.assertEqual(ev["order_volume"], 200)
        self.assertEqual(ev["traded_volume"], 50)
        self.assertEqual(ev["price"], 9.9)
        self.assertEqual(ev["traded_price"], 9.8)
        self.assertEqual(ev["status"], 50)
        self.assertEqual(ev["action"], "SELL")  # m_nDirection 49 -> sell
        self.assertEqual(ev["strategy_name"], "s1")
        self.assertEqual(ev["remark"], "remark-1")
        self.assertEqual(ev["user_order_id"], "remark-1")
        self.assertEqual(ev["opt_name"], "限价买入")

    def test_normalize_order_event_emits_real_order_time(self):
        # 官方 Order 字段 m_strInsertDate + m_strInsertTime -> 真实报单 Unix 秒。
        ev = normalize_order_event(FakeOrderWithInsertDateTime(), "acct")
        expected = int(time.mktime(time.strptime("20260702093000", "%Y%m%d%H%M%S")))
        self.assertEqual(ev["order_time"], expected)

    def test_normalize_order_event_appends_exchange_suffix_to_bare_code(self):
        # 实盘: order_callback 的 m_strInstrumentID 只有裸代码；交易所后缀必须
        # 从 m_strExchangeID 补全，与 MiniQMT 契约及查询路径的代码形态一致。
        ev = normalize_order_event(FakeOrderBareCodeLive(), "acct")
        self.assertEqual(ev["stock_code"], "600000.SH")

    def test_normalize_order_event_order_volume_prefers_original_not_remaining(self):
        # MiniQMT XtOrder.order_volume 是原始委托量；全部成交推送里剩余量
        # m_nVolumeTotal=0，若读剩余量会把已成交委托显示成 0 股。
        ev = normalize_order_event(FakeOrderBareCodeLive(), "acct")
        self.assertEqual(ev["order_volume"], 100)

    def test_normalize_order_event_suffixed_code_unchanged_with_exchange(self):
        # 已带后缀的代码不能因为对象带 m_strExchangeID 而重复追加。
        class Suffixed(FakeOrderBareCodeLive):
            m_strInstrumentID = "600000.SH"

        ev = normalize_order_event(Suffixed(), "acct")
        self.assertEqual(ev["stock_code"], "600000.SH")

    def test_normalize_trade_event_appends_exchange_suffix_to_bare_code(self):
        ev = normalize_trade_event(FakeDealBareCodeLive(), "acct")
        self.assertEqual(ev["stock_code"], "600000.SH")
        self.assertEqual(ev["trade_id"], "23808292")
        self.assertEqual(ev["amount"], 906.0)

    def test_order_event_fills_strategy_from_remark_identity(self):
        class CallbackOrder:
            m_strAccountID = "acct"
            m_strInstrumentID = "159518"
            m_strRemark = "涨停价买入1手"
            m_strOptName = "限价买入"

        redis_client = FakeRedis()
        remember_order_identity(redis_client, "acct", "涨停价买入1手", "rpc_test", "159518")
        ev = enrich_order_identity(redis_client, "acct", normalize_order_event(CallbackOrder(), "acct"))

        self.assertEqual(ev["strategy_name"], "rpc_test")
        self.assertEqual(ev["remark"], "涨停价买入1手")

    def test_publish_writes_stream_and_channel(self):
        r = FakeRedis()
        publish_trade_event(r, "acct", {"event_type": "trade", "trade_id": "T1"})

        self.assertEqual(r.pubs[0][0], trade_channel("acct"))
        self.assertEqual(r.xadds[0][0], trade_channel("acct"))
        self.assertIn("T1", r.pubs[0][1])

        publish_order_event(r, "acct", {"event_type": "order"})
        self.assertEqual(r.pubs[1][0], order_channel("acct"))

    def test_arbitration_resolves_direction_offset_conflict_via_op_type(self):
        """When m_nDirection and m_nOffsetFlag disagree (futures: sell+open),
        arbitration via m_nOpType picks the semantically correct field."""
        class Deal:
            m_strInstrumentID = "600000.SH"
            m_nDirection = 49    # EEntrustBS sell
            m_nOffsetFlag = 48   # offset 48 = 开仓 (open)
            m_nOpType = 24       # STOCK_SELL — arbiter confirms sell
            m_nVolume = 10
            m_dPrice = 1.0
            m_strTradeID = "X"

        ev = normalize_trade_event(Deal(), "acct")

        self.assertEqual(ev["action"], "SELL")       # from direction via arbitration
        self.assertEqual(ev["direction"], 49)        # direction field = m_nDirection
        self.assertEqual(ev["offset_flag"], 48)      # raw offset preserved, not conflated

    def test_arbitration_stock_sell_wrong_direction_fixed_by_op_type(self):
        """Stock sell: m_nDirection=48 (bug: always 48), m_nOffsetFlag=49,
        m_nOpType=24 → arbitration picks offset (49→SELL)."""
        class SellOrder:
            m_strInstrumentID = "601398.SH"
            m_nDirection = 48       # QMT bug — always 48 in live callbacks
            m_nOffsetFlag = 49      # 平仓 = sell (correct)
            m_nOpType = 24          # STOCK_SELL (correct)
            m_nVolumeTotal = 100
            m_nVolumeTraded = 0
            m_dLimitPrice = 6.34
            m_strOrderSysID = "S123"

        ev = normalize_order_event(SellOrder(), "acct")
        self.assertEqual(ev["action"], "SELL")
        self.assertEqual(ev["direction"], 49)       # offset_flag wins via arbitration

    def test_arbitration_stock_buy_agree(self):
        """Stock buy: m_nDirection=48, m_nOffsetFlag=48 → agree → BUY."""
        class BuyOrder:
            m_strInstrumentID = "601398.SH"
            m_nDirection = 48
            m_nOffsetFlag = 48
            m_nOpType = 23
            m_nVolumeTotal = 100
            m_nVolumeTraded = 0
            m_dLimitPrice = 5.0
            m_strOrderSysID = "B456"

        ev = normalize_order_event(BuyOrder(), "acct")
        self.assertEqual(ev["action"], "BUY")
        self.assertEqual(ev["direction"], 48)

    def test_direction_zero_falls_back_to_offset(self):
        """m_nDirection=0 is treated as absent; offset determines direction."""
        class SellOrder:
            m_strInstrumentID = "601398.SH"
            m_nDirection = 0
            m_nOffsetFlag = 49
            m_nVolumeTotal = 100
            m_nVolumeTraded = 0
            m_dLimitPrice = 6.34
            m_strOrderSysID = "S123"

        ev = normalize_order_event(SellOrder(), "acct")
        self.assertEqual(ev["action"], "SELL")
        self.assertEqual(ev["direction"], 49)

    def test_direction_none_falls_back_to_offset(self):
        """m_nDirection=None → offset determines direction."""
        class BuyOrder:
            m_strInstrumentID = "601398.SH"
            m_nDirection = None
            m_nOffsetFlag = 48
            m_nVolumeTotal = 100
            m_nVolumeTraded = 0
            m_dLimitPrice = 5.0
            m_strOrderSysID = "B456"

        ev = normalize_order_event(BuyOrder(), "acct")
        self.assertEqual(ev["action"], "BUY")
        self.assertEqual(ev["direction"], 48)

    def test_pledge_direction_has_no_buy_sell_action(self):
        class Deal:
            m_strInstrumentID = "600000.SH"
            m_nDirection = 81   # 质押入库
            m_nVolume = 10
            m_dPrice = 1.0

        ev = normalize_trade_event(Deal(), "acct")

        self.assertEqual(ev["action"], "")   # pledge is neither buy nor sell
        self.assertEqual(ev["direction"], 81)  # raw direction preserved

    def test_normalize_order_error_event_maps_fields(self):
        class OrderError:
            m_strAccountID = "acct"
            m_strInstrumentID = "600654.SH"
            m_strOrderSysID = "sys-err-1"
            m_nErrorID = 2147483647
            m_strErrorMsg = "废单"

        ev = normalize_order_error_event(OrderError(), "acct")

        self.assertEqual(ev["event_type"], "order_error")
        self.assertEqual(ev["account_id"], "acct")
        self.assertEqual(ev["stock_code"], "600654.SH")
        self.assertEqual(ev["order_sys_id"], "sys-err-1")
        self.assertEqual(ev["error_id"], 2147483647)
        self.assertEqual(ev["error_msg"], "废单")

    def test_normalize_order_error_carries_remark_and_status(self):
        # issue #64: 柜台拒单时 error_msg 已有原因（#60），但事件缺 order_remark
        # 和 status，客户端关联不上是哪一笔委托。
        class OrderError:
            m_strAccountID = "acct"
            m_strInstrumentID = "600654.SH"
            m_strOrderSysID = "sys-err-2"
            m_nOrderStatus = 57
            m_strRemark = "grid-tag-7"
            m_strCancelInfo = "[COUNTER] 资金可用余额不足"

        ev = normalize_order_error_event(OrderError(), "acct")

        self.assertEqual(ev["order_remark"], "grid-tag-7")
        self.assertEqual(ev["user_order_id"], "grid-tag-7")
        self.assertEqual(ev["status"], 57)
        self.assertEqual(ev["error_msg"], "[COUNTER] 资金可用余额不足")

    def test_normalize_cancel_error_event_maps_fields(self):
        class CancelError:
            m_strAccountID = "acct"
            m_strInstrumentID = "600654.SH"
            m_strOrderSysID = "sys-cancel-1"
            m_nErrorID = 99
            m_strErrorMsg = "撤单失败"

        ev = normalize_cancel_error_event(CancelError(), "acct")

        self.assertEqual(ev["event_type"], "cancel_error")
        self.assertEqual(ev["account_id"], "acct")
        self.assertEqual(ev["order_sys_id"], "sys-cancel-1")
        self.assertEqual(ev["error_id"], 99)
        self.assertEqual(ev["error_msg"], "撤单失败")

    def test_error_channels_are_account_scoped(self):
        self.assertTrue(order_error_channel("acct").endswith(":acct"))
        self.assertTrue(cancel_error_channel("acct").endswith(":acct"))


class RawFieldSnapshotTest(unittest.TestCase):
    """The snapshot exists to settle what live callbacks actually carry, so it
    must capture m_* and MiniQMT fields alike and never raise."""

    def test_captures_thinktrader_and_miniqmt_fields(self):
        snap = raw_field_snapshot(FakeOrder())

        self.assertIn("m_nDirection", snap)
        self.assertIn("49", snap["m_nDirection"])
        self.assertIn("int", snap["m_nDirection"])
        self.assertIn("m_strInstrumentID", snap)

    def test_captures_miniqmt_style_object(self):
        class XtOrderLike:
            stock_code = "601398.SH"
            order_type = 24
            order_volume = 100

        snap = raw_field_snapshot(XtOrderLike())

        self.assertIn("24", snap["order_type"])
        self.assertIn("601398.SH", snap["stock_code"])

    def test_captures_dict_payload(self):
        snap = raw_field_snapshot({"m_nOffsetFlag": 48, "order_type": 24})

        self.assertIn("48", snap["m_nOffsetFlag"])
        self.assertIn("24", snap["order_type"])

    def test_skips_callables_and_dunders(self):
        class WithMethod:
            m_nDirection = 49

            def m_method(self):
                return 1

        snap = raw_field_snapshot(WithMethod())

        self.assertIn("m_nDirection", snap)
        self.assertNotIn("m_method", snap)

    def test_unreadable_attribute_does_not_raise(self):
        class Exploding:
            m_nDirection = 49

            @property
            def m_nOffsetFlag(self):
                raise RuntimeError("boom")

        snap = raw_field_snapshot(Exploding())

        self.assertIn("m_nDirection", snap)
        self.assertIn("unreadable", snap["m_nOffsetFlag"])

    def test_format_is_a_single_ascii_safe_line(self):
        line = format_raw_snapshot("order", FakeOrder())

        self.assertNotIn("\n", line)
        self.assertTrue(line.startswith("[bigqmt_exec_raw] order"))
        self.assertIn("m_nDirection", line)


class ExecEventsClientDispatchTest(unittest.TestCase):
    def _trader(self):
        trader = BigQmtXtTrader(account_id="acct")
        cb = RecordingCallback()
        trader.register_callback(cb)
        return trader, cb

    def test_dispatch_trade_invokes_on_stock_trade(self):
        trader, cb = self._trader()
        event = {
            "event_type": "trade",
            "account_id": "acct",
            "stock_code": "600000.SH",
            "order_sys_id": "sys-1",
            "trade_id": "t-1",
            "volume": 100,
            "price": 10.5,
            "action": "BUY",
            "traded_at": "2026-07-02 10:00:00",
        }
        trader._dispatch_event(json.dumps(event).encode("utf-8"))

        self.assertEqual(len(cb.trades), 1)
        trade = cb.trades[0]
        self.assertEqual(trade.stock_code, "600000.SH")
        self.assertEqual(trade.trade_id, "t-1")
        self.assertEqual(trade.traded_volume, 100)
        self.assertEqual(trade.traded_price, 10.5)
        self.assertEqual(trade.order_type, 23)  # BUY -> STOCK_BUY

    def test_dispatch_order_invokes_on_stock_order(self):
        trader, cb = self._trader()
        event = {
            "event_type": "order",
            "account_id": "acct",
            "stock_code": "000001.SZ",
            "order_sys_id": "sys-2",
            "order_volume": 200,
            "traded_volume": 50,
            "price": 9.9,
            "traded_price": 9.8,
            "status": 50,
            "action": "SELL",
        }
        trader._dispatch_event(json.dumps(event).encode("utf-8"))

        self.assertEqual(len(cb.orders), 1)
        order = cb.orders[0]
        self.assertEqual(order.stock_code, "000001.SZ")
        self.assertEqual(order.order_volume, 200)
        self.assertEqual(order.traded_volume, 50)
        self.assertEqual(order.price, 9.9)
        self.assertEqual(order.traded_price, 9.8)
        self.assertEqual(order.order_status, 50)
        self.assertEqual(order.order_type, 24)  # SELL -> STOCK_SELL

    def test_dispatch_order_bare_code_gets_inferred_suffix(self):
        # 老服务端事件只带 6 位裸代码（无交易所信息可补全）时，客户端按
        # A 股代码段推断后缀，保证回调对象与 MiniQMT 契约一致。
        trader, cb = self._trader()
        event = {
            "event_type": "order",
            "account_id": "acct",
            "stock_code": "600000",
            "order_sys_id": "sys-bare",
            "order_volume": 100,
            "traded_volume": 0,
            "price": 9.14,
            "status": 50,
            "action": "BUY",
        }
        trader._dispatch_event(json.dumps(event).encode("utf-8"))

        self.assertEqual(len(cb.orders), 1)
        self.assertEqual(cb.orders[0].stock_code, "600000.SH")

    def test_dispatch_trade_bare_code_gets_inferred_suffix(self):
        trader, cb = self._trader()
        event = {
            "event_type": "trade",
            "account_id": "acct",
            "stock_code": "000001",
            "order_sys_id": "sys-bare",
            "trade_id": "t-bare",
            "volume": 100,
            "price": 10.5,
            "action": "BUY",
            "traded_at": "2026-07-02 10:00:00",
        }
        trader._dispatch_event(json.dumps(event).encode("utf-8"))

        self.assertEqual(len(cb.trades), 1)
        self.assertEqual(cb.trades[0].stock_code, "000001.SZ")

    def test_dispatch_without_callback_is_noop(self):
        trader = BigQmtXtTrader(account_id="acct")
        # No callback registered; must not raise.
        trader._dispatch_event(json.dumps({"event_type": "trade"}).encode("utf-8"))

    def test_dispatch_order_error_invokes_on_order_error(self):
        trader, cb = self._trader()
        event = {
            "event_type": "order_error",
            "account_id": "acct",
            "stock_code": "600654.SH",
            "order_sys_id": "sys-err-1",
            "error_id": 2147483647,
            "error_msg": "废单",
        }
        trader._dispatch_event(json.dumps(event).encode("utf-8"))

        self.assertEqual(len(cb.order_errors), 1)
        err = cb.order_errors[0]
        self.assertEqual(str(err.order_id), "sys-err-1")
        self.assertEqual(err.error_id, 2147483647)
        self.assertEqual(err.error_msg, "废单")
        self.assertEqual(err.stock_code, "600654.SH")

    def test_dispatch_cancel_error_invokes_on_cancel_error(self):
        trader, cb = self._trader()
        event = {
            "event_type": "cancel_error",
            "account_id": "acct",
            "stock_code": "600654.SH",
            "order_sys_id": "sys-cancel-1",
            "error_id": 99,
            "error_msg": "撤单失败",
        }
        trader._dispatch_event(json.dumps(event).encode("utf-8"))

        self.assertEqual(len(cb.cancel_errors), 1)
        err = cb.cancel_errors[0]
        self.assertEqual(str(err.order_id), "sys-cancel-1")
        self.assertEqual(err.error_id, 99)
        self.assertEqual(err.error_msg, "撤单失败")

    def test_error_callbacks_carry_miniqmt_order_sysid_alias(self):
        # issue #65: on_stock_order/on_stock_trade 用 order_sysid（MiniQMT 名），
        # on_order_error/on_cancel_error/async 回报却是 order_sys_id——不一致。
        # 所有回调对象现在必须同时带 order_sysid 和 order_sys_id。
        trader, cb = self._trader()
        trader._dispatch_event(json.dumps({
            "event_type": "order_error", "account_id": "acct",
            "order_sys_id": "sys-err-1", "error_msg": "废单",
        }).encode("utf-8"))
        trader._dispatch_event(json.dumps({
            "event_type": "cancel_error", "account_id": "acct",
            "order_sys_id": "sys-cancel-1", "error_msg": "撤单失败",
        }).encode("utf-8"))

        err = cb.order_errors[0]
        self.assertEqual(err.order_sysid, "sys-err-1")
        self.assertEqual(err.order_sys_id, "sys-err-1")
        self.assertEqual(str(err.order_id), "sys-err-1")
        cerr = cb.cancel_errors[0]
        self.assertEqual(cerr.order_sysid, "sys-cancel-1")
        self.assertEqual(cerr.order_sys_id, "sys-cancel-1")

    def test_order_error_carries_remark_and_status(self):
        # issue #64: 柜台拒单时 error_msg 带原因（#60），但 order_remark 是空的、
        # status 也没有，客户端无法关联是哪一笔委托被拒。
        trader, cb = self._trader()
        trader._dispatch_event(json.dumps({
            "event_type": "order_error", "account_id": "acct",
            "order_sys_id": "sys-err-2", "order_remark": "my-tag-1",
            "status": 57, "error_id": 2147483647,
            "error_msg": "[COUNTER] 资金可用余额不足",
        }).encode("utf-8"))

        err = cb.order_errors[0]
        self.assertEqual(err.order_remark, "my-tag-1")
        self.assertEqual(err.status, 57)


    def _run_async(self, trader, result=None, raises=None):
        """Submit one async order with order_stock_result stubbed, and wait.

        order_stock_async is fire-and-forget since issue #50: it returns the seq
        without touching the network and the submit happens on a worker thread,
        so the callback assertions need the queue drained first. The stub is
        restored only after the worker is done with it.
        """
        original = trader.order_stock_result

        def fake(*args, **kwargs):
            if raises is not None:
                raise raises
            return result

        trader.order_stock_result = fake
        try:
            seq = trader.order_stock_async("acct", "600654.SH", 23, 100, 11, 10.0, "s", "r")
            self.assertTrue(trader.wait_async_orders(timeout=5.0), "async order did not finish")
        finally:
            trader.order_stock_result = original
        return seq

    def test_order_stock_async_returns_seq_without_submitting(self):
        """issue #50: the seq must come back before any RPC happens."""
        trader, _cb = self._trader()
        started = threading.Event()
        release = threading.Event()

        def blocking(*args, **kwargs):
            started.set()
            release.wait(5.0)
            return {"order_sys_id": "sys-slow"}

        trader.order_stock_result = blocking
        try:
            t0 = time.time()
            seq = trader.order_stock_async("acct", "600654.SH", 23, 100, 11, 10.0, "s", "r")
            elapsed = time.time() - t0

            self.assertGreater(seq, 0)
            self.assertLess(elapsed, 0.2, "order_stock_async blocked for %.3fs" % elapsed)
            self.assertTrue(started.wait(5.0), "submit never ran on the worker")
        finally:
            release.set()
            trader.wait_async_orders(timeout=5.0)

    def test_order_stock_async_fires_response_when_submitted(self):
        trader, cb = self._trader()
        seq = self._run_async(trader, result={"order_sys_id": "sys-ok-1", "user_order_id": "u-1"})

        self.assertGreater(seq, 0)
        self.assertEqual(len(cb.async_responses), 1)
        resp = cb.async_responses[0]
        self.assertEqual(str(resp.order_id), "sys-ok-1")
        self.assertEqual(resp.account_id, "acct")
        self.assertEqual(resp.seq, seq)

    def test_order_stock_async_requests_no_settlement_wait(self):
        """The server must not hold the reply for the order id on this path."""
        trader, _cb = self._trader()
        seen = {}
        original = trader.order_stock_result

        def fake(*args, **kwargs):
            seen.update(kwargs)
            return {"order_sys_id": "sys-1"}

        trader.order_stock_result = fake
        try:
            trader.order_stock_async("acct", "600654.SH", 23, 100, 11, 10.0, "s", "r")
            trader.wait_async_orders(timeout=5.0)
        finally:
            trader.order_stock_result = original

        self.assertIs(seen.get("wait_settlement"), False)

    def test_order_stock_async_minus_one_fires_order_error(self):
        trader, cb = self._trader()
        seq = self._run_async(trader, result=-1)  # MiniQMT: submit failed

        self.assertGreater(seq, 0)
        self.assertEqual(len(cb.order_errors), 1)
        err = cb.order_errors[0]
        self.assertEqual(err.error_id, -1)
        self.assertEqual(err.stock_code, "600654.SH")
        self.assertEqual(err.seq, seq)          # correlate the failure to the seq
        # No success response for a failed submit.
        self.assertEqual(len(cb.async_responses), 0)

    def test_order_stock_async_submitted_without_sysid_fires_response_not_error(self):
        # issue #38: passorder 已提交但委托号还没分配到（order_sys_id 为空）时，
        # 必须回调成功响应而不是误报 on_order_error。issue #50 之后这是常态：
        # 异步路径不再等待委托号，它由 order_callback 推送。
        trader, cb = self._trader()
        seq = self._run_async(
            trader, result={"status": "SUBMITTED", "user_order_id": "u-1", "order_sys_id": ""})

        self.assertGreater(seq, 0)
        self.assertEqual(len(cb.order_errors), 0)
        self.assertEqual(len(cb.async_responses), 1)
        resp = cb.async_responses[0]
        self.assertEqual(str(resp.order_id), "u-1")  # 委托号未知时回退到 user_order_id
        self.assertEqual(resp.order_sys_id, "")

    def test_order_stock_async_server_error_fires_order_error_with_reason(self):
        # server_error（委托没进系统）由 call() 转成异常后，async 必须把真实
        # 原因回调给 on_order_error（issue #38）。
        trader, cb = self._trader()
        seq = self._run_async(trader, raises=RuntimeError(
            "Big QMT order_stock server_error: passorder submitted but "
            "order not found in system (stock=600654.SH action=BUY price=10.00 "
            "volume=100). QMT may have silently rejected it."))

        self.assertGreater(seq, 0)
        self.assertEqual(len(cb.async_responses), 0)
        self.assertEqual(len(cb.order_errors), 1)
        err = cb.order_errors[0]
        self.assertIn("not found in system", err.error_msg)
        self.assertEqual(err.stock_code, "600654.SH")

    def test_async_orders_keep_submission_order(self):
        """One worker, so responses arrive in the order the calls were made."""
        trader, cb = self._trader()
        original = trader.order_stock_result

        def fake(*args, **kwargs):
            return {"order_sys_id": "sys-%s" % args[1]}

        trader.order_stock_result = fake
        try:
            for code in ("A.SH", "B.SH", "C.SH"):
                trader.order_stock_async("acct", code, 23, 100, 11, 10.0, "s", "r")
            self.assertTrue(trader.wait_async_orders(timeout=5.0))
        finally:
            trader.order_stock_result = original

        self.assertEqual([str(r.order_id) for r in cb.async_responses],
                         ["sys-A.SH", "sys-B.SH", "sys-C.SH"])
        self.assertEqual([r.seq for r in cb.async_responses],
                         sorted(r.seq for r in cb.async_responses))

    def test_cancel_order_stock_async_fires_response(self):
        trader, cb = self._trader()
        original = trader.cancel_order_stock_sysid

        def fake_cancel(account, market, sysid):
            return 0        # MiniQMT: 0 == success (issue #113)

        trader.cancel_order_stock_sysid = fake_cancel
        try:
            seq = trader.cancel_order_stock_sysid_async("acct", "SH", "sys-1")
        finally:
            trader.cancel_order_stock_sysid = original

        self.assertGreater(seq, 0)
        self.assertEqual(len(cb.cancel_async_responses), 1)
        resp = cb.cancel_async_responses[0]
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_sys_id, "sys-1")
        self.assertEqual(resp.account_id, "acct")
        self.assertEqual(resp.seq, seq)

    def test_connect_and_subscribe_fire_account_status(self):
        trader, cb = self._trader()
        trader.client.account_id = "acct"
        # connect() calls ping via RPC — stub it.
        trader.client.call = lambda *a, **k: {"ok": True}
        trader.connect()
        trader.subscribe("acct")

        self.assertEqual(len(cb.account_statuses), 2)
        status = cb.account_statuses[0]
        self.assertEqual(status.account_id, "acct")
        self.assertEqual(status.account_type, "STOCK")
        self.assertEqual(status.status, 1)


if __name__ == "__main__":
    unittest.main()
