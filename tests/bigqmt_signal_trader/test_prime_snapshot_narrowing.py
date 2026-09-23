# coding: utf-8
"""#358：>100 只订阅时，首帧打底按交易所 token 取数，但没说要什么品种。

服务端对 token 的处理是 `wanted = types or DEFAULT_TICK_TYPES`，默认 ("stock",)，
于是 `SH` 被展开成板块「上证A股」—— 只有 A 股。推送侧是 ContextInfo 自己的
subscribe_whole_quote，压根不 narrowing。**推的是全品种，打底的只有 A 股**，
所以可转债 / 基金 / ETF / 指数在 >100 只订阅里没有首帧，而且不报错：增量推送
后来又把它们推出来了，消费者看到的是「先缺失后突然出现」。

≤100 只走的是另一条路（`get_full_tick(codes, types=["all"])`），所以把列表裁短
同样的转债就正常了 —— 这正是这个 bug 难看出来的地方。

修的方向不是改成 types=["all"]：那是 #247/#104 拿掉的那次 26744 个标的、7.5s
占住 adjust 线程的全市场读。按请求的代码段推断品种，判不出来的代码不猜，落到
逐码直读那条路上。
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import BigQmtXtData  # noqa: E402


WHOLE_MARKET_TOKENS = ("SH", "SZ", "BJ")
# 服务端 DEFAULT_TICK_TYPES，客户端不传 types 时就是它。
SERVER_DEFAULT_TYPES = ("stock",)


class _NarrowingRecorder(object):
    """照服务端的规矩回答 token 查询：只给 types 里点了名的品种。

    这是这条测试的全部要害 —— 旧的 recorder 对 token 查询一律返回全部行，
    无论 types 是什么，所以 narrowing 掉东西它看不见。
    """

    def __init__(self, rows):
        #: {code: 品种}，品种用服务端 SECTOR_BY_TYPE 的键
        self.rows = dict(rows)
        self.calls = []

    def __call__(self, codes, types=None):
        asked = [str(c) for c in codes]
        self.calls.append((asked, list(types) if types else None))
        tokens = [c.upper() for c in asked]
        if not all(t in WHOLE_MARKET_TOKENS for t in tokens):
            return {c: {"lastPrice": 1.0} for c in asked}
        wanted = [str(t).lower() for t in (types or SERVER_DEFAULT_TYPES)]
        everything = "all" in wanted
        return {
            code: {"lastPrice": 1.0}
            for code, kind in self.rows.items()
            if code.rsplit(".", 1)[-1].upper() in tokens
            and (everything or kind in wanted)
        }

    def token_types(self):
        """每次 token 查询带的 types。"""
        return [types for asked, types in self.calls
                if all(str(c).upper() in WHOLE_MARKET_TOKENS for c in asked)]

    def direct_calls(self):
        return [(asked, types) for asked, types in self.calls
                if not all(str(c).upper() in WHOLE_MARKET_TOKENS for c in asked)]


def _data(recorder):
    data = BigQmtXtData.__new__(BigQmtXtData)
    data.get_full_tick = recorder
    return data


def _stocks(n=150):
    return {"600%03d.SH" % i: "stock" for i in range(n)}


class PrimeSnapshotNarrowingTest(unittest.TestCase):
    def test_convertibles_are_in_the_first_frame_above_the_threshold(self):
        """修正前这条是红的：113050.SH 被 narrowing 到「上证A股」之外了。"""
        rows = _stocks()
        rows["113050.SH"] = "convertible"
        rec = _NarrowingRecorder(rows)

        out = _data(rec)._prime_snapshot(sorted(rows))

        self.assertIn("113050.SH", out,
                      "可转债没进首帧；订阅推送里却有它 —— 就是 #358")
        self.assertEqual(sorted(out), sorted(rows), "首帧还漏了别的")

    def test_funds_etfs_and_indices_are_in_the_first_frame_too(self):
        rows = _stocks()
        rows.update({
            "510300.SH": "etf",
            "000300.SH": "index",
            "159915.SZ": "etf",
            "399006.SZ": "index",
            "000001.SZ": "stock",
            "123456.SZ": "convertible",
        })
        rec = _NarrowingRecorder(rows)

        out = _data(rec)._prime_snapshot(sorted(rows))

        for code in ("510300.SH", "000300.SH", "159915.SZ", "399006.SZ",
                     "123456.SZ"):
            self.assertIn(code, out, "%s 没进首帧" % code)

    def test_a_plain_stock_list_still_only_asks_for_stocks(self):
        """别用 types=['all'] 图省事 —— 那是 #247/#104 拿掉的 7.5s 全市场读。"""
        rows = _stocks()
        rec = _NarrowingRecorder(rows)

        _data(rec)._prime_snapshot(sorted(rows))

        self.assertEqual(rec.token_types(), [["stock"]],
                         "纯股票列表把请求放宽了：%s" % rec.token_types())

    def test_an_unclassifiable_code_goes_direct_instead_of_being_dropped(self):
        """899050.BJ 是北证50，「沪深指数」板块不含它 —— 判不出来就不要猜。"""
        rows = _stocks()
        rows["899050.BJ"] = "index"
        rec = _NarrowingRecorder(rows)

        out = _data(rec)._prime_snapshot(sorted(rows))

        self.assertIn("899050.BJ", out, "判不出品种的代码被静默丢了")
        direct = rec.direct_calls()
        self.assertTrue(direct, "该落到逐码直读，实际一次都没有")
        self.assertIn("899050.BJ", [c for asked, _ in direct for c in asked])
        for asked, types in direct:
            self.assertEqual(types, ["all"], "逐码直读也 narrowing 了")

    def test_futures_still_bypass_the_token_path(self):
        """期货 token 在真终端返回 0 条（#247），它们本来就不该走 token。"""
        codes = ["cu%d.SF" % n for n in range(2600, 2800)]
        rec = _NarrowingRecorder({})

        out = _data(rec)._prime_snapshot(codes)

        self.assertEqual(sorted(out), sorted(codes))
        self.assertEqual(rec.token_types(), [], "期货被当成交易所 token 查了")


