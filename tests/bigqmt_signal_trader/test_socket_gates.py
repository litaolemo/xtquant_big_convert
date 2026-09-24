# -*- coding: utf-8 -*-
"""pipe/沙箱部署的 socket 闸门（2026-09-24 实盘逐行核对的两个漏网点）。

服务端：native xtdata SDK 的调用会拨本地 58610 行情服务——pipe（外连即杀）
下 native_xtdata_enabled 默认 False，_native() 恒 None，连探测都不加载。

客户端：非 redis 传输且没有显式 redis 配置时，事件线程每轮开头的 ping
会照默认 127.0.0.1:6379 拨——现在整步跳过（不建 client、不 ping）；
全推推送通道同样直接报错而不是拨默认 redis。
"""
import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider  # noqa: E402
from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    BigQmtXtTrader, _build_quote_push_channel,
)
from bigqmt_signal_trader.quote_push_channel import RedisQuotePushChannel  # noqa: E402


class NativeXtdataGateTest(unittest.TestCase):
    def test_disabled_never_loads(self):
        import bigqmt_signal_trader.adapters.market_bigqmt as m

        def _boom():
            raise AssertionError("_load_native_xtdata must not be called")

        saved = m._load_native_xtdata
        m._load_native_xtdata = _boom
        try:
            provider = BigQmtMarketDataProvider(object(), native_xtdata_enabled=False)
            self.assertIsNone(provider._native())
        finally:
            m._load_native_xtdata = saved

    def test_enabled_loads_lazily_as_before(self):
        provider = BigQmtMarketDataProvider(object(), native_xtdata_enabled=True)
        # 仓库环境里没有 QMT 的原生 SDK：加载尝试合法地回 None，但确实走了加载
        self.assertIsNone(provider._native())

    def test_factory_defaults_off_for_pipe(self):
        # build_app 在函数内局部 import BigQmtMarketDataProvider——要钉在
        # 源模块上才有用。
        import bigqmt_signal_trader.adapter_factory as af
        import bigqmt_signal_trader.adapters.market_bigqmt as market_mod

        captured = {}

        class SpyProvider(BigQmtMarketDataProvider):
            def __init__(self, context_info, **kwargs):
                captured.update(kwargs)
                super(SpyProvider, self).__init__(context_info, **kwargs)

        saved = market_mod.BigQmtMarketDataProvider
        market_mod.BigQmtMarketDataProvider = SpyProvider
        try:
            af.build_app(types.SimpleNamespace(),
                         {"mode": "bigqmt", "rpc": {"transport": "pipe"},
                          "qmt_api": {}})
            self.assertFalse(captured["native_xtdata_enabled"])
            captured.clear()
            af.build_app(types.SimpleNamespace(),
                         {"mode": "bigqmt",
                          "rpc": {"transport": "pipe", "native_xtdata_enabled": True},
                          "qmt_api": {}})
            self.assertTrue(captured["native_xtdata_enabled"])
            captured.clear()
            af.build_app(types.SimpleNamespace(),
                         {"mode": "bigqmt", "rpc": {"transport": "redis"},
                          "qmt_api": {}})
            self.assertTrue(captured["native_xtdata_enabled"])
        finally:
            market_mod.BigQmtMarketDataProvider = saved


class ClientRedisGateTest(unittest.TestCase):
    def _trader(self, transport, explicit):
        trader = BigQmtXtTrader(account_id="acct")
        trader.client = types.SimpleNamespace(
            transport_name=transport,
            account_id="acct",
            _redis_explicit=explicit,
            _redis=lambda: (_ for _ in ()).throw(AssertionError("must not build")),
        )
        return trader

    def test_pipe_without_explicit_redis_skips_the_probe_entirely(self):
        trader = self._trader("pipe", explicit=False)

        self.assertIsNone(trader._exec_events_redis_or_none())

    def test_pipe_with_explicit_redis_probes_as_before(self):
        reachable = types.SimpleNamespace(ping=lambda: True)
        trader = self._trader("pipe", explicit=True)
        trader.client._redis = lambda: reachable

        self.assertIs(trader._exec_events_redis_or_none(), reachable)

    def test_redis_transport_always_probes(self):
        reachable = types.SimpleNamespace(ping=lambda: True)
        trader = self._trader("redis", explicit=False)
        trader.client._redis = lambda: reachable

        self.assertIs(trader._exec_events_redis_or_none(), reachable)


class QuoteChannelGateTest(unittest.TestCase):
    def test_pipe_without_explicit_redis_raises_instead_of_dialling(self):
        client = types.SimpleNamespace(
            transport_name="pipe", account_id="acct", _redis_explicit=False,
            _redis=lambda: (_ for _ in ()).throw(AssertionError("must not build")),
        )
        with self.assertRaises(RuntimeError) as caught:
            _build_quote_push_channel(client)
        self.assertIn("没有全推推送通道", str(caught.exception))

    def test_pipe_with_explicit_redis_builds_the_redis_channel(self):
        client = types.SimpleNamespace(
            transport_name="pipe", account_id="acct", _redis_explicit=True,
            _redis=lambda: object(),
        )
        channel = _build_quote_push_channel(client)
        self.assertIsInstance(channel, RedisQuotePushChannel)


if __name__ == "__main__":
    unittest.main()
