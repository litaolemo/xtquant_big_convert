"""The single-file build must stay correct as the package changes (issue #56).

The users who need this build run QMT sandboxes that cannot read files or
import anything external, which is also to say they cannot debug it. So the
generator is checked here rather than at their end: the emitted file is
executed with the real package hidden from sys.path, proving that everything it
needs is genuinely embedded.
"""

import io
import os
import subprocess
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)

import build_single_file as builder


class _Isolated(object):
    """Run with the real package removed from sys.path and sys.modules.

    Without this the generated file would happily import the package from disk
    and the test would pass no matter what the build left out.
    """

    def __enter__(self):
        self._path = list(sys.path)
        self._modules = dict(sys.modules)
        sys.path = [p for p in sys.path if "xtquant_big_convert" not in p.replace("\\", "/")]
        for name in list(sys.modules):
            if name.startswith("bigqmt_signal_trader"):
                del sys.modules[name]
        self._meta = list(sys.meta_path)
        return self

    def __exit__(self, *exc):
        sys.meta_path[:] = self._meta
        sys.path = self._path
        sys.modules.clear()
        sys.modules.update(self._modules)
        return False


def _build():
    text, modules, skipped = builder.build(
        builder.ENTRY_MODULES, "bigqmt_signal_trader_strategy")
    return text, modules, skipped


def _run(text, module_name="BIGQMT_SINGLE_UNDER_TEST"):
    module = types.ModuleType(module_name)
    exec(compile(text, "<single-file>", "exec"), module.__dict__)
    return module


class BuildTest(unittest.TestCase):
    def test_emits_valid_python(self):
        import ast

        ast.parse(_build()[0])

    def test_embeds_the_modules_the_bridge_needs(self):
        _, modules, _ = _build()
        names = set(n for n, _, _ in modules)

        for required in ("bigqmt_signal_trader_strategy",
                         "bigqmt_signal_trader.redis_rpc",
                         "bigqmt_signal_trader.transports.zmq_transport",
                         "bigqmt_signal_trader.exec_events",
                         "bigqmt_signal_trader.adapters.order_bigqmt"):
            self.assertIn(required, names)

    def test_dependencies_precede_their_dependents(self):
        """Registration executes eagerly, so order is not cosmetic."""
        _, modules, _ = _build()
        position = dict((n, i) for i, (n, _, _) in enumerate(modules))

        for name, source, is_pkg in modules:
            for dep in builder.intra_package_imports(source, name, is_pkg, top_level_only=True):
                if dep in position and dep != name:
                    self.assertLess(position[dep], position[name],
                                    "%s executes before its dependency %s" % (name, dep))

    def test_an_excluded_module_imported_at_top_level_is_refused(self):
        """Shipping that build would fail on first import inside the sandbox."""
        original = builder.EXCLUDED_MODULES
        builder.EXCLUDED_MODULES = frozenset(["bigqmt_signal_trader.code_utils"])
        try:
            with self.assertRaises(builder.BuildError) as caught:
                _build()
            self.assertIn("code_utils", str(caught.exception))
        finally:
            builder.EXCLUDED_MODULES = original


class GeneratedFileTest(unittest.TestCase):
    """Execute the artifact with the real package hidden."""

    @classmethod
    def setUpClass(cls):
        cls.text = _build()[0]

    def test_runs_with_the_package_off_sys_path(self):
        with _Isolated():
            module = _run(self.text)
            self.assertTrue(callable(module.__dict__.get("init")))

    def test_every_module_comes_from_the_single_file(self):
        with _Isolated():
            _run(self.text)
            origins = set(
                getattr(sys.modules[n], "__file__", None)
                for n in sys.modules if n.startswith("bigqmt_signal_trader"))

            self.assertEqual(origins, {"<bigqmt_single_file>"},
                             "something was imported from disk, not from the build")

    def test_relative_imports_resolve(self):
        with _Isolated():
            _run(self.text)
            from bigqmt_signal_trader.code_utils import normalize_stock_code

            self.assertEqual(normalize_stock_code("600000"), "600000.SH")

    def test_business_logic_works(self):
        """A smoke test through real logic, not just imports."""
        with _Isolated():
            _run(self.text)
            from bigqmt_signal_trader.exec_events import normalize_order_event

            class Row(object):
                m_strInstrumentID = "601398"
                m_strExchangeID = "SH"
                m_nOffsetFlag = 49
                m_nVolumeTotalOriginal = 100
                m_nVolumeTraded = 0
                m_strCancelInfo = "[COUNTER] 资金可用余额不足"
                m_strInsertDate = "20260821"
                m_strInsertTime = "093015"

            event = normalize_order_event(Row(), "acct")

            self.assertEqual(event["stock_code"], "601398.SH")   # suffix completed
            self.assertEqual(event["action"], "SELL")
            self.assertEqual(event["order_volume"], 100)         # original, not remaining
            self.assertIn("资金可用余额不足", event["status_msg"])
            self.assertGreater(event["order_time"], 0)

    def test_zmq_transport_can_be_built(self):
        with _Isolated():
            _run(self.text)
            from bigqmt_signal_trader.transports.factory import build_transport

            transport = build_transport(
                "zmq", {"zmq": {"connect_address": "tcp://127.0.0.1:19999"}},
                account_id="123")

            self.assertEqual(type(transport).__name__, "ZmqTransport")

    def test_exports_the_qmt_callbacks(self):
        with _Isolated():
            module = _run(self.text)

            for name in ("init", "handlebar", "adjust", "order_callback", "deal_callback"):
                self.assertTrue(callable(module.__dict__.get(name)), name)

    def test_reload_works(self):
        """QMT re-runs a strategy without restarting, and the strategy reloads
        adapter_factory on every run. reload() ignores an existing __spec__ and
        re-runs find_spec, so the embedded finder has to answer."""
        with _Isolated():
            _run(self.text)
            import importlib

            importlib.reload(sys.modules["bigqmt_signal_trader.adapter_factory"])
            from bigqmt_signal_trader.code_utils import normalize_stock_code

            self.assertEqual(normalize_stock_code("600000"), "600000.SH")

    def test_running_twice_is_safe(self):
        """A QMT strategy re-run executes the file again in a live process."""
        with _Isolated():
            _run(self.text, "FIRST")
            module = _run(self.text, "SECOND")

            self.assertTrue(callable(module.__dict__.get("handlebar")))

    def test_missing_account_id_is_refused(self):
        with _Isolated():
            module = _run(self.text)

            with self.assertRaises(ValueError):
                module.init(None)


class CliTest(unittest.TestCase):
    def test_check_mode_writes_nothing(self):
        result = subprocess.run(
            [sys.executable, os.path.join(TOOLS, "build_single_file.py"), "--check"],
            capture_output=True, text=True, cwd=ROOT)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("embedded", result.stdout)


if __name__ == "__main__":
    unittest.main()
