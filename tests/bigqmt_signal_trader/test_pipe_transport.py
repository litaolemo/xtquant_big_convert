# coding: utf-8
"""Windows 命名管道传输。

存在的理由是依赖，不是速度：有些券商 QMT 的导入白名单拒 ``socket``（还包括
``logging.handlers`` 间接引入的那次），且不许往自带 Python 里 pip install ——
那种终端上 redis 客户端和 pyzmq 都装不进去，桥根本没有线可用。命名管道走
``ctypes.WinDLL("kernel32")``，是标准库、也不是套接字。

**QMT 沙箱内实测放行**：``import ctypes`` / ``from ctypes import wintypes`` /
``WinDLL("kernel32")`` / 六个 kernel32 函数解析 / ``CreateNamedPipeW`` 真建出
管道内核对象，全部通过（probe_capabilities 的 ctypes_probe 一项）。

速度实测（同机、108 字节载荷）::

    裸 IPC 往返        命名管道 0.012ms   zmq 0.109ms   redis 0.798ms
    完整传输层         命名管道 0.031ms                 redis ~0.81ms
    端到端 RPC（活桥）  3~12ms

传输层快 26 倍，但端到端只省 0.8ms —— 瓶颈在桥内处理，不在线缆。这一点要写在
这里，免得有人拿裸 IPC 的 66 倍去做架构决策。
"""
import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports.base import TransportError
from bigqmt_signal_trader.transports.factory import KNOWN_TRANSPORTS, build_transport
from bigqmt_signal_trader.transports.pipe_transport import (
    NamedPipeTransport, pipe_path,
)


WINDOWS_ONLY = unittest.skipUnless(os.name == "nt", "命名管道是 Windows 专有")


def _echo(request):
    return {
        "schema_version": 1,
        "request_id": request.get("request_id"),
        "method": request.get("method"),
        "ok": True,
        "data": {"echo": request.get("params")},
    }


class _Pair(object):
    """一对已连上的服务端/客户端，测完保证拆干净。"""

    def __init__(self, name, on_request=_echo):
        self.server = NamedPipeTransport(account_id="t", pipe_name=name)
        self.server.start_receiving(on_request)
        time.sleep(0.3)
        self.client = NamedPipeTransport(account_id="t", pipe_name=name)

    def close(self):
        self.server.stop()
        self.client.stop()


class PipePathTest(unittest.TestCase):

    def test_the_account_id_is_part_of_the_pipe_name(self):
        """两个桥 —— 一个实盘一个模拟 —— 绝不能共用一条线。

        同一个错误让两个部署共用过一个日志文件（#144），那次的代价是
        TimedRotatingFileHandler 永远轮转不了、日志无限增长。
        """
        live = pipe_path("bigqmt_rpc", "8886800503")
        sim = pipe_path("bigqmt_rpc", "5500000001")
        self.assertNotEqual(live, sim)
        self.assertIn("8886800503", live)

    def test_no_account_id_still_yields_a_usable_path(self):
        self.assertTrue(pipe_path("bigqmt_rpc", "").endswith("bigqmt_rpc"))


class ServiceContractTest(unittest.TestCase):
    """传输要能被服务端那套 drain/inline 机制驱动，不只是自己能收发。"""

    def test_it_has_drain_request_queue(self):
        """少了这个方法，adjust 每个 tick 都会撞 Redis 兜底分支。

        服务端 drain_request_queue 的兜底是 listen_redis.lpop，而 pipe 部署上
        listen_redis 是 None —— 实盘上每 100ms 抛一次
        AttributeError: 'NoneType' object has no attribute 'lpop'。
        单测抓不到，只有端到端能暴露，所以这条要钉住。
        """
        transport = build_transport("pipe", {}, account_id="acct")
        self.assertTrue(callable(getattr(transport, "drain_request_queue", None)))
        self.assertEqual(transport.drain_request_queue(max_items=20), 0)

    def test_every_transport_the_factory_builds_can_be_drained(self):
        """同一个坑不要在下一个传输上重犯。"""
        for name in ("redis", "zmq", "pipe"):
            try:
                transport = build_transport(name, {}, account_id="acct")
            except Exception:
                continue          # 缺依赖的传输跳过，不是本条要测的
            self.assertTrue(
                callable(getattr(transport, "drain_request_queue", None)),
                "%s 传输没有 drain_request_queue，adjust 会掉进 redis 兜底" % name)


