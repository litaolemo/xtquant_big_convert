# coding: utf-8
"""#247 的两处收尾：期货首帧不能静默丢失，start() 不能盲等一秒。

#247 修的是真 bug —— 实测 get_full_tick 传 1000 只个股中位 9.5s、3000 只直接
TimeoutError，整段时间 QMT 的 adjust 线程被占住，drain 停摆、后续 RPC 全排队。
交易所整体 token 稳定 ~330ms，所以大列表走 token 是对的。

但它留下两个问题：

1. **期货会静默失去首帧。** 实测 SF / DF / ZF / IF / INE / GF 这些期货 token
   whole-market 查询**全部返回 0 条**（只有 SH / SZ / BJ 有数）。原实现把后缀
   一律当 token 用，于是 160 只期货代码 -> get_full_tick(['SF']) -> 0 条，
   再加上当时的 `if snapshot:` 判断，回调**根本不触发**。订阅看着是活的，首帧
   没了 —— 和 #95 同一个形状。

2. **subscribe() 里 time.sleep(1)**，作者注明「原因不明」。竞态是真的：
   `_start_event_listener` 起 daemon 线程就返回，pubsub.subscribe 之前发布的
   事件会丢。但盲等一秒给每个客户端启动都上税，且治的是症状。
"""
import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import BigQmtXtData, BigQmtXtTrader  # noqa: E402


class _Recorder(object):
    """记下每次 get_full_tick 收到什么，并按 token 能力返回。"""

    def __init__(self, whole_market_rows=None):
        self.calls = []
        self.whole = whole_market_rows or {}

    def __call__(self, codes, types=None):
        self.calls.append(list(codes))
        # 期货 token 在真终端返回 0 条 —— 照实模拟
        if all(str(c).upper() in ("SH", "SZ", "BJ") for c in codes):
            return dict(self.whole)
        if any(str(c).upper() in ("SF", "DF", "ZF", "IF", "INE", "GF") for c in codes):
            return {}
        return {str(c): {"lastPrice": 1.0} for c in codes}


def _data(recorder):
    data = BigQmtXtData.__new__(BigQmtXtData)
    data.get_full_tick = recorder
    return data


class PrimeSnapshotTest(unittest.TestCase):
    def test_a_small_list_still_goes_direct(self):
        rec = _Recorder()
        codes = ["600000.SH", "000001.SZ"]
        out = _data(rec)._prime_snapshot(codes)
        self.assertEqual(rec.calls, [codes], "小列表不该走交易所整体")
        self.assertEqual(sorted(out), sorted(codes))

    def test_a_large_stock_list_uses_whole_market_tokens(self):
        rows = {"600%03d.SH" % i: {"lastPrice": 1.0} for i in range(200)}
        rows.update({"000%03d.SZ" % i: {"lastPrice": 2.0} for i in range(200)})
        rec = _Recorder(whole_market_rows=rows)
        codes = sorted(rows)
        out = _data(rec)._prime_snapshot(codes)
        self.assertEqual(rec.calls, [["SH", "SZ"]],
                         "大列表该只发一次交易所整体查询，实际发了 %s" % rec.calls)
        self.assertEqual(sorted(out), sorted(codes))

    def test_a_large_futures_list_does_not_come_back_empty(self):
        """修正前这条是红的：期货 token 返回 0 条，首帧整个丢掉。"""
        codes = ["cu%d.SF" % n for n in range(2600, 2800)]
        rec = _Recorder()
        out = _data(rec)._prime_snapshot(codes)
        self.assertTrue(out, "期货大列表的首帧被静默丢空了")
        self.assertEqual(sorted(out), sorted(codes))

    def test_futures_codes_reach_qmt_verbatim(self):
        """大 QMT 有 cu2610.SF，没有 CU2610.SF（#58/#95）。"""
        codes = ["cu%d.SF" % n for n in range(2600, 2800)]
        rec = _Recorder()
        _data(rec)._prime_snapshot(codes)
        sent = [c for call in rec.calls for c in call]
        self.assertIn("cu2600.SF", sent,
                      "期货代码被改了大小写再发给 QMT：%s" % sent[:3])

    def test_a_mixed_list_splits_instead_of_losing_the_futures_half(self):
        stocks = ["600%03d.SH" % i for i in range(150)]
        futures = ["cu%d.SF" % n for n in range(2600, 2700)]
        rows = {c: {"lastPrice": 1.0} for c in stocks}
        rec = _Recorder(whole_market_rows=rows)
        out = _data(rec)._prime_snapshot(stocks + futures)
        for code in futures:
            self.assertIn(code, out, "混合列表把期货那半丢了")
        for code in stocks:
            self.assertIn(code, out, "混合列表把股票那半丢了")


class StartWaitsForReadinessTest(unittest.TestCase):
    def _trader(self):
        trader = BigQmtXtTrader.__new__(BigQmtXtTrader)
        trader._event_ready = threading.Event()
        trader.event_listener_ready_timeout = 1.0
        return trader

    def test_it_returns_as_soon_as_the_listener_subscribes(self):
        trader = self._trader()

        def subscribe_soon():
            time.sleep(0.05)
            trader._event_ready.set()

        threading.Thread(target=subscribe_soon, daemon=True).start()
        started = time.time()
        self.assertTrue(trader._await_event_listener())
        elapsed = time.time() - started
        self.assertLess(elapsed, 0.8,
                        "订阅早就就绪了还等了 %.2fs —— 那就是盲等" % elapsed)

    def test_it_is_bounded_when_the_listener_never_comes_up(self):
        trader = self._trader()
        started = time.time()
        self.assertFalse(trader._await_event_listener())
        elapsed = time.time() - started
        self.assertLess(elapsed, 2.0, "等待没有上界")
        self.assertGreaterEqual(elapsed, 0.9, "上界比原来的 sleep(1) 还短")

    def test_zero_disables_the_wait(self):
        trader = self._trader()
        trader.event_listener_ready_timeout = 0
        started = time.time()
        self.assertFalse(trader._await_event_listener())
        self.assertLess(time.time() - started, 0.2)

    def test_there_is_no_fixed_sleep_left_in_subscribe(self):
        """盲等一秒不能再回来。#247 把它加在 subscribe() 里，不是 start()。

        按 AST 查真实调用，不按文本 —— 解释这段历史的注释里就写着 time.sleep(1)，
        字符串匹配会被自己的注释绊倒。
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(BigQmtXtTrader.subscribe)))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Attribute):
                    base = target.value
                    name = getattr(base, "id", None)
                    called.add("%s.%s" % (name, target.attr) if name else target.attr)
                elif isinstance(target, ast.Name):
                    called.add(target.id)
        self.assertNotIn("time.sleep", called,
                         "subscribe() 里又出现了固定 sleep：%s" % sorted(called))
        self.assertIn("self._await_event_listener", called,
                      "subscribe() 没有等监听器就绪：%s" % sorted(called))


if __name__ == "__main__":
    unittest.main()
