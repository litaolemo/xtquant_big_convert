# coding: utf-8
"""Redis 断开时行情接收器每秒重连，但不能每秒刷一行日志（#256 的跟进）。

#257 让接收器自己负责重连，这是对的 —— 在那之前连接一断行情就永久停掉，而
keepalive 和补订阅还在成功，外部看着一切健康。

但它每次失败都打一行，重试间隔又是固定 1 秒：Redis 掉一个周末就是 17 万行。
这个仓库为日志量吃过亏 —— #139 把同一行写了 16 次，#144 因为轮转永远失败导致
日志无上限增长。zmq 的 ROUTER 重建为同样的理由做了退避（#240）。

所以重连速度保持 1 秒（行情要快点回来），只让**报告**退避：第 1、2、4、8…次
各报一次，之后最多每分钟一次。第一次失败仍然是响的，长时间故障也读得下去。
"""
import contextlib
import io as _io
import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.quote_push_channel import RedisQuotePushChannel  # noqa: E402


class _DeadPubsub(object):
    def subscribe(self, *channels):
        raise ConnectionError("redis unreachable")

    def get_message(self, **kwargs):
        return None

    def close(self):
        pass


class _DeadRedis(object):
    def __init__(self):
        self.connections = 0

    def pubsub(self, **kwargs):
        self.connections += 1
        return _DeadPubsub()


class ReconnectLogThrottleTest(unittest.TestCase):
    def _run_outage(self, seconds):
        redis = _DeadRedis()
        channel = RedisQuotePushChannel(redis, account_id="t")
        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            channel.start_subscriber(["x"], lambda topic, data: None)
            time.sleep(seconds)
            channel.stop()
        lines = [l for l in buffer.getvalue().splitlines() if "reconnecting" in l]
        return redis, lines

    def test_it_keeps_retrying_every_second(self):
        """退避的是日志，不是重连 —— 行情要尽快回来。"""
        redis, _ = self._run_outage(4.5)
        self.assertGreaterEqual(redis.connections, 4,
                                "重连变慢了：4.5 秒只试了 %d 次" % redis.connections)

    def test_it_does_not_print_once_per_attempt(self):
        redis, lines = self._run_outage(4.5)
        self.assertLess(len(lines), redis.connections,
                        "每次重连都打了一行：%d 次尝试 %d 行" % (redis.connections, len(lines)))

    def test_the_first_failure_is_still_reported(self):
        """节流不能把第一次失败也吞掉 —— 那就没人知道断了。"""
        _, lines = self._run_outage(1.5)
        self.assertTrue(lines, "第一次连接失败一声不吭")

    def test_a_long_outage_stays_readable(self):
        """按 1、2、4、8… 报告，10 秒内不该超过 5 行。"""
        _, lines = self._run_outage(10.0)
        self.assertLessEqual(len(lines), 5,
                             "10 秒故障打了 %d 行，长时间故障会淹掉日志" % len(lines))

    def test_the_report_says_which_attempt_it_is(self):
        """跳号报告必须带次数，否则读的人无法判断断了多久。"""
        _, lines = self._run_outage(3.0)
        self.assertTrue(any("attempt" in l for l in lines),
                        "报告里没有尝试次数：%s" % lines[:2])


if __name__ == "__main__":
    unittest.main()
