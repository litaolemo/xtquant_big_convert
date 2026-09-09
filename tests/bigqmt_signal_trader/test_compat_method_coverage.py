"""兼容层不能漏掉服务端已经支持的 MiniQMT 方法（issue #130）。

#130 的现象是 `xtdata.download_sector_data()` 抛 AttributeError。查下来服务端
适配器和 RPC 白名单里一直都有这个方法，缺的只是客户端 BigQmtXtData 上那层包装。
同一类缺口当时一共 6 个，光靠人工比对迟早会再漏。

所以这里钉一条不变式：

    白名单（MARKET_DATA_METHODS）里的每个方法，要么兼容层有同名包装，
    要么明确写进 CALL_METHOD_ONLY —— 二选一，不能悄悄漏掉。

`CALL_METHOD_ONLY` 是大 QMT 独有的 ContextInfo 扩展，MiniQMT 本来就没有这些
方法，按设计走 `xtdata.call_method()` 兜底（README「通用 RPC 兜底」一节）。
往白名单加新方法时，这个测试会强制你做一次选择。

注意不要用 `hasattr(xtquant.xtdata, name)` 当判据：本仓库 `src/xtquant/` 有个
shim，测试里 `from xtquant import xtdata` 命中的是兼容层自己，那样断言等于
自己跟自己比，恒真。
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import MARKET_DATA_METHODS
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData, BigQmtXtTrader


# 大 QMT 独有能力（ContextInfo 扩展），MiniQMT 没有对应方法，不需要同名包装。
# 调用方式：xtdata.call_method("get_ETF_list")
#
# issue #262：这张清单一度装着整个「合约/品种」族。问题在于 README 的 RPC
# 表按名字把它们列成「可调用」，报的人照着写 xtdata.get_open_date(...) 就吃
# 一个 AttributeError —— 清单是对的（走 call_method 确实能调），但没有任何
# 一处让调用方发现这一点。现在这一族都有同名包装了（含两个「实测答不了、
# 于是显式报错」的），清单只留真正没人按名字承诺过的。
CALL_METHOD_ONLY = frozenset({
    "get_ETF_list",
})


def _exposed(name):
    return hasattr(BigQmtXtData, name) or hasattr(BigQmtXtTrader, name)


class MethodCoverageTest(unittest.TestCase):
    def test_every_whitelisted_method_is_wrapped_or_declared_call_method_only(self):
        missing = sorted(name for name in MARKET_DATA_METHODS
                         if not _exposed(name) and name not in CALL_METHOD_ONLY)
        self.assertEqual(
            missing, [],
            "服务端白名单里有，但兼容层既没包装也没声明为 call_method 专用：%s\n"
            "MiniQMT 也有的方法 -> 补一个 self._call(\"<name>\", ...) 包装；\n"
            "大 QMT 独有的能力 -> 加进本文件的 CALL_METHOD_ONLY 并说明。\n"
            "（issue #130 就是漏了包装撞出来的 AttributeError）" % missing)

    def test_call_method_only_entries_are_really_in_the_whitelist(self):
        # 清单过期了要能发现：方法从白名单删掉后，这里也该跟着删
        stale = sorted(name for name in CALL_METHOD_ONLY
                       if name not in MARKET_DATA_METHODS)
        self.assertEqual(stale, [], "CALL_METHOD_ONLY 里有白名单已经没有的方法：%s" % stale)

    def test_call_method_only_entries_are_not_secretly_wrapped(self):
        # 反过来：包装上了就该从清单里挪走，免得清单变成谎话
        wrapped = sorted(name for name in CALL_METHOD_ONLY if _exposed(name))
        self.assertEqual(
            wrapped, [],
            "这些已经有包装了，应当从 CALL_METHOD_ONLY 移除：%s" % wrapped)

    def test_the_generic_fallback_exists(self):
        # CALL_METHOD_ONLY 成立的前提
        self.assertTrue(hasattr(BigQmtXtData, "call_method"))


class ReportedGapTest(unittest.TestCase):
    """issue #130 报的那批：服务端一直支持，客户端漏了包装。"""

    REPORTED = ("download_sector_data", "download_cb_data",
                "download_index_weight", "download_history_contracts",
                "get_stock_type", "subscribe_l2thousand")

    def test_all_of_them_are_now_exposed(self):
        for name in self.REPORTED:
            self.assertTrue(_exposed(name), name)

    def test_all_of_them_are_whitelisted_server_side(self):
        for name in self.REPORTED:
            self.assertIn(name, MARKET_DATA_METHODS, name)


