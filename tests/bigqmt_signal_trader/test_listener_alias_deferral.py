# coding: utf-8
"""别名也算 trade-context 方法，不能靠拼写绕过延后派发（#252）。

#244/#248 把 `LISTENER_DEFERRED_METHODS` 的减法提到了展开的最后一步，但减的
是 **canonical 名**，而 `_expand_listener_methods` 同时把**原始别名**也加进了
结果里：

    methods.add("query_stock_positions")   # 别名，留下了
    methods.add("get_positions")           # canonical，被减掉了

于是 `rpc_listener_methods=("query_stock_positions",)` 里那个别名活了下来，
`_should_process_in_listener` 又是**先拿原始方法名直接匹配**，一命中就返回
True —— POSITION 查询回到收包线程。`("get_positions",)` 和 `("*",)` 都是对的，
只有别名这条路漏了，所以问题看起来像「配置写法不同结果不同」。

为什么是静默的：离开主策略线程后 `get_trade_detail_data` 返回的不是异常，
是**行数对、字段全 None** 的对象，客户端读成「这账户没钱 / 没持仓」，不是
「调用失败」（#244 的已知事实）。

上一版回归测试（test_listener_methods_never_leak_deferred.py）没照到这里，
因为它把 `service.handlers` 置成 None —— 那样 `_canonical_method` 退化成恒等
函数，别名根本没被解析。这里用**真的 handlers**，别名才会走到实际那条路上。
"""
import os
import sys
import threading
import unittest

from types import SimpleNamespace
from unittest.mock import MagicMock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    LISTENER_DEFERRED_METHODS,
    METHOD_ALIASES,
    BigQmtRpcHandlers,
    RedisPubSubRpcService,
)


DEFERRED_ALIASES = sorted(
    alias for alias, canonical in METHOD_ALIASES.items()
    if canonical in LISTENER_DEFERRED_METHODS
)


def _handlers(position_provider=None):
    """真的 handlers —— 别名解析必须是真的，否则测不到这个缺口。"""
    return BigQmtRpcHandlers(
        account_id="test",
        market_data=None,
        position_provider=position_provider,
    )


def _service(listener_methods, handlers=None):
    return RedisPubSubRpcService(
        redis_client=MagicMock(),
        handlers=handlers if handlers is not None else _handlers(),
        account_id="test",
        process_in_listener=True,
        listener_methods=listener_methods,
        background_threads=True,
    )


class DeferredAliasesAreNotDispatchableInlineTest(unittest.TestCase):
    def test_there_is_something_to_test(self):
        """别名表里确实有指向 deferred 方法的条目，否则下面几条是空转。"""
        self.assertIn("query_stock_positions", DEFERRED_ALIASES)
        self.assertIn("query_stock_asset", DEFERRED_ALIASES)

    def test_naming_a_deferred_alias_does_not_put_it_in_listener_methods(self):
        """修复前是红的：别名留在展开结果里。"""
        for alias in DEFERRED_ALIASES:
            expanded = _service((alias,)).listener_methods
            self.assertNotIn(
                alias, expanded,
                "别名 %s 留在了 listener_methods 里；它的 canonical 名 %s 是 "
                "trade-context 方法，跑在收包线程上会返回字段全 None 的空壳"
                % (alias, METHOD_ALIASES[alias]))

    def test_a_deferred_alias_is_never_processed_in_the_listener(self):
        """修复前是红的：_should_process_in_listener 先按原始名直接命中。"""
        for alias in DEFERRED_ALIASES:
            service = _service((alias,))
            self.assertFalse(
                service._should_process_in_listener({"method": alias}),
                "%s 被派发到收包线程了" % alias)

    def test_the_guard_holds_even_if_listener_methods_is_forced(self):
        """第二道闸：就算别名被硬塞进 listener_methods，也不许 inline。

        展开是配置期的一次性动作，派发是每个请求都走的路径。把不变量钉在
        派发处，任何新的别名/配置写法都绕不过去。
        """
        service = _service(("ping",))
        service.listener_methods = set(DEFERRED_ALIASES) | {"ping"}
        for alias in DEFERRED_ALIASES:
            self.assertFalse(
                service._should_process_in_listener({"method": alias}),
                "%s 绕过了派发处的闸门" % alias)
        self.assertTrue(service._should_process_in_listener({"method": "ping"}),
                        "把安全方法也一起关掉了")

    def test_canonical_and_wildcard_still_behave(self):
        """报告人量到的对照组：这两种写法本来就是对的，别修坏了。"""
        for methods in (("get_positions",), ("*",)):
            service = _service(methods)
            self.assertFalse(
                service._should_process_in_listener({"method": "query_stock_positions"}))
            self.assertFalse(
                service._should_process_in_listener({"method": "get_positions"}))

    def test_safe_aliases_still_run_inline(self):
        """别名本身不是罪名 —— get_full_tick -> get_ticks 是行情读，必须留在收包线程。

        行情读走 inline 是有意的：ZMQ 传输没有 adjust 驱动的 drain，把它们
        一起延后会永远卡住（redis_rpc.py 里 LISTENER_DEFERRED_METHODS 下面
        那段注释）。
        """
        service = _service(("get_full_tick",))
        self.assertTrue(
            service._should_process_in_listener({"method": "get_full_tick"}),
            "把安全的行情别名也挡掉了")
        self.assertTrue(
            service._should_process_in_listener({"method": "get_ticks"}))


class ReporterReproductionTest(unittest.TestCase):
    """#252 正文那段脚本，原样跑一遍：POSITION 查询必须落在主线程。"""

    def _run(self, listener_methods):
        seen = []

        def get_positions(account_id):
            seen.append(threading.current_thread().name)
            return {}

        service = _service(
            listener_methods,
            handlers=_handlers(SimpleNamespace(get_positions=get_positions)))
        request = {
            "request_id": "example", "account_id": "test",
            "method": "query_stock_positions", "params": {"account_id": "test"},
        }
        receiver = threading.Thread(
            target=service.enqueue_payload, args=(request,), name="redis-receiver")
        receiver.start()
        receiver.join()
        before = list(seen)
        pending = service.pending.qsize()
        service.drain_pending()
        return before, pending, list(seen)

    def test_every_spelling_defers_to_the_main_thread(self):
        for methods in [("query_stock_positions",), ("get_positions",), ("*",)]:
            before, pending, after = self._run(methods)
            self.assertEqual(
                before, [],
                "%s：查询在收包线程上就跑掉了" % (methods,))
            self.assertEqual(
                pending, 1,
                "%s：没有排进 pending 队列" % (methods,))
            self.assertEqual(
                after, ["MainThread"],
                "%s：drain 之后没有落在主线程上" % (methods,))


if __name__ == "__main__":
    unittest.main()
