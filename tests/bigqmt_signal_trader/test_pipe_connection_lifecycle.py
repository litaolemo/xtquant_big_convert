# coding: utf-8
"""#431：pipe 客户端按线程开的连接，线程退出时必须释放。

修复前两条泄漏路径，根子都是「句柄只随进程退出回收」：

* 线程退出不释放 —— 客户端每死一条线程，服务端白留一条连接
  （background 模式还搭一个工作线程）。churn 下去积到
  PIPE_UNLIMITED_INSTANCES=255 上限，之后谁 CreateFileW 都是
  PIPE_BUSY，表现为「再也连不上」。报告者说的「内存溢出」实际是
  服务端的线程/句柄耗尽。
* trader.stop() 不关底层 transport —— 守规矩的单线程客户端一样漏。
"""
import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports.pipe_transport import NamedPipeTransport  # noqa: E402
from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader  # noqa: E402


WINDOWS_ONLY = unittest.skipUnless(os.name == "nt", "命名管道是 Windows 专有")
ACCOUNT = "8886800503"


def _echo(request):
    return {
        "schema_version": 1,
        "request_id": request.get("request_id"),
        "method": request.get("method"),
        "ok": True,
        "data": {"echo": request.get("params")},
    }


def _wait_for(predicate, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@WINDOWS_ONLY
class ThreadExitReleaseTest(unittest.TestCase):
    """线程退出时，它那条连接两侧都要收掉。"""

    def _pair(self, name, background=True):
        server = NamedPipeTransport(account_id="t", pipe_name=name)
        server.start_receiving(_echo, background_threads=background)
        self.addCleanup(server.stop)
        time.sleep(0.3)
        client = NamedPipeTransport(account_id="t", pipe_name=name)
        self.addCleanup(client.stop)
        return server, client

    def _churn(self, client, count):
        def worker(i):
            client.send_request(
                {"request_id": "t%d" % i, "method": "p", "params": {}}, 5)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for t in threads:
            self.assertFalse(t.is_alive(), "请求线程没出来，别在卡死状态下做断言")

    def test_dead_threads_connections_are_released_on_both_sides(self):
        server, client = self._pair("bigqmt_test_431_exit")
        self._churn(client, 6)
        self.assertTrue(
            _wait_for(lambda: not client._client_threads, 5),
            "线程退出后注册表里还挂着 %d 条句柄" % len(client._client_threads))
        # 客户端句柄一关，服务端工作线程的 ReadFile 即报 broken pipe 自行回收；
        # 稳态 = accept 循环常驻的那一个待接实例。
        self.assertTrue(
            _wait_for(lambda: len(server._server_handles) == 1, 5),
            "服务端还留着 %d 条已死客户端的连接" % (len(server._server_handles) - 1))

    def test_drain_mode_server_recycles_the_connection_too(self):
        """drain 模式没有工作线程，泄漏落在 _drain_handles 里，同一个根因。"""
        server, client = self._pair("bigqmt_test_431_drain", background=False)
        done = []

        def worker(i):
            client.send_request(
                {"request_id": "t%d" % i, "method": "p", "params": {}}, 10)
            done.append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        # 没人 drain 就没人回答 —— 本测试扮演 adjust 线程持续轮询。
        self.assertTrue(
            _wait_for(lambda: (server.drain_request_queue(max_items=5),
                               len(done) == 3)[1], 10),
            "drain 轮询下请求没被回答")
        for t in threads:
            t.join(timeout=30)
        self.assertTrue(_wait_for(lambda: not client._client_threads, 5))

        def drained():
            server.drain_request_queue(max_items=5)
            return not server._drain_handles

        self.assertTrue(
            _wait_for(drained, 5),
            "drain 表还留着 %d 条已死客户端的连接" % len(server._drain_handles))

    def test_stop_closes_connections_opened_by_other_threads(self):
        _server, client = self._pair("bigqmt_test_431_stop")
        client.send_request({"request_id": "m", "method": "p", "params": {}}, 5)
        self.assertTrue(client._client_threads, "主线程的连接应该登记在册")
        client.stop()
        self.assertFalse(client._client_threads)

    def test_release_requires_the_exact_registration_token(self):
        """ident 和句柄值都会被系统回收复用，只认 token 全等才允许关。"""
        transport = NamedPipeTransport(account_id="t", pipe_name="bigqmt_test_431_token")
        self.addCleanup(transport.stop)
        ident = 424242
        token = object()
        transport._client_threads[ident] = (123456, token)
        transport._release_thread_handle(ident, object())      # 别人的 token
        self.assertIn(ident, transport._client_threads,
                      "token 不符也关了 —— 会错杀被复用 ident 上的活句柄")
        transport._release_thread_handle(ident, token)
        self.assertNotIn(ident, transport._client_threads)

    def test_sweep_collects_entries_whose_finalizer_never_ran(self):
        transport = NamedPipeTransport(account_id="t", pipe_name="bigqmt_test_431_sweep")
        self.addCleanup(transport.stop)
        dead_ident = 99999999          # 不属于任何活线程
        transport._client_threads[dead_ident] = (123456, object())
        me = threading.current_thread().ident
        transport._client_threads[me] = (123457, object())
        try:
            transport._sweep_dead_thread_handles()
            self.assertNotIn(dead_ident, transport._client_threads)
            self.assertIn(me, transport._client_threads,
                          "清扫把活线程自己的条目也收了")
        finally:
            transport._client_threads.pop(me, None)


class TraderStopClosesTransportTest(unittest.TestCase):
    """trader.stop() 必须把底层 RPC transport 一起关掉（#431 的第二半）。"""

    def test_stop_closes_the_clients_transport(self):
        trader = BigQmtXtTrader(account_id=ACCOUNT)
        calls = []

        class _FakeTransport(object):
            def stop(self):
                calls.append(1)

        trader.client._transport_instance = _FakeTransport()
        trader.stop()
        self.assertEqual(calls, [1], "trader.stop() 没有关底层 transport")
        self.assertIsNone(trader.client._transport_instance,
                          "关掉之后还捏着旧 transport，下次用的是条死线")

    def test_close_without_a_transport_is_a_no_op(self):
        trader = BigQmtXtTrader(account_id=ACCOUNT)
        trader.client.close()      # transport 从未构建
        trader.client.close()      # 第二次同样安全

    def test_the_transport_is_rebuilt_lazily_after_close(self):
        trader = BigQmtXtTrader(account_id=ACCOUNT)
        trader.client.transport_name = "pipe"
        first = trader.client._transport()
        trader.client.close()
        second = trader.client._transport()
        self.assertIsNot(first, second, "close 之后复用了已关闭的 transport")
        trader.client.close()


if __name__ == "__main__":
    unittest.main()