class _Recorder(BigQmtXtData):
    """记录 _call 的参数，不真的发 RPC。

    `_answer` 是「服务端这一次会答什么」——#262 的几个包装会回读答案再决定
    转发还是报错，光看参数转对了证明不了什么（CLAUDE.md：验证含义，不是形状）。
    """

    _answer = "ok"

    def __init__(self):
        self.calls = []

    def _call(self, method, **params):
        self.calls.append((method, params))
        return self._answer


class DownloadWrapperTest(unittest.TestCase):
    def setUp(self):
        self.data = _Recorder()

    def test_download_methods_forward_their_own_name_without_params(self):
        for name in ("download_sector_data", "download_cb_data",
                     "download_index_weight"):
            self.data.calls = []
            self.assertEqual(getattr(self.data, name)(), "ok")
            self.assertEqual(self.data.calls, [(name, {})], name)

    def test_download_history_contracts_keeps_the_xtdata_signature(self):
        # xtdata 是 download_history_contracts(incrementally=True)；大 QMT 侧
        # 没有增量参数，形参保留只为签名一致，不往下转发
        self.assertEqual(self.data.download_history_contracts(), "ok")
        self.assertEqual(self.data.download_history_contracts(incrementally=False), "ok")
        self.assertEqual(
            self.data.calls,
            [("download_history_contracts", {}), ("download_history_contracts", {})])


class StockTypeWrapperTest(unittest.TestCase):
    """get_stock_type 是这批里唯一一个不能直接转发的。

    实盘验证发现服务端 ContextInfo.get_stock_type 对任何代码都返回 0
    （股票 / ETF / 债券 / 期权都一样），转发等于把一个假分类递给调用方。
    详见 test_get_stock_type_unavailable.py。
    """

    def test_it_refuses_instead_of_forwarding(self):
        data = _Recorder()

        with self.assertRaises(NotImplementedError):
            data.get_stock_type("600000.SH")

        self.assertEqual(data.calls, [])

class L2ThousandWrapperTest(unittest.TestCase):
    def setUp(self):
        self.data = _Recorder()

    def test_gear_num_defaults_to_zero_like_the_server(self):
        self.data.subscribe_l2thousand("600000.SH")
        self.assertEqual(
            self.data.calls,
            [("subscribe_l2thousand", {"stock_code": "600000.SH", "gear_num": 0})])

    def test_callback_is_accepted_but_not_forwarded(self):
        # RPC 模型下没有回调通道，服务端也会忽略；要推送用 subscribe_whole_quote
        self.data.subscribe_l2thousand("600000.SH", gear_num=5, callback=lambda d: None)
        self.assertEqual(
            self.data.calls,
            [("subscribe_l2thousand", {"stock_code": "600000.SH", "gear_num": 5})])


# ----------------------------------------------------------------------
# issue #262：README「合约/品种」一行按名字列出来的方法，客户端得能按名字调
#
# 报的人（pujfei）打的是：
#     xtdata.get_stock_name("513100.SH")   -> '纳指ETF国泰'   可以
#     xtdata.get_open_date("600519.SH")
#     AttributeError: 'BigQmtXtData' object has no attribute 'get_open_date'
#
# 下面的期望值全部来自 2026-09-09 对实盘桥的实测（见 docs/RPC_API_REFERENCE.md
# 3.12 的备注）。两个「对任何输入都答同一个空值」的按 get_stock_type 的先例
# 显式报错，不转发假答案。
#
# 曾经是三个：get_bvol 也被判成「恒 0」而拒绝转发，实际是取样全是收盘后只剩
# 15:00 集合竞价的股票 —— 逆回购（204001.SH -> 1506858）和 511990.SH
# （-> 22883）立刻有值。所以这里也钉一条：**它必须透传**，0 是合法答案，
# 没有哨兵可用（回归防线，见 ContractInfoWindowVolumeTest）。
# ----------------------------------------------------------------------

README_CONTRACT_ROW = (
    "get_instrument", "get_instrument_type", "get_stock_name", "get_stock_type",
    "get_last_close", "get_last_volume", "get_open_date",
    "get_contract_expire_date", "get_contract_multiplier", "get_float_caps",
    "get_total_share", "get_turn_over_rate", "get_weight_in_index",
    "get_svol", "get_bvol", "get_risk_free_rate", "is_stock_type", "get_cb_info",
)


