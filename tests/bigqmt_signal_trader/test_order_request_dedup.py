# coding: utf-8
"""同一个 request_id 的下单请求只派发一次（#245）。

客户端的 redis-py 连接默认带透明重试（ConnectionError / TimeoutError），而
`Redis._execute_command` 里 `conn.retry.call_with_retry(...)` 包的是
**send + parse**：服务端已经接受的 RPUSH，如果应答阶段出错，整条命令会被
重发。RPUSH 不是幂等的，于是同一次 `client.call('passorder', ...)` 可能派发
两次原生下单。

下游没有任何一层兜住：`order_stock` / `order_stock_async` 都是 `submit_order`
的别名，而 `_handle_submit_order` 没有去重（幂等日志只在
`submit_orders_batch` 里）。`request_id` 此前只用于路由应答。

能在服务端修干净，靠的是重试**重发的是同一份 payload**，request_id 一样 ——
第二份认得出来。而且两份都指向同一个应答键，所以把第一份的答案重发一次，
正好落在客户端等待的位置。
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("BIGQMT_LOG_NAME", "bigqmt-test-dedup")

from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    ORDER_METHODS,
    RedisPubSubRpcService,
)


class _Handlers(object):
    """只记参数，不碰 QMT、不碰柜台。"""

    def __init__(self):
        self.dispatched = []
        self._last_server_error = ""

    def _canonical_method(self, method):
        return {"order_stock": "submit_order",
                "order_stock_async": "submit_order"}.get(method, method)

    def handle(self, method, params=None):
        self.dispatched.append((self._canonical_method(method), dict(params or {})))
        return {"order_sys_id": "1001", "user_order_id": (params or {}).get("user_order_id")}

    def take_pending_settlement(self):
        return None


def _service():
    service = RedisPubSubRpcService.__new__(RedisPubSubRpcService)
    service.handlers = _Handlers()
    service.account_id = "acct"
    service.print_prefix = "[test]"
    service.debug_log_limit = 0
    service._processed_count = 0
    service._deferred_count = 0
    import collections
    service._order_requests = collections.OrderedDict()
    service._duplicate_order_requests = 0
    service.published = []
    service._publish_response = lambda request, response: service.published.append(response)
    return service


def _request(method, request_id, **params):
    return {"schema_version": 1, "request_id": request_id, "account_id": "acct",
            "method": method, "params": params}


class OrderDedupTest(unittest.TestCase):
    def test_a_resent_passorder_dispatches_once(self):
        """报告里的场景：RPUSH 被透明重试重发。"""
        service = _service()
        request = _request("passorder", "abc123", user_order_id="u-1")

        service.process_request(dict(request))
        service.process_request(dict(request))      # 重试重发的那一份

        dispatched = [m for m, _ in service.handlers.dispatched]
        self.assertEqual(dispatched, ["passorder"], "派发了两次：%s" % dispatched)
        self.assertEqual(service._duplicate_order_requests, 1)

    def test_the_duplicate_still_gets_an_answer(self):
        """不能只是丢掉——客户端还在同一个应答键上等。"""
        service = _service()
        request = _request("passorder", "abc123", user_order_id="u-1")

        first = service.process_request(dict(request))
        second = service.process_request(dict(request))

        self.assertIsNotNone(second)
        self.assertEqual(second["request_id"], first["request_id"])
        self.assertEqual(second["data"], first["data"])
        self.assertEqual(len(service.published), 2, "重复请求没有重发答案")

    def test_alias_paths_are_covered_too(self):
        """order_stock / order_stock_async 都别名到 submit_order。"""
        for method in ("order_stock", "order_stock_async", "submit_order"):
            service = _service()
            request = _request(method, "same-id", stock_code="600000.SH")
            service.process_request(dict(request))
            service.process_request(dict(request))
            self.assertEqual(len(service.handlers.dispatched), 1,
                             "%s 派发了两次" % method)

    def test_every_order_method_is_deduped(self):
        for method in sorted(ORDER_METHODS):
            service = _service()
            request = _request(method, "id-%s" % method)
            service.process_request(dict(request))
            service.process_request(dict(request))
            self.assertEqual(len(service.handlers.dispatched), 1,
                             "%s 没有去重" % method)

    def test_distinct_request_ids_both_run(self):
        """去重的是重发，不是两笔真实下单。"""
        service = _service()
        service.process_request(_request("passorder", "id-1", user_order_id="u-1"))
        service.process_request(_request("passorder", "id-2", user_order_id="u-2"))
        self.assertEqual(len(service.handlers.dispatched), 2,
                         "把两笔不同的下单也去掉了")

    def test_reads_are_not_deduped(self):
        """只读请求重发是无害的，去重反而会返回陈旧数据。"""
        service = _service()
        request = _request("query_stock_positions", "same-id")
        service.process_request(dict(request))
        service.process_request(dict(request))
        self.assertEqual(len(service.handlers.dispatched), 2)

    def test_the_table_does_not_grow_without_bound(self):
        service = _service()
        for index in range(service.ORDER_DEDUP_MAX + 50):
            service.process_request(_request("passorder", "id-%d" % index))
        self.assertLessEqual(len(service._order_requests), service.ORDER_DEDUP_MAX + 1)

    def test_an_in_flight_duplicate_is_not_dispatched(self):
        """第一份还没出答案时来的重发，也不能派发第二次。"""
        service = _service()
        key = ("acct", "abc123")
        service._claim_order_request(key)           # 模拟「已认领、尚无答案」
        result = service.process_request(_request("passorder", "abc123"))
        self.assertIsNone(result)
        self.assertEqual(service.handlers.dispatched, [])


if __name__ == "__main__":
    unittest.main()
