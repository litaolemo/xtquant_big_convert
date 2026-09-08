# coding: utf-8
"""账户查询失败不能伪装成成功（#243）。

`query_stock_positions` / `query_stock_asset` / `query_stock_position` 在 RPC
抛异常后会去读 `bigqmt:positions:<account>`，只要那里有非空旧数据就当成
查询结果返回。后果是 #229/#230 好不容易让原生 POSITION 查询异常上抛，在
redis 客户端这条路上又被吞回去 —— **同一个输入，zmq 抛错、redis 返回旧持仓**。

对交易系统来说这是最坏的一种错：策略拿它去算仓位，而它看起来完全正常。
缓存还不校 `updated_at`，2000 年的快照和一秒前的一样会被采信。

判定「开没开」以前只看 transport，`local_cache_enabled=False` 关不掉它 ——
那个键本来也是**客户端行情缓存**的开关，两回事。所以这里给账户缓存一个
自己的开关，默认关（失败就是失败），打开也要求快照有日期且在时限内。
"""
import datetime
import json
import os
import sys
import unittest

from unittest.mock import Mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("BIGQMT_LOG_NAME", "bigqmt-test-account-cache")

from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader  # noqa: E402


POSITIONS = {"600000.SH": {"stock_code": "600000.SH", "volume": 1000,
                           "available": 1000, "cost": 10.0}}
ASSET = {"cash": 1234.0, "total_asset": 5678.0}


def _snapshot(updated_at):
    snap = {"positions": POSITIONS, "asset": ASSET}
    if updated_at is not None:
        snap["updated_at"] = updated_at
    return snap


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _trader(snapshot, **extra):
    config = {"transport": "redis", "local_cache_enabled": False}
    config.update(extra)
    trader = BigQmtXtTrader(account_id="test-only", redis_config=config)
    trader.client.call = Mock(side_effect=RuntimeError("native query failed"))
    trader.client._redis = Mock(return_value=Mock(
        get=Mock(return_value=json.dumps(snapshot).encode())))
    return trader


class DefaultPropagatesTest(unittest.TestCase):
    """默认行为：失败就是失败，和 zmq 一致。"""

    def test_positions_failure_reaches_the_caller(self):
        trader = _trader(_snapshot("2000-01-01 00:00:00"))
        with self.assertRaises(RuntimeError):
            trader.query_stock_positions("test-only")

    def test_a_fresh_snapshot_does_not_change_that(self):
        """不是「太旧才不用」，是默认根本不用。"""
        trader = _trader(_snapshot(_now()))
        with self.assertRaises(RuntimeError):
            trader.query_stock_positions("test-only")

    def test_asset_failure_reaches_the_caller(self):
        trader = _trader(_snapshot(_now()))
        with self.assertRaises(RuntimeError):
            trader.query_stock_asset("test-only")

    def test_single_position_failure_reaches_the_caller(self):
        trader = _trader(_snapshot(_now()))
        with self.assertRaises(RuntimeError):
            trader.query_stock_position("test-only", "600000.SH")

    def test_redis_now_matches_zmq(self):
        """#243 的核心症状：同一输入两种传输答案相反。"""
        for transport in ("redis", "zmq"):
            trader = _trader(_snapshot(_now()))
            trader.client.transport_name = transport
            with self.assertRaises(RuntimeError, msg="transport=%s 吞掉了异常" % transport):
                trader.query_stock_positions("test-only")


class OptInFallbackTest(unittest.TestCase):
    """显式打开后：要有日期，且在时限内。"""

    def test_fresh_snapshot_answers(self):
        trader = _trader(_snapshot(_now()), account_cache_fallback=True)
        positions = trader.query_stock_positions("test-only")
        self.assertEqual(positions[0].volume, 1000)

    def test_stale_snapshot_still_raises(self):
        trader = _trader(_snapshot("2000-01-01 00:00:00"), account_cache_fallback=True)
        with self.assertRaises(RuntimeError):
            trader.query_stock_positions("test-only")

    def test_an_undated_snapshot_is_refused(self):
        """没有 updated_at 就无法判断新旧，不能采信。"""
        trader = _trader(_snapshot(None), account_cache_fallback=True)
        with self.assertRaises(RuntimeError):
            trader.query_stock_positions("test-only")

    def test_the_age_bound_is_configurable(self):
        old = (datetime.datetime.now() - datetime.timedelta(seconds=120)
               ).strftime("%Y-%m-%d %H:%M:%S")
        tight = _trader(_snapshot(old), account_cache_fallback=True)
        with self.assertRaises(RuntimeError):
            tight.query_stock_positions("test-only")

        loose = _trader(_snapshot(old), account_cache_fallback=True,
                        account_cache_max_age_seconds=600)
        self.assertEqual(loose.query_stock_positions("test-only")[0].volume, 1000)

    def test_asset_and_single_position_honour_the_same_switch(self):
        asset = _trader(_snapshot(_now()), account_cache_fallback=True)
        self.assertEqual(asset.query_stock_asset("test-only").cash, 1234.0)

        single = _trader(_snapshot(_now()), account_cache_fallback=True)
        self.assertEqual(single.query_stock_position("test-only", "600000.SH").volume, 1000)


class SwitchIndependenceTest(unittest.TestCase):
    def test_local_cache_enabled_is_a_different_cache(self):
        """local_cache_enabled 管客户端行情缓存，不该顺手管账户缓存。

        它开着也不该把账户回退打开 —— 账户回退有自己的键。
        """
        trader = _trader(_snapshot(_now()), local_cache_enabled=True)
        with self.assertRaises(RuntimeError):
            trader.query_stock_positions("test-only")


class SnapshotAgeTest(unittest.TestCase):
    def test_parses_the_formats_the_publisher_writes(self):
        trader = _trader(_snapshot(_now()))
        for stamp in ("2026-09-08 10:00:00", "2026-09-08T10:00:00",
                      "2026-09-08 10:00:00.500"):
            self.assertIsNotNone(trader._snapshot_age_seconds({"updated_at": stamp}), stamp)

    def test_unparseable_is_none_not_zero(self):
        """认不出来要当「不知道多旧」，不能当「刚刚」。"""
        trader = _trader(_snapshot(_now()))
        for stamp in ("", None, "not-a-date"):
            self.assertIsNone(trader._snapshot_age_seconds({"updated_at": stamp}), repr(stamp))


if __name__ == "__main__":
    unittest.main()
