"""期货 / ETF 期权 opType 必须原样到达 passorder（issue 中 PR #129 的场景）。

和 credit 那一层是同一类问题：opType 编码的信息比「买还是卖」多。期货的
0-15 里带着开/平和今/昨，ETF 期权的 50-59 里带着开/平和备兑。映射回 BUY/SELL
再重新拼一个 opType 就把这些丢了——平今多会变成普通卖出。

数值全部对着官方枚举核过，不是按数字规律推的：
https://dict.thinktrader.net/innerApi/enum_constants.html?id=NF25nX
（本仓库 docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md 10.1 节是它的抄录）

两个容易踩的地方，各有一组用例钉住：

* **官方只定义 0-15**。16-22 没有定义，不能当期货收下——PR #129 的第一版
  把 0-22 全当期货，多出来的 7 个值没有依据。
* **ETF 期权是 50-59，不是 48-57**，而且段内不是「偶数买奇数卖」：
  52 卖出开仓、53 买入平仓、54 备兑开仓、55 备兑平仓，规律从 52 就断了。
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.order_bigqmt import (
    BigQmtOrderGateway, passthrough_action_of, passthrough_optype_of)
from bigqmt_signal_trader.models import OrderRequest
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers


# opType -> (官方含义, 期望的记账方向)。None = 没有买卖方向。
OFFICIAL = {
    # 期货/股指期权/商品期权 —— 六键
    0: ("开多", "BUY"),
    1: ("平昨多", "SELL"),
    2: ("平今多", "SELL"),
    3: ("开空", "SELL"),
    4: ("平昨空", "BUY"),
    5: ("平今空", "BUY"),
    # 四键
    6: ("平多，优先平今", "SELL"),
    7: ("平多，优先平昨", "SELL"),
    8: ("平空，优先平今", "BUY"),
    9: ("平空，优先平昨", "BUY"),
    # 两键
    10: ("卖出，多仓优先平今，余量开空", "SELL"),
    11: ("卖出，多仓优先平昨，余量开空", "SELL"),
    12: ("买入，空仓优先平今，余量开多", "BUY"),
    13: ("买入，空仓优先平昨，余量开多", "BUY"),
    14: ("买入，不优先平仓", "BUY"),
    15: ("卖出，不优先平仓", "SELL"),
    # ETF 期权
    50: ("买入开仓", "BUY"),
    51: ("卖出平仓", "SELL"),
    52: ("卖出开仓", "SELL"),
    53: ("买入平仓", "BUY"),
    54: ("备兑开仓", "SELL"),
    55: ("备兑平仓", "BUY"),
    56: ("认购行权", None),
    57: ("认沽行权", None),
    58: ("证券锁定", None),
    59: ("证券解锁", None),
}

UNDEFINED_OP_TYPES = tuple(range(16, 23))   # 官方表里没有


class _Recorder(object):
    def __init__(self):
        self.op_type = None

    def __call__(self, op_type, order_type, account, code, price_type, price,
                 volume, strategy, quick, remark, context=None, *args, **kwargs):
        self.op_type = op_type


def _submit(order_type, action="BUY", account_type="FUTURE"):
    recorder = _Recorder()
    gateway = BigQmtOrderGateway(account_id="acct", passorder_func=recorder,
                                 context_info=object(), account_type=account_type)
    gateway.submit(OrderRequest(
        signal_id="s", account_id="acct", stock_code="IF2601.IF", action=action,
        volume=1, price=4000.0, price_type="LIMIT", strategy_name="s",
        order_type=order_type))
    return recorder.op_type


def _handlers():
    # 和 test_credit_order_types.py / test_unknown_order_type_message.py 一致：
    # 这几个方法是纯函数，不需要真的建连接
    return BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)


class DirectionMatchesTheOfficialTableTest(unittest.TestCase):
    """逐个 opType 对着官方表核方向，不靠数字规律。"""

    def test_every_documented_op_type_has_the_official_side(self):
        for op_type, (name, expected) in sorted(OFFICIAL.items()):
            self.assertEqual(passthrough_action_of(op_type), expected,
                             "opType %d (%s)" % (op_type, name))

    def test_closing_a_long_is_a_sell_and_closing_a_short_is_a_buy(self):
        # 这是最容易搞反的一组：平多是卖、平空是买
        for op_type in (1, 2, 6, 7):        # 平昨多/平今多/平多优先今/平多优先昨
            self.assertEqual(passthrough_action_of(op_type), "SELL", op_type)
        for op_type in (4, 5, 8, 9):        # 平昨空/平今空/平空优先今/平空优先昨
            self.assertEqual(passthrough_action_of(op_type), "BUY", op_type)

    def test_etf_option_side_is_not_odd_even(self):
        # 50 买 / 51 卖之后规律就断了，写死规律会全错
        self.assertEqual(passthrough_action_of(52), "SELL")   # 卖出开仓
        self.assertEqual(passthrough_action_of(53), "BUY")    # 买入平仓
        self.assertEqual(passthrough_action_of(54), "SELL")   # 备兑开仓
        self.assertEqual(passthrough_action_of(55), "BUY")    # 备兑平仓

    def test_exercise_and_lock_have_no_side(self):
        for op_type in (56, 57, 58, 59):
            self.assertIsNone(passthrough_action_of(op_type), op_type)
            # 但仍然是可直通的 opType
            self.assertEqual(passthrough_optype_of(op_type), op_type)


class RangeBoundaryTest(unittest.TestCase):
    def test_undefined_range_is_not_treated_as_futures(self):
        for op_type in UNDEFINED_OP_TYPES:
            self.assertIsNone(passthrough_optype_of(op_type),
                              "opType %d 官方表里没有定义" % op_type)
            self.assertIsNone(passthrough_action_of(op_type), op_type)

    def test_stock_and_credit_types_are_not_diverted(self):
        for op_type in (23, 24, 27, 28, 32, 33, 34, 70, 75):
            self.assertIsNone(passthrough_optype_of(op_type), op_type)

    def test_etf_option_range_starts_at_fifty(self):
        # 48/49 属于组合交易，不是 ETF 期权
        for op_type in (48, 49):
            self.assertIsNone(passthrough_optype_of(op_type), op_type)

    def test_garbage_is_rejected_not_crashed(self):
        for value in (None, "", "abc", object()):
            self.assertIsNone(passthrough_optype_of(value))
            self.assertIsNone(passthrough_action_of(value))


class SubmitForwardsRawOpTypeTest(unittest.TestCase):
    def test_futures_account_forwards_the_op_type_untouched(self):
        for op_type in sorted(OFFICIAL):
            self.assertEqual(_submit(op_type, action="BUY", account_type="FUTURE"),
                             op_type, op_type)

    def test_stock_option_account_forwards_too(self):
        self.assertEqual(_submit(50, account_type="STOCK_OPTION"), 50)

    def test_stock_account_refuses_a_futures_op_type(self):
        # 关键安全用例：股票账号收到期货 opType 必须拒绝。
        # 回落到 23/24 会真的发出一笔品种和方向都不对的股票单 —— 这正是
        # issue #103 的失败模式（"a real but different order"）。
        with self.assertRaises(ValueError) as ctx:
            _submit(2, action="BUY", account_type="STOCK")
        message = str(ctx.exception)
        self.assertIn("futures/option opType", message)
        self.assertIn("STOCK", message)

    def test_credit_account_also_refuses_a_futures_op_type(self):
        with self.assertRaises(ValueError):
            _submit(0, action="BUY", account_type="CREDIT")

    def test_plain_stock_orders_are_unaffected(self):
        self.assertEqual(_submit(None, action="BUY", account_type="STOCK"), 23)
        self.assertEqual(_submit(None, action="SELL", account_type="STOCK"), 24)


class RpcActionResolutionTest(unittest.TestCase):
    def test_action_is_derived_from_the_op_type(self):
        handlers = _handlers()
        for op_type, (name, expected) in sorted(OFFICIAL.items()):
            if expected is None:
                continue
            self.assertEqual(
                handlers._order_action_from_params({"order_type": op_type}),
                expected, "opType %d (%s)" % (op_type, name))

    def test_directionless_op_types_get_a_bookkeeping_side(self):
        """They used to demand an explicit action; MiniQMT's order_stock has
        no such parameter, so that made them unreachable (#314). The side
        recorded is bookkeeping only -- passorder gets the raw opType."""
        handlers = _handlers()
        for op_type in (56, 57, 58, 59):
            self.assertIn(handlers._order_action_from_params({"order_type": op_type}),
                          ("BUY", "SELL"))
            self.assertEqual(handlers._forwarded_order_type({"order_type": op_type}), op_type)

    def test_explicit_action_wins_for_directionless_types(self):
        handlers = _handlers()
        params = {"order_type": 56, "action": "BUY"}
        self.assertEqual(handlers._order_action_from_params(params), "BUY")
        self.assertEqual(handlers._forwarded_order_type(params), 56)

    def test_undefined_op_type_still_raises_the_deployment_hint(self):
        handlers = _handlers()
        with self.assertRaises(ValueError) as ctx:
            handlers._order_action_from_params({"order_type": 18})
        self.assertIn("not recognised", str(ctx.exception))


class ForwardedOrderTypeIsOrderIndependentTest(unittest.TestCase):
    """透传值不能靠调用顺序传递。

    PR #129 的第一版把原始值写进 params 字典，再由另一个函数读出来 —— 依赖
    OrderRequest(...) 里 action= 恰好写在 order_type= 前面这个关键字求值顺序。
    谁调换一下参数顺序，透传就静默失效，submit() 回落到 14/15，平仓单变开仓单。
    """

    def test_forwarding_does_not_depend_on_calling_action_first(self):
        handlers = _handlers()
        params = {"order_type": 2}
        # 先取透传值、完全不调 _order_action_from_params，也要拿到 2
        self.assertEqual(handlers._forwarded_order_type(params), 2)
        self.assertEqual(handlers._order_action_from_params(params), "SELL")
        self.assertEqual(handlers._forwarded_order_type(params), 2)

    def test_resolver_leaves_no_state_in_params(self):
        handlers = _handlers()
        params = {"order_type": 5}
        handlers._order_action_from_params(params)
        self.assertEqual(params, {"order_type": 5}, "不应往 params 里塞中间状态")

    def test_credit_forwarding_is_unchanged(self):
        handlers = _handlers()
        self.assertEqual(handlers._forwarded_order_type({"order_type": 27}), 27)
        self.assertIsNone(handlers._forwarded_order_type({"order_type": 23}))


if __name__ == "__main__":
    unittest.main()