class ReadmeContractRowIsReachableByNameTest(unittest.TestCase):
    """README 承诺了名字，客户端就不能只有 call_method 兜底。"""

    def test_every_name_in_the_readme_row_is_an_attribute(self):
        missing = sorted(name for name in README_CONTRACT_ROW
                         if not hasattr(BigQmtXtData, name))
        self.assertEqual(
            missing, [],
            "README「合约/品种」列了这些名字，但 BigQmtXtData 上没有：%s" % missing)

    def test_the_reporters_exact_call_no_longer_raises_attribute_error(self):
        data = _Recorder()
        data._answer = 20010827           # 实测 600519.SH 的真实答案

        self.assertEqual(data.get_open_date("600519.SH"), 20010827)
        self.assertEqual(data.calls, [("get_open_date", {"stock": "600519.SH"})])

    def test_get_ticks_is_reachable_by_its_rpc_name(self):
        # README「行情快照」行写的是 get_ticks / get_full_tick
        self.assertTrue(hasattr(BigQmtXtData, "get_ticks"))


class ContractInfoForwardingTest(unittest.TestCase):
    """方法名和参数名都必须和服务端对上。

    服务端 _handle_market_data_method 是 handler(**params)，参数名错一个字
    就是 TypeError —— stock / stockcode / mtkindexcode 三种拼法都在用。
    """

    FORWARDS = (
        ("get_last_close", ("600519.SH",), {}, {"stock": "600519.SH"}),
        ("get_last_volume", ("600519.SH",), {}, {"stock": "600519.SH"}),
        ("get_open_date", ("600519.SH",), {}, {"stock": "600519.SH"}),
        ("get_contract_expire_date", ("600519.SH",), {}, {"stock": "600519.SH"}),
        ("get_svol", ("600519.SH",), {}, {"stock": "600519.SH"}),
        ("get_float_caps", ("600519.SH",), {}, {"stockcode": "600519.SH"}),
        ("get_total_share", ("600519.SH",), {}, {"stockcode": "600519.SH"}),
    )

    def test_each_wrapper_forwards_its_own_name_and_param_name(self):
        for name, args, kwargs, expected in self.FORWARDS:
            data = _Recorder()
            getattr(data, name)(*args, **kwargs)
            self.assertEqual(data.calls, [(name, expected)], name)

    def test_weight_in_index_keeps_both_positional_names(self):
        data = _Recorder()
        data.get_weight_in_index("000300.SH", "600519.SH")
        self.assertEqual(
            data.calls,
            [("get_weight_in_index",
              {"mtkindexcode": "000300.SH", "stockcode": "600519.SH"})])

    def test_risk_free_rate_defaults_to_minus_one_like_the_server(self):
        data = _Recorder()
        data.get_risk_free_rate()
        data.get_risk_free_rate(index=3)
        self.assertEqual(
            data.calls,
            [("get_risk_free_rate", {"index": -1}),
             ("get_risk_free_rate", {"index": 3})])

    def test_get_instrument_is_the_same_answer_as_get_instrument_detail(self):
        # 服务端 METHOD_ALIASES 把 get_instrument_detail 映射到 get_instrument；
        # 客户端三个名字也该回同一份数据
        class _Detail(BigQmtXtData):
            def __init__(self):
                self.seen = []

            def get_instrument_detail(self, stock_code):
                self.seen.append(stock_code)
                return {"InstrumentID": "600519"}

        data = _Detail()
        self.assertEqual(data.get_instrument("600519.SH"), {"InstrumentID": "600519"})
        self.assertEqual(data.seen, ["600519.SH"])

    def test_get_ticks_delegates_to_get_full_tick(self):
        class _Tick(BigQmtXtData):
            def __init__(self):
                self.seen = []

            def get_full_tick(self, code_list, timeout_seconds=None, types=None):
                self.seen.append((list(code_list), timeout_seconds, types))
                return {"600519.SH": {"lastPrice": 1309.3}}

        data = _Tick()
        answer = data.get_ticks(["600519.SH"], timeout_seconds=5, types=["stock"])
        self.assertEqual(answer, {"600519.SH": {"lastPrice": 1309.3}})
        self.assertEqual(data.seen, [(["600519.SH"], 5, ["stock"])])