@WINDOWS_ONLY
class DrainModeTest(unittest.TestCase):
    """drain 模式：adjust 线程自己非阻塞轮询，不起工作线程。

    background_threads=True 时实测请求要 ~200ms 才到达 handler，而 handler
    本身 0.0ms —— 和 #183 记录的 zmq 404ms 同一个形状。#183 的解法是让
    adjust 线程自己拉，去掉跨线程交接；这里给 pipe 补上同一条路。
    """

    def _pair(self, name):
        srv = NamedPipeTransport(account_id="d", pipe_name=name)
        srv.start_receiving(_echo, background_threads=False)
        time.sleep(0.3)
        cli = NamedPipeTransport(account_id="d", pipe_name=name)
        self.addCleanup(srv.stop)
        self.addCleanup(cli.stop)
        return srv, cli

    def test_nothing_is_answered_until_the_adjust_thread_drains(self):
        """没人调 drain 就没人回答 —— 这正是 drain 模式的定义。"""
        srv, cli = self._pair("bigqmt_test_drain_wait")
        answers = []
        t = threading.Thread(target=lambda: answers.append(
            cli.send_request({"request_id": "d1", "method": "p", "params": {}}, 10)))
        t.daemon = True
        t.start()
        time.sleep(0.6)
        self.assertEqual(answers, [], "没 drain 就被回答了，说明还在走工作线程")
        self.assertGreater(srv.drain_request_queue(max_items=20), 0)
        t.join(timeout=5)
        self.assertEqual(answers[0]["request_id"], "d1")

    def test_the_service_can_answer_through_send_response(self):
        """服务端不走 deliver 的返回值，它自己调 send_response 发响应。

        drain 模式下没有工作线程，也就没有 outbox —— 第一版 send_response
        强制要求 outbox，于是抛「no pipe handle」，客户端只看到超时。实盘上
        就是这么卡住的：background_threads=False 一生效，一条都答不出来。
        """
        answers = []
        holder = {}

        def capture(req):
            holder["req"] = req
            return None                      # 模拟服务端：自己发，不靠返回值

        srv = NamedPipeTransport(account_id="d", pipe_name="bigqmt_test_drain_sr")
        srv.start_receiving(capture, background_threads=False)
        self.addCleanup(srv.stop)
        time.sleep(0.3)
        cli = NamedPipeTransport(account_id="d", pipe_name="bigqmt_test_drain_sr")
        self.addCleanup(cli.stop)

        t = threading.Thread(target=lambda: answers.append(
            cli.send_request({"request_id": "s1", "method": "p", "params": {}}, 10)))
        t.daemon = True
        t.start()
        time.sleep(0.5)
        srv.drain_request_queue(max_items=5)
        srv.send_response(holder["req"], {"request_id": "s1", "ok": True, "data": {"v": 1}})
        t.join(timeout=5)
        self.assertEqual(answers[0]["data"]["v"], 1)

    def test_drain_is_non_blocking_when_idle(self):
        """空闲时 drain 必须立刻返回 —— 它跑在 adjust 主线程上。"""
        srv, _cli = self._pair("bigqmt_test_drain_idle")
        started = time.time()
        for _ in range(5):
            self.assertEqual(srv.drain_request_queue(max_items=20), 0)
        self.assertLess(time.time() - started, 0.5, "drain 在空闲时阻塞了")

    def test_drain_respects_max_items(self):
        srv, cli = self._pair("bigqmt_test_drain_cap")
        for i in range(6):
            t = threading.Thread(target=lambda i=i: cli.send_request(
                {"request_id": "c%d" % i, "method": "p", "params": {}}, 10))
            t.daemon = True
            t.start()
        time.sleep(0.6)
        self.assertLessEqual(srv.drain_request_queue(max_items=2), 2)

    def test_background_mode_leaves_drain_a_no_op(self):
        """开着工作线程时 drain 不能插手，否则两边抢同一个句柄。"""
        srv = NamedPipeTransport(account_id="d", pipe_name="bigqmt_test_drain_bg")
        srv.start_receiving(_echo, background_threads=True)
        self.addCleanup(srv.stop)
        time.sleep(0.3)
        self.assertEqual(srv.drain_request_queue(max_items=20), 0)


