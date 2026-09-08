# coding: utf-8
"""没有任何配置能把 trade-context 方法排到后台线程上（#244）。

`LISTENER_DEFERRED_METHODS` 是「必须在 adjust 主线程执行」的名单，因为
`get_trade_detail_data` 离开主策略线程返回空。`_expand_listener_methods`
以前只在 `"*"` 分支里减掉这个名单，**显式点名的方法直接 add**，所以
`rpc_listener_methods=("get_asset",)` 会把它放回收包线程。

这在 `rpc_background_threads=False` 时无害（收包线程就是 adjust 线程），
在 True 时静默出错。而且错得很阴：离开主线程后 `get_asset` 返回的不是空，
是**行数对、字段全是 None** 的对象 —— 客户端读成「这账户没钱」，不是
「调用失败」。

安全性依赖两个不相干的键碰巧一致，正是 `rpc_background_threads` 被一刀切
钉死为 False 的原因。在展开处强制执行之后，这个开关才可以纯按延迟来选。
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    LISTENER_DEFERRED_METHODS,
    READ_METHODS,
    RedisPubSubRpcService,
)


class ListenerExpansionTest(unittest.TestCase):
    def _service(self):
        service = RedisPubSubRpcService.__new__(RedisPubSubRpcService)
        service.handlers = None
        return service

    def _leaked(self, listener_methods):
        expanded = self._service()._expand_listener_methods(listener_methods)
        return expanded & LISTENER_DEFERRED_METHODS

    def test_wildcard_excludes_deferred(self):
        self.assertEqual(self._leaked(("*",)), set())

    def test_naming_a_deferred_method_explicitly_does_not_smuggle_it_in(self):
        """这条在修复前是红的。"""
        for method in sorted(LISTENER_DEFERRED_METHODS):
            self.assertEqual(
                self._leaked((method,)), set(),
                "显式点名 %s 把它排到了收包线程；background_threads=True 时它会"
                "返回「行数对、字段全 None」，客户端读成账户没钱" % method)

    def test_a_mixed_list_keeps_the_safe_ones(self):
        safe = sorted(READ_METHODS - LISTENER_DEFERRED_METHODS)[:3]
        deferred = sorted(LISTENER_DEFERRED_METHODS)[:2]
        expanded = self._service()._expand_listener_methods(tuple(safe) + tuple(deferred))
        self.assertEqual(expanded & LISTENER_DEFERRED_METHODS, set())
        for method in safe:
            self.assertIn(method, expanded, "把安全的只读方法也一起丢了")

    def test_wildcard_still_covers_the_read_surface(self):
        expanded = self._service()._expand_listener_methods(("*",))
        self.assertEqual(expanded, READ_METHODS - LISTENER_DEFERRED_METHODS)


if __name__ == "__main__":
    unittest.main()
