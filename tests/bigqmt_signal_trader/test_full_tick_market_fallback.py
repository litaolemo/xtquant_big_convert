# coding: utf-8
"""get_full_tick 的 market_fallback 开关与显式超时 (#373)。

0.3.56 之前：传入大名单 + 显式超时，直读不全或失败时客户端自动扩读交易
所（['SH'] types stock / all），且把显式超时硬抬到 60s——接受部分行情
的调用方没有关闭扩大查询的办法，部分结果还会被空的市场回包整体顶掉。
本文件的形状直接来自 reporter 的最小复现（真实安装的客户端，仅替换
client.call，记录调用轨迹）。
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.xtquant_compat import BigQmtXtData


class Client(object):
    account_id = "acct"
    full_tick_cache_config = {"enabled": False}

    def __init__(self, direct, market=None):
        # direct: dict to answer on the first (explicit-list) call, or an
        # exception instance to raise. market: dict for the token re-reads.
        self._direct = direct
        self._market = market or {}
        self.calls = []

    def call(self, method, params, **kwargs):
        codes = params["codes"]
        self.calls.append((list(codes) if len(codes) < 3 else ["<%d codes>" % len(codes)],
                           params.get("types"), kwargs.get("timeout_seconds")))
        if len(self.calls) == 1 and isinstance(self._direct, Exception):
            raise self._direct
        if len(self.calls) == 1:
            return self._direct
        return dict(self._market)


def _codes(n):
    return ["%06d.SH" % (600000 + i) for i in range(n)]


class MarketFallbackDisabledTest(unittest.TestCase):
    def test_partial_answer_is_returned_as_is_no_market_read(self):
        client = Client(direct={"600000.SH": {"lastPrice": 10.0}})
        result = BigQmtXtData(client).get_full_tick(
            _codes(1001), timeout_seconds=10, market_fallback=False)

        self.assertEqual({"600000.SH": {"lastPrice": 10.0}}, result)
        self.assertEqual(1, len(client.calls), "must not re-read the exchange")
        self.assertEqual(10, client.calls[0][2], "the explicit timeout stands")

    def test_the_original_error_propagates_no_market_read(self):
        client = Client(direct=TimeoutError("synthetic direct-request timeout"))
        with self.assertRaises(TimeoutError):
            BigQmtXtData(client).get_full_tick(
                _codes(1001), timeout_seconds=10, market_fallback=False)
        self.assertEqual(1, len(client.calls), "must not re-read the exchange")


class MarketFallbackDefaultTest(unittest.TestCase):
    def test_partial_direct_rows_survive_the_recovery(self):
        """0.3.56 丢的就是这条：直读已有 1 行，空扩读把它顶成 0。"""
        client = Client(direct={"600000.SH": {"lastPrice": 10.0}},
                        market={"600001.SH": {"lastPrice": 11.0}})
        result = BigQmtXtData(client).get_full_tick(_codes(1001), timeout_seconds=10)

        self.assertIn("600000.SH", result, "the direct row must survive")
        self.assertIn("600001.SH", result, "the recovered row joins in")
        self.assertEqual(10.0, result["600000.SH"]["lastPrice"], "direct wins on overlap")

    def test_the_explicit_timeout_is_not_inflated_to_60(self):
        client = Client(direct={}, market={"600000.SH": {"lastPrice": 10.0}})
        BigQmtXtData(client).get_full_tick(_codes(1001), timeout_seconds=10)

        market_calls = [c for c in client.calls if c[0] == ["SH"]]
        self.assertTrue(market_calls, "expected the market re-read")
        for _codes_, _types, timeout in market_calls:
            self.assertEqual(10, timeout, "explicit timeout must reach market reads")

    def test_no_explicit_timeout_still_gets_the_60s_floor(self):
        client = Client(direct={}, market={"600000.SH": {"lastPrice": 10.0}})
        BigQmtXtData(client).get_full_tick(_codes(1001))

        market_calls = [c for c in client.calls if c[0] == ["SH"]]
        self.assertTrue(market_calls)
        for _codes_, _types, timeout in market_calls:
            self.assertEqual(60, timeout)


if __name__ == "__main__":
    unittest.main()
