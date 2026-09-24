# coding: utf-8
"""A deployment must be able to say it has no redis (#147).

Every consumer of the redis client already guards the same way:

    redis_config = dict(config.get("redis") or {})
    if not redis_config:
        return None

and every one of those guards was dead code, because configure_runtime emitted
the block unconditionally from module defaults:

    redis={"host": REDIS_HOST,     # "127.0.0.1"
           "port": REDIS_PORT,     # 6379
           ...}

So config["redis"] was never empty and "I have no redis" was not expressible.
The reporter of #145 -- whose broker QMT does not whitelist the redis import at
all -- had no lever except patching _exec_event_redis to key off the transport,
which would have killed four unrelated features on zmq (the order-identity
store and download jobs were made transport-independent one day earlier, in
f20c58c and 053c9dc, for exactly the opposite reason).

redis_enabled=False now empties the block, and the existing guards do the rest:
nothing dials redis even once. transport=redis overrides it, because there the
bridge itself needs redis and honouring the switch would break the RPC rather
than the optional extras.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader_redis_rpc_runtime as runtime


class _RuntimeState(unittest.TestCase):
    KEYS = ("REDIS_ENABLED", "RPC_TRANSPORT", "REDIS_HOST", "REDIS_PORT", "REDIS_DB",
            "REDIS_ENABLED_EXPLICIT", "RPC_PIPE_CONFIG")

    def setUp(self):
        self._saved = {k: getattr(runtime, k) for k in self.KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(runtime, k, v)

    def _configure(self, enabled, transport, explicit=True):
        runtime.REDIS_ENABLED = enabled
        runtime.REDIS_ENABLED_EXPLICIT = explicit
        runtime.RPC_TRANSPORT = transport


class RedisBlockTest(_RuntimeState):
    def test_enabled_emits_the_settings(self):
        self._configure(True, "zmq")

        block = runtime._redis_block()

        self.assertEqual(block["host"], runtime.REDIS_HOST)
        self.assertEqual(block["port"], runtime.REDIS_PORT)

    def test_disabled_on_zmq_emits_nothing(self):
        """An empty dict is what makes every `if not redis_config` guard fire."""
        self._configure(False, "zmq")

        self.assertEqual(runtime._redis_block(), {})

    def test_disabled_on_mysql_and_shm_too(self):
        for transport in ("mysql", "shm"):
            self._configure(False, transport)

            self.assertEqual(runtime._redis_block(), {}, transport)

    def test_the_redis_transport_overrides_the_switch(self):
        """That deployment has no bridge at all without redis; honouring the
        switch would break the RPC instead of the optional extras."""
        for transport in ("redis", "", "default"):
            self._configure(False, transport)

            self.assertNotEqual(runtime._redis_block(), {}, transport)

    def test_the_default_keeps_existing_deployments_unchanged(self):
        self.assertTrue(self._saved["REDIS_ENABLED"])


class ConfigReadingTest(_RuntimeState):
    def test_configure_runtime_redis_reads_the_switch(self):
        runtime.configure_runtime_redis({"redis_enabled": False, "transport": "zmq"})

        self.assertFalse(runtime.REDIS_ENABLED)

    def test_it_defaults_to_on_when_absent(self):
        runtime.REDIS_ENABLED = True
        runtime.configure_runtime_redis({"transport": "zmq"})

        self.assertTrue(runtime.REDIS_ENABLED)

    def test_setting_it_true_explicitly_works(self):
        runtime.REDIS_ENABLED = False
        runtime.configure_runtime_redis({"redis_enabled": True, "transport": "zmq"})

        self.assertTrue(runtime.REDIS_ENABLED)


class ConsumersSeeAnEmptyBlockTest(_RuntimeState):
    """The point of an empty dict: the guards that were dead now fire."""

    def test_the_strategy_builds_no_client_from_an_empty_block(self):
        import bigqmt_signal_trader_strategy as strategy

        saved = strategy._exec_event_redis_client
        service = strategy._rpc_service
        try:
            strategy._exec_event_redis_client = None
            strategy._rpc_service = None

            self.assertIsNone(strategy._exec_event_redis({"redis": {}}))
            self.assertIsNone(strategy._exec_event_redis({}))
        finally:
            strategy._exec_event_redis_client = saved
            strategy._rpc_service = service

    def test_a_populated_block_still_builds_one(self):
        """The switch must not break deployments that do have redis."""
        import bigqmt_signal_trader_strategy as strategy

        saved = strategy._exec_event_redis_client
        service = strategy._rpc_service
        try:
            strategy._exec_event_redis_client = None
            strategy._rpc_service = None

            client = strategy._exec_event_redis(
                {"redis": {"host": "127.0.0.1", "port": 6379, "db": 5}})

            self.assertIsNotNone(client)
        finally:
            strategy._exec_event_redis_client = saved
            strategy._rpc_service = service


class ExampleConfigTest(unittest.TestCase):
    def test_the_switch_is_documented_where_people_will_look(self):
        import io

        path = os.path.join(ROOT, "src",
                            "bigqmt_signal_trader_local_config.example.py")
        text = io.open(path, encoding="utf-8").read()

        self.assertIn("redis_enabled", text)

    def test_the_example_states_what_it_costs(self):
        """Turning it on silently loses strategy_name backfill (#133); saying
        so in the config is cheaper than another issue."""
        import io

        path = os.path.join(ROOT, "src",
                            "bigqmt_signal_trader_local_config.example.py")
        text = io.open(path, encoding="utf-8").read()

        self.assertIn("strategy_name", text)

    def test_it_ships_commented_out(self):
        """Uncommenting is a decision; defaulting to it would change every
        existing deployment."""
        import io

        path = os.path.join(ROOT, "src",
                            "bigqmt_signal_trader_local_config.example.py")
        for line in io.open(path, encoding="utf-8"):
            if "redis_enabled" in line and "#" not in line.split("redis_enabled")[0]:
                self.fail("redis_enabled is live in the example: %r" % line)


class NoRedisBuildTest(unittest.TestCase):
    """The variant whose whole reason to exist is "redis is not importable here".

    It already forced transport=zmq, but not redis_enabled -- so the runtime
    filled the block in from its defaults and every consumer dialled
    127.0.0.1:6379 anyway. That is the build most likely to hit #145.

    Two files, because the forcing block is hand-copied into the single-file
    builder as well. PR #134 was about exactly this class of drift, so pin
    both rather than trusting them to stay in step.
    """

    FILES = (
        os.path.join("bigqmt_no_redis", "DRYRUN_no_redis.py"),
        os.path.join("tools", "build_no_redis_single_file_flat.py"),
    )

    def _read(self, relative):
        import io

        with io.open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
            return handle.read()

    def test_both_entries_disable_redis(self):
        for relative in self.FILES:
            self.assertIn('"redis_enabled"', self._read(relative), relative)

    def test_they_set_it_to_False_not_merely_mention_it(self):
        for relative in self.FILES:
            text = self._read(relative)
            self.assertTrue(
                'BIGQMT_REDIS_CONFIG["redis_enabled"] = False' in text
                or '"redis_enabled": False' in text,
                relative)

    def test_they_still_force_zmq(self):
        """The new line must not have displaced the existing one."""
        for relative in self.FILES:
            self.assertIn('"transport"] = "zmq"', self._read(relative), relative)

    def test_the_entry_says_what_it_costs(self):
        """A silent loss of strategy_name backfill is how #133 gets reopened."""
        self.assertIn("strategy_name", self._read(self.FILES[0]))


class PipeDropsRedisByDefaultTest(_RuntimeState):
    """2026-09-24 实盘：pipe 部署（沙箱禁 socket，EDR 抓 connect() 杀进程）
    的 redis 块默认还在，exec 事件链路的懒 client 首个命令就拨号。pipe 下
    默认不下发；显式 redis_enabled=True 才是真的要 redis 附加能力。"""

    def test_pipe_drops_the_block_by_default(self):
        self._configure(True, "pipe", explicit=False)

        self.assertEqual(runtime._redis_block(), {})

    def test_pipe_keeps_the_block_when_explicitly_enabled(self):
        self._configure(True, "pipe", explicit=True)

        block = runtime._redis_block()

        self.assertEqual(block["host"], runtime.REDIS_HOST)

    def test_zmq_still_keeps_the_block_by_default(self):
        """zmq + redis 是合法组合（exec 事件回放、服务发现）——只有 pipe
        这类沙箱传输默认丢块。"""
        self._configure(True, "zmq", explicit=False)

        self.assertNotEqual(runtime._redis_block(), {})


class PipeConfigReadingTest(_RuntimeState):
    def test_configure_runtime_redis_reads_the_pipe_block(self):
        runtime.configure_runtime_redis(
            {"transport": "pipe", "pipe": {"pipe_name": "probe_pipe"}})

        self.assertEqual("pipe", runtime.RPC_TRANSPORT)
        self.assertEqual({"pipe_name": "probe_pipe"}, runtime.RPC_PIPE_CONFIG)

    def test_redis_enabled_explicit_flag_is_tracked(self):
        runtime.REDIS_ENABLED_EXPLICIT = False
        runtime.configure_runtime_redis({"transport": "pipe"})
        self.assertFalse(runtime.REDIS_ENABLED_EXPLICIT)

        runtime.configure_runtime_redis({"transport": "pipe", "redis_enabled": True})
        self.assertTrue(runtime.REDIS_ENABLED_EXPLICIT)


class ModuleLevelReadTest(unittest.TestCase):
    """直接挂 runtime（不经 DRYRUN 外壳）的部署在模块级读配置——漏读
    transport 就是「配了 pipe 实际还在跑 redis」（2026-09-24 实盘根因之一）。
    reload 一次 runtime 来验证模块级读取。"""

    def test_module_level_reads_transport_and_pipe_block(self):
        import importlib
        import types

        stub = types.ModuleType("bigqmt_signal_trader_local_config")
        stub.BIGQMT_ACCOUNT_ID = ""
        stub.BIGQMT_REDIS_CONFIG = {"transport": "pipe",
                                    "pipe": {"pipe_name": "probe_mod"},
                                    "redis_enabled": False}
        saved_module = sys.modules.get("bigqmt_signal_trader_local_config")
        sys.modules["bigqmt_signal_trader_local_config"] = stub
        try:
            importlib.reload(runtime)
            self.assertEqual("pipe", runtime.RPC_TRANSPORT)
            self.assertEqual({"pipe_name": "probe_mod"}, runtime.RPC_PIPE_CONFIG)
            self.assertTrue(runtime.REDIS_ENABLED_EXPLICIT)
            self.assertFalse(runtime.REDIS_ENABLED)
        finally:
            if saved_module is None:
                sys.modules.pop("bigqmt_signal_trader_local_config", None)
            else:
                sys.modules["bigqmt_signal_trader_local_config"] = saved_module
            importlib.reload(runtime)   # 恢复默认状态，别污染其它用例


class PipeBuildTest(unittest.TestCase):
    """pipe 单文件打包器：和 no-redis 版同款钉法（PR #134 的教训是这类
    强制块会漂移）。"""

    BUILDER = os.path.join("tools", "build_pipe_single_file_flat.py")

    def _read(self, relative):
        import io

        with io.open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
            return handle.read()

    def test_the_builder_forces_pipe(self):
        self.assertIn('"transport"] = "pipe"', self._read(self.BUILDER))

    def test_the_builder_disables_redis_and_quote_push(self):
        text = self._read(self.BUILDER)
        self.assertIn('"redis_enabled"] = False', text)
        self.assertIn('"quote_push"] = {"enabled": False}', text)

    def test_the_builder_does_not_substitute_zmq(self):
        """pipe_transport 在包里、零依赖——pipe 版不做 no-redis 的
        zmq_transport 替换，也不内嵌 bigqmt_no_redis。"""
        text = self._read(self.BUILDER)
        self.assertNotIn("no_redis_override_path()", text.replace("flat.no_redis_override_path", ""))
        self.assertIn("named-pipe version", text)


if __name__ == "__main__":
    unittest.main()