class BackgroundThreadResolutionTest(unittest.TestCase):
    """新增传输时，「谁能走 drain」不能靠一张手写名单。

    这张名单原来硬编码成 ("zmq", "mysql")，pipe 加进来时没人记得改它 ——
    于是配置里写了 rpc_background_threads=False，日志里却是 True，drain 从
    没跑起来，而我还拿那组数字下了「drain 没用」的结论。和入口文件手抄
    QMT 全局函数名单（#202）是同一类错误。
    """

    def test_pipe_may_opt_into_drain(self):
        from bigqmt_signal_trader_strategy import _resolve_background_threads
        self.assertFalse(_resolve_background_threads("pipe", False))
        self.assertTrue(_resolve_background_threads("pipe", True))

    def test_a_transport_without_a_real_drain_keeps_its_thread(self):
        """没实现 drain 的传输必须保留接收线程，否则一条请求都收不到。"""
        from bigqmt_signal_trader_strategy import _resolve_background_threads
        self.assertTrue(_resolve_background_threads("shm", False))

    def test_the_decision_asks_the_transport_not_a_list(self):
        from bigqmt_signal_trader_strategy import _transport_can_drain
        for name in ("zmq", "mysql", "pipe"):
            self.assertTrue(_transport_can_drain(name), name)
        self.assertFalse(_transport_can_drain("shm"))
        self.assertFalse(_transport_can_drain("nonesuch"))

    def test_unset_means_the_adjust_drain(self):
        # #343: pipe can drain, so an absent key drains (94ms vs 189ms with
        # the receiver thread on the 0.3.28 table). The historical default
        # -- receiver thread on -- is only kept for transports that cannot.
        from bigqmt_signal_trader_strategy import _resolve_background_threads
        self.assertFalse(_resolve_background_threads("pipe", None))
        self.assertTrue(_resolve_background_threads("pipe", True))


class FactoryTest(unittest.TestCase):

    def test_pipe_is_a_known_transport(self):
        self.assertIn("pipe", KNOWN_TRANSPORTS)

    def test_the_factory_builds_it(self):
        transport = build_transport("pipe", {}, account_id="acct")
        self.assertEqual(transport.name, "pipe")
        self.assertIn("acct", transport.path)

    def test_config_can_override_the_pipe_name(self):
        transport = build_transport(
            "pipe", {"pipe": {"pipe_name": "custom_wire"}}, account_id="acct")
        self.assertIn("custom_wire", transport.path)


