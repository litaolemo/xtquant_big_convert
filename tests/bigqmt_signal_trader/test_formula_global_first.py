# coding: utf-8
"""call_formula 一族优先走注入的运行时全局，其次才看 ContextInfo (#374)。

官方入口是注入策略命名空间的全局函数；完整大 QMT 的 ContextInfo 上没有
它们。0.3.56 之前适配器只查 ContextInfo，于是一台一切正常的完整大 QMT
收到的是「ContextInfo.call_formula is not available」——失败发生在方法
查找，根本没进公式计算。reporter 的关键验证（空 ContextInfo + 显式传入
qmt_api callable，该 callable 仍不被执行）就是这里的第一个用例。
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider


class EmptyContext(object):
    """A ContextInfo with none of the formula family on it."""

    pass


class Recorder(object):
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"stub": name}
        return _method


def _provider(context, qmt_api):
    return BigQmtMarketDataProvider(context_info=context, qmt_api=qmt_api)


class GlobalIsPreferredTest(unittest.TestCase):
    def test_call_formula_invokes_the_injected_global(self):
        """The reporter's repro: empty ContextInfo, qmt_api carries the
        callable -- it MUST be the one invoked."""
        globals_ = Recorder()
        provider = _provider(EmptyContext(), {"call_formula": globals_.call_formula})

        result = provider.call_formula("MA", "000300.SH", "1m", count=1,
                                       dividend_type="none", extend_param={})

        self.assertEqual({"stub": "call_formula"}, result)
        name, args, _kw = globals_.calls[0]
        self.assertEqual("call_formula", name)
        self.assertEqual(("MA", "000300.SH", "1m", "", "", 1, "none", {}), args)

    def test_the_whole_family_prefers_globals(self):
        globals_ = Recorder()
        provider = _provider(EmptyContext(), {
            "call_formula": globals_.call_formula,
            "subscribe_formula": globals_.subscribe_formula,
            "unsubscribe_formula": globals_.unsubscribe_formula,
            "get_formula_result": globals_.get_formula_result,
            "gen_factor_index": globals_.gen_factor_index,
        })

        provider.call_formula("MA", "000300.SH", "1m")
        provider.subscribe_formula("MA", "000300.SH", "1m")
        provider.unsubscribe_formula("req-1")
        provider.get_formula_result("req-1")
        provider.gen_factor_index("close", "MA", [], ["000300.SH"])

        self.assertEqual(
            ["call_formula", "subscribe_formula", "unsubscribe_formula",
             "get_formula_result", "gen_factor_index"],
            [name for name, _args, _kw in globals_.calls])


class ContextFallbackStillWorksTest(unittest.TestCase):
    def test_no_global_falls_back_to_contextinfo(self):
        context = Recorder()
        provider = _provider(context, {})

        result = provider.call_formula("MA", "000300.SH", "1m", count=1)

        self.assertEqual({"stub": "call_formula"}, result)
        name, args, _kw = context.calls[0]
        self.assertEqual("call_formula", name)

    def test_neither_raises_a_clear_error(self):
        provider = _provider(EmptyContext(), {})

        with self.assertRaises(NotImplementedError) as caught:
            provider.call_formula("MA", "000300.SH", "1m")
        self.assertIn("call_formula", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