class PrimeTickTypesTest(unittest.TestCase):
    """品种推断本身。猜错一条就是又一个静默缺失，所以逐段钉住。"""

    def test_known_segments(self):
        from bigqmt_signal_trader.xtquant_compat import _prime_tick_types

        cases = {
            "600000.SH": ("stock",),
            "601318.SH": ("stock",),
            "603000.SH": ("stock",),
            "605500.SH": ("stock",),
            "688111.SH": ("stock",),
            "510300.SH": ("fund", "etf"),
            "000001.SH": ("index",),
            "113050.SH": ("convertible",),
            "110059.SH": ("convertible",),
            "118000.SH": ("convertible",),
            "132018.SH": ("convertible",),
            "000001.SZ": ("stock",),
            "002415.SZ": ("stock",),
            "300750.SZ": ("stock",),
            # 实测「深证A股」板块里已经有 302 段了
            "302132.SZ": ("stock",),
            "159915.SZ": ("fund", "etf"),
            "399006.SZ": ("index",),
            "123456.SZ": ("convertible",),
            "430047.BJ": ("stock",),
            "830799.BJ": ("stock",),
        }
        for code, expected in sorted(cases.items()):
            self.assertEqual(_prime_tick_types(code), expected, code)

    def test_unknown_segments_refuse_to_guess(self):
        from bigqmt_signal_trader.xtquant_compat import _prime_tick_types

        for code in ("899050.BJ",     # 北证50，沪深指数板块不含
                     "980001.SZ",     # 深证的另一套指数段，同样不在沪深指数里
                     "900901.SH",     # B 股
                     "200011.SZ",     # 深 B
                     "cu2610.SF",     # 期货
                     "10007304.SHO",  # 期权
                     "600000",        # 没有后缀，不知道是哪个市场
                     ""):
            self.assertEqual(_prime_tick_types(code), (),
                             "%s 被猜了一个品种出来" % code)


if __name__ == "__main__":
    unittest.main()