@WINDOWS_ONLY
class RoundTripTest(unittest.TestCase):

    def setUp(self):
        self.pair = _Pair("bigqmt_test_roundtrip")
        self.addCleanup(self.pair.close)

    def test_a_request_comes_back_with_its_own_request_id(self):
        out = self.pair.client.send_request(
            {"request_id": "r1", "method": "ping", "params": {"a": 1}}, 5)
        self.assertEqual(out["request_id"], "r1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["echo"], {"a": 1})

    def test_utf8_survives_both_directions(self):
        out = self.pair.client.send_request(
            {"request_id": "r2", "method": "x",
             "params": {"名称": "维持担保比例", "值": 3.35}}, 5)
        self.assertEqual(out["data"]["echo"]["名称"], "维持担保比例")
        self.assertEqual(out["data"]["echo"]["值"], 3.35)

    def test_a_whole_market_payload_survives(self):
        """全推订阅一帧是 26800 个代码 —— 消息模式下不能被截断。"""
        codes = ["%06d.SH" % i for i in range(26800)]
        out = self.pair.client.send_request(
            {"request_id": "r3", "method": "big", "params": {"codes": codes}}, 30)
        self.assertEqual(len(out["data"]["echo"]["codes"]), 26800)
        self.assertEqual(out["data"]["echo"]["codes"][-1], codes[-1])

    def test_floats_keep_their_precision(self):
        out = self.pair.client.send_request(
            {"request_id": "r4", "method": "x",
             "params": {"price": 13.981600016666668}}, 5)
        self.assertEqual(out["data"]["echo"]["price"], 13.981600016666668)


@WINDOWS_ONLY
class HandlerFailureTest(unittest.TestCase):
    """handler 抛异常不能把连接搞死 —— 后面的请求还得能走。"""

    def setUp(self):
        def boom(request):
            if request.get("method") == "boom":
                raise RuntimeError("handler exploded")
            return _echo(request)

        self.pair = _Pair("bigqmt_test_boom", on_request=boom)
        self.addCleanup(self.pair.close)

    def test_the_error_comes_back_as_a_response_not_a_dropped_connection(self):
        out = self.pair.client.send_request(
            {"request_id": "e1", "method": "boom", "params": {}}, 5)
        self.assertFalse(out["ok"])
        self.assertIn("handler exploded", out["error"])

    def test_the_connection_still_works_afterwards(self):
        self.pair.client.send_request(
            {"request_id": "e1", "method": "boom", "params": {}}, 5)
        out = self.pair.client.send_request(
            {"request_id": "e2", "method": "fine", "params": {"ok": 1}}, 5)
        self.assertTrue(out["ok"])


@WINDOWS_ONLY
class ConcurrencyTest(unittest.TestCase):
    """每个线程一条自己的管道句柄。

    共用一个句柄会把所有调用方串行化 —— 正是 #186 给 ZMQ DEALER 修掉的那个
    bug，不能在新传输上重犯。串包（拿到别人的 request_id）比慢更严重。
    """

    def setUp(self):
        self.pair = _Pair("bigqmt_test_concurrent")
        self.addCleanup(self.pair.close)

    def test_twenty_threads_never_cross_responses(self):
        errors = []

        def worker(wid):
            for i in range(25):
                rid = "w%d-%d" % (wid, i)
                try:
                    out = self.pair.client.send_request(
                        {"request_id": rid, "method": "p",
                         "params": {"w": wid, "i": i}}, 15)
                except Exception as exc:
                    errors.append((rid, repr(exc)))
                    continue
                if out["request_id"] != rid:
                    errors.append(("串包", rid, out["request_id"]))
                elif out["data"]["echo"]["w"] != wid:
                    errors.append(("载荷串了", rid))

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors[:5], [], "500 次并发里出了 %d 个错" % len(errors))

    def test_each_thread_gets_its_own_handle(self):
        seen = []
        lock = threading.Lock()

        def grab():
            self.pair.client.send_request(
                {"request_id": "h", "method": "p", "params": {}}, 5)
            with lock:
                seen.append(self.pair.client._client_local.handle)

        threads = [threading.Thread(target=grab) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(seen), 6)
        self.assertEqual(len(set(seen)), 6, "线程之间共用了句柄")


@WINDOWS_ONLY
class ShutdownTest(unittest.TestCase):
    """stop() 曾经死锁，而且是必然死锁，不是偶发。

    工作线程阻塞在 ReadFile 等下一个请求，主线程调 DisconnectNamedPipe ——
    而 DisconnectNamedPipe 会等同一句柄上挂起的同步 ReadFile 完成。两边互等。
    线程栈实锤，修法是先 CancelIoEx 取消挂起的 I/O。
    """

    def test_stop_returns_even_with_idle_connections_parked_in_readfile(self):
        pair = _Pair("bigqmt_test_shutdown")
        for w in range(8):
            threading.Thread(
                target=lambda: pair.client.send_request(
                    {"request_id": "s%d" % w, "method": "p", "params": {}}, 10)
            ).start()
        time.sleep(1.0)

        done = threading.Event()

        def stopper():
            pair.close()
            done.set()

        threading.Thread(target=stopper, daemon=True).start()
        self.assertTrue(done.wait(timeout=20),
                        "stop() 又卡住了 —— 检查 CancelIoEx 还在不在")

    def test_stop_is_safe_to_call_twice(self):
        pair = _Pair("bigqmt_test_double_stop")
        pair.close()
        pair.close()


@WINDOWS_ONLY
class ClientWithoutServerTest(unittest.TestCase):

    def test_connecting_to_a_dead_pipe_says_what_to_check(self):
        client = NamedPipeTransport(
            account_id="nobody", pipe_name="bigqmt_test_absent",
            connect_timeout_seconds=0.3)
        self.addCleanup(client.stop)
        with self.assertRaises(TransportError) as caught:
            client.send_request({"request_id": "x", "method": "ping"}, 2)
        # 「连不上」要指向该查的地方，不能只报一个 errno
        self.assertIn("transport=pipe", str(caught.exception))


class NonWindowsTest(unittest.TestCase):

    @unittest.skipIf(os.name == "nt", "这条测的是非 Windows 上的报错")
    def test_it_fails_fast_with_a_clear_message(self):
        client = NamedPipeTransport(account_id="x")
        with self.assertRaises(TransportError) as caught:
            client.send_request({"request_id": "x", "method": "ping"}, 1)
        self.assertIn("Windows-only", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
