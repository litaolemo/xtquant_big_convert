# coding: utf-8
"""ROUTER 线程出错要自己重建，不能静悄悄死掉（#240）。

原来 `_router_loop` 是一层 try/finally：任何逃出内层 while 的异常都走到
finally 关掉 socket、线程结束。**桥从此不再接收任何请求，而且没有任何提示**
—— 从客户端看和「服务端死了」一模一样，可 QMT 里的策略还好好地跑着，adjust
照常打点、日志照常滚。这正是本仓反复吃亏的那种形态：失败看起来像没发生。

redis 那两条循环早就是「异常 -> 退避 -> 重建连接」的形状（`_listen_loop` /
`_queue_loop`），zmq 缺这一层。
"""
import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport  # noqa: E402


class _Boom(RuntimeError):
    pass


class RouterSelfHealTest(unittest.TestCase):
    """用打桩的 _router_session / _bind_configured_address 驱动重建逻辑。

    不起真 socket：这条要测的是**循环的控制流**，不是 zmq 本身。真 socket
    会把「重建了几次」变成时序问题，而时序问题的测试是不稳定的。
    """

    def _transport(self):
        transport = ZmqTransport.__new__(ZmqTransport)
        transport.print_prefix = "[test]"
        transport._running = True
        transport._actual_bind_address = "tcp://127.0.0.1:15560"
        transport.bind_address = "tcp://127.0.0.1:15560"
        return transport

    def test_a_crashed_session_is_rebuilt_not_abandoned(self):
        transport = self._transport()
        sessions = []
        rebinds = []

        def session():
            sessions.append(1)
            if len(sessions) < 3:
                raise _Boom("router died")
            transport._running = False      # 第三次正常收工

        transport._router_session = session
        transport._bind_configured_address = lambda: rebinds.append(1)

        started = time.time()
        transport._router_loop()

        self.assertEqual(len(sessions), 3, "崩了以后没有重建，线程直接结束了")
        self.assertEqual(len(rebinds), 2, "重建时没有重新 bind")
        self.assertLess(time.time() - started, 10, "退避太久")

    def test_backoff_grows_so_a_held_port_does_not_spam(self):
        """端口被别的进程占着时不能每秒刷屏。"""
        transport = self._transport()
        slept = []
        rounds = []

        def session():
            rounds.append(1)
            if len(rounds) >= 4:
                transport._running = False
            raise _Boom("still dead")

        transport._router_session = session
        transport._bind_configured_address = lambda: None
        real_sleep = time.sleep
        mine = threading.current_thread()
        try:
            # 只记录本线程的 sleep。这里替换的是**全局** time.sleep，套件里任何
            # 并发线程在这个窗口调一次，都会挤进 slept 把断言弄红 —— 实际发生过
            # 一次偶发失败。门禁里的偶发红比没有断言更糟：读的人会学会忽略红。
            time.sleep = lambda s: slept.append(s) if threading.current_thread() is mine else None
            transport._router_loop()
        finally:
            time.sleep = real_sleep

        self.assertGreaterEqual(len(slept), 3)
        self.assertEqual(slept[:3], [1.0, 2.0, 4.0], "退避没有翻倍：%s" % slept[:3])

    def test_backoff_is_capped(self):
        transport = self._transport()
        slept = []
        rounds = []

        def session():
            rounds.append(1)
            if len(rounds) >= 12:
                transport._running = False
            raise _Boom("still dead")

        transport._router_session = session
        transport._bind_configured_address = lambda: None
        real_sleep = time.sleep
        mine = threading.current_thread()
        try:
            # 只记录本线程的 sleep。这里替换的是**全局** time.sleep，套件里任何
            # 并发线程在这个窗口调一次，都会挤进 slept 把断言弄红 —— 实际发生过
            # 一次偶发失败。门禁里的偶发红比没有断言更糟：读的人会学会忽略红。
            time.sleep = lambda s: slept.append(s) if threading.current_thread() is mine else None
            transport._router_loop()
        finally:
            time.sleep = real_sleep

        self.assertLessEqual(max(slept), 30.0, "退避没有封顶：%s" % max(slept))

    def test_a_failed_rebind_keeps_retrying(self):
        """重建失败不能放弃 —— 放弃就回到了「线程静悄悄死掉」。"""
        transport = self._transport()
        attempts = []

        def session():
            raise _Boom("router died")

        def rebind():
            attempts.append(1)
            if len(attempts) >= 3:
                transport._running = False
            raise _Boom("port still held")

        transport._router_session = session
        transport._bind_configured_address = rebind
        real_sleep = time.sleep
        mine = threading.current_thread()
        try:
            time.sleep = lambda s: None if threading.current_thread() is mine else real_sleep(s)
            transport._router_loop()
        finally:
            time.sleep = real_sleep

        self.assertGreaterEqual(len(attempts), 3, "rebind 失败一次就不再重试了")

    def test_stop_does_not_trigger_a_rebuild(self):
        """正常停机不该被当成故障重连。"""
        transport = self._transport()
        rebinds = []

        def session():
            transport._running = False      # stop() 的效果

        transport._router_session = session
        transport._bind_configured_address = lambda: rebinds.append(1)
        transport._router_loop()

        self.assertEqual(rebinds, [], "正常停机也去重连了")

    def test_an_exception_after_stop_is_not_reported_as_failure(self):
        """stop() 让阻塞的 recv 抛异常 —— 那是停机路径，不是故障。

        redis 那边为这件事专门修过一次（#189）：正常停机打完整堆栈，
        会让每次重启都看着像故障，读的人于是学会跳过它 —— 包括真的那次。
        """
        transport = self._transport()
        rebinds = []

        def session():
            transport._running = False
            raise _Boom("socket closed under us by stop()")

        transport._router_session = session
        transport._bind_configured_address = lambda: rebinds.append(1)
        transport._router_loop()

        self.assertEqual(rebinds, [], "停机时的异常被当成故障去重连了")


class RouterSessionOwnershipTest(unittest.TestCase):
    """重连时不能把新建的 socket 关掉。"""

    def test_the_session_only_closes_its_socket_when_stopping(self):
        source = open(os.path.join(
            ROOT, "src", "bigqmt_signal_trader", "transports", "zmq_transport.py"),
            encoding="utf-8").read()
        session = source.split("def _router_session", 1)[1]
        session = session.split("\n    def ", 1)[0]
        self.assertIn("if not self._running:", session,
                      "_router_session 的 finally 无条件关 socket —— 重连路径上会把"
                      "刚建好的新 socket 关掉，让『重连成功』变成静默失效")


if __name__ == "__main__":
    unittest.main()