class ContractInfoAnswerShapeTest(unittest.TestCase):
    """转发不等于答案对。至少 get_open_date 要能把真实形状原样带出来。"""

    def test_get_open_date_passes_through_the_live_yyyymmdd_int(self):
        # 实测 600519.SH -> 20010827、510300.SH -> 20120528，与
        # get_instrument_detail 的 OpenDate 逐位一致
        for code, answer in (("600519.SH", 20010827), ("510300.SH", 20120528)):
            data = _Recorder()
            data._answer = answer
            got = data.get_open_date(code)
            self.assertEqual(got, answer, code)
            self.assertIsInstance(got, int, code)
            self.assertRegex(str(got), r"^(19|20)\d{6}$", code)

    def test_get_contract_expire_date_keeps_the_string_the_terminal_gives(self):
        # 实测是字符串 '99999999'，不是 int —— 别在包装里悄悄转型
        data = _Recorder()
        data._answer = "99999999"
        got = data.get_contract_expire_date("600519.SH")
        self.assertEqual(got, "99999999")
        self.assertIsInstance(got, str)


class ContractInfoRefusalTest(unittest.TestCase):
    """实测对任何输入都答同一个空值的，报错而不是转发。"""

    def test_get_turn_over_rate_refuses_instead_of_handing_back_none(self):
        data = _Recorder()
        data._answer = None
        with self.assertRaises(NotImplementedError) as caught:
            data.get_turn_over_rate("600519.SH")
        text = str(caught.exception)
        self.assertIn("returns None for every code", text)
        self.assertIn("call_method", text)
        self.assertEqual(data.calls, [])

    def test_the_turn_over_rate_hint_uses_pvolume_not_volume(self):
        # get_ticks()['volume'] 是手、get_last_volume 是股，相除小 100 倍
        # （600519.SH 32226 手 vs 3222611 股 / 1250081601 股 = 0.258%）。
        # 提示里写 'volume' 就是把调用方直接送进那个 100 倍错误。
        data = _Recorder()
        data._answer = None
        with self.assertRaises(NotImplementedError) as caught:
            data.get_turn_over_rate("600519.SH")
        text = str(caught.exception)
        # 公式里必须是 pvolume；提到 ['volume'] 只能是「别用它」那句
        self.assertIn("[code]['pvolume']", text)
        self.assertNotIn("[code]['volume']", text)
        self.assertIn("get_last_volume", text)
        self.assertIn("100x", text)             # 说清楚用错字段的后果

    def test_the_turn_over_rate_message_names_the_data_precondition(self):
        # 区间版 get_turnover_rate 要求先下载财务数据（股本）+ 日线
        # （docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md）。本终端没下过，所以
        # 「stub 坏了」并不是结论；有数据的终端应当被指到那条路上去，而不是
        # 读成「这个功能坏了」。
        data = _Recorder()
        data._answer = None
        with self.assertRaises(NotImplementedError) as caught:
            data.get_turn_over_rate("600519.SH")
        text = str(caught.exception)
        self.assertIn("financial data", text)
        self.assertIn("download", text)


class ContractInfoWindowVolumeTest(unittest.TestCase):
    """内外盘：两个都必须透传。

    get_bvol 一度被判成「对任何代码都答 0」而拒绝转发 —— 那一轮取样全是收盘
    后只剩 15:00 集合竞价的股票/ETF（集合竞价一个价位撮合、没有主动方，整根
    落进单侧），换成连续交易到 15:30 的逆回购立刻两侧都有值：

        204001.SH  svol=43226876  bvol=1506858
        131810.SZ  svol=2898199   bvol=2404676
        511990.SH  svol=0         bvol=22883     <- 反过来，内盘才是 0

    0 是合法答案、没有哨兵可用，所以拒绝转发等于把一个能用的方法判死。
    """

    LIVE = (
        # (code, svol, bvol) —— 2026-09-09 收盘后实测
        ("204001.SH", 43226876, 1506858),
        ("131810.SZ", 2898199, 2404676),
        ("511990.SH", 0, 22883),
        ("601398.SH", 32586, 0),
    )

    def test_both_sides_forward_the_terminals_own_number(self):
        for code, svol, bvol in self.LIVE:
            for name, answer in (("get_svol", svol), ("get_bvol", bvol)):
                data = _Recorder()
                data._answer = answer
                got = getattr(data, name)(code)
                self.assertEqual(got, answer, "%s(%s)" % (name, code))
                self.assertEqual(data.calls, [(name, {"stock": code})],
                                 "%s(%s)" % (name, code))

    def test_a_zero_is_forwarded_not_turned_into_an_error(self):
        # 收盘后的股票外盘、511990.SH 的内盘都真的是 0，那是答案不是故障
        for name in ("get_svol", "get_bvol"):
            data = _Recorder()
            data._answer = 0
            self.assertEqual(getattr(data, name)("601398.SH"), 0, name)


