# coding: utf-8
"""connect() 失败必须拆掉事件监听，否则每次重试泄漏一个 Redis pubsub 订阅。

实测单日 8000+ 个 subscribe 连接逼近 maxclients 的部署，根因就是重连风暴
里每个被丢弃的实例都留着 start() 拉起的监听线程（每实例 4 个 exec 事件
频道订阅）。修复后 connect 抛错前先 stop()。
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader  # noqa: E402

ACCOUNT = "8886800503"


class _FailingClient(object):
    """ping 必超时的客户端：connect 的第一步就失败。"""

    account_id = ACCOUNT
    local_cache_config = {}
    full_tick_cache_config = {}

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        if method == "ping":
            raise TimeoutError("redis rpc timeout: ping")
        return {}

    def _redis(self):
        raise AssertionError("redis not expected here")


class ConnectFailTeardownTest(unittest.TestCase):
    def test_failed_connect_tears_down_the_event_listener(self):
        trader = BigQmtXtTrader(account_id=ACCOUNT)
        trader.client = _FailingClient()

        stops = []
        original_stop = trader.stop

        def spying_stop():
            stops.append(1)
            return original_stop()

        trader.stop = spying_stop
        trader.start()  # start() 拉起事件监听线程（泄漏源）
        self.assertIsNotNone(trader._event_thread)

        with self.assertRaises(TimeoutError):
            trader.connect()

        self.assertEqual(stops, [1], "connect 失败必须 stop() 拆掉监听")
        thread = trader._event_thread
        self.assertTrue(
            thread is None or not thread.is_alive(),
            "事件监听线程还留着，它的 pubsub 订阅会泄漏在服务端",
        )


if __name__ == "__main__":
    unittest.main()