class ContractMultiplierSentinelTest(unittest.TestCase):
    """合约乘数：先读回答案，对上 int32 哨兵就报错。

    实测这台终端对股票 / ETF / 期权 / 期货代码一律返回 2147483647。把它当
    乘数用会把下单金额算错 20 亿倍 —— 正是 CLAUDE.md 那条「写完读回来再说
    成功」的读侧版本。
    """

    def test_the_sentinel_raises_and_says_what_it_is(self):
        data = _Recorder()
        data._answer = 2147483647
        with self.assertRaises(NotImplementedError) as caught:
            data.get_contract_multiplier("IF2612.IF")
        text = str(caught.exception)
        self.assertIn("2147483647", text)
        self.assertIn("VolumeMultiple", text)   # 指向真能用的那条
        # 哨兵是回读出来的，所以这一次 RPC 必须真的发出去过
        self.assertEqual(
            data.calls, [("get_contract_multiplier", {"stockcode": "IF2612.IF"})])

    def test_a_real_multiplier_passes_through_untouched(self):
        # 有期货行情的终端上 IF 是 300、rb 是 10 —— 不能被这层拦掉
        for answer in (300, 10, 5, 1):
            data = _Recorder()
            data._answer = answer
            self.assertEqual(data.get_contract_multiplier("rb2701.SF"), answer)

    def test_a_non_numeric_answer_is_not_mistaken_for_the_sentinel(self):
        data = _Recorder()
        data._answer = "300"
        self.assertEqual(data.get_contract_multiplier("IF2612.IF"), "300")

        data = _Recorder()
        data._answer = None
        self.assertIsNone(data.get_contract_multiplier("IF2612.IF"))


class ShimReachesTheWrappersTest(unittest.TestCase):
    """报的人写的是 `from xtquant import xtdata`，所以这条路要真的走一遍。

    这里**调**，不 grep 源码：原来的版本断言 src/xtquant/xtdata.py 里有
    `def __getattr__` 和那行 `return getattr(...)`，两句在补包装之前就都在
    文件里 —— 测试是绿的，而 xtdata.get_open_date(...) 照样抛 AttributeError
    （CLAUDE.md：验证含义，不是形状）。

    走的是 _Recorder（BigQmtXtData 子类，_call 只记账），所以不需要活桥。
    """

    def setUp(self):
        from unittest import mock

        import bigqmt_signal_trader.xtquant_compat as _compat

        self.recorder = _Recorder()
        patcher = mock.patch.object(_compat, "xtdata", self.recorder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_reporters_import_path_resolves_and_forwards(self):
        from xtquant import xtdata as shim

        self.recorder._answer = 20010827
        self.assertEqual(shim.get_open_date("600519.SH"), 20010827)
        self.assertEqual(self.recorder.calls,
                         [("get_open_date", {"stock": "600519.SH"})])

    def test_the_whole_contract_row_is_callable_through_the_shim(self):
        from xtquant import xtdata as shim

        for name in README_CONTRACT_ROW:
            attr = getattr(shim, name)          # 补包装前这里就是 AttributeError
            self.assertTrue(callable(attr), name)

    def test_bvol_answers_through_the_shim_too(self):
        # 一度在这层被拒；实测 204001.SH -> 1506858
        from xtquant import xtdata as shim

        self.recorder._answer = 1506858
        self.assertEqual(shim.get_bvol("204001.SH"), 1506858)
        self.assertEqual(self.recorder.calls,
                         [("get_bvol", {"stock": "204001.SH"})])

    def test_a_refused_one_arrives_as_the_explained_error_not_as_none(self):
        from xtquant import xtdata as shim

        self.recorder._answer = None
        with self.assertRaises(NotImplementedError):
            shim.get_turn_over_rate("600519.SH")


if __name__ == "__main__":
    unittest.main()
