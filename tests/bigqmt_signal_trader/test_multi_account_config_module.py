# coding: utf-8
"""One QMT terminal, two accounts: each entry must be able to name its config
module (#261).

The README has told people to "load two DRYRUN files, each pointing at a
different config" since the multi-account section was written. Nothing pointed
anywhere: the config module name was hardcoded in three places --

    BIGQMT_REDIS_DRYRUN.py            _load_local_config()
    bigqmt_signal_trader_redis_rpc_runtime.py
                                      from bigqmt_signal_trader_local_config
                                      import BIGQMT_ACCOUNT_ID, ...
    bigqmt_signal_trader_strategy.py  _detect_account_id's importlib.reload

-- so a second copy of the entry read the first account's config and the two
instances traded the same account. The documented feature did not exist.

The mechanism: the entry resolves BIGQMT_LOCAL_CONFIG_MODULE (a global in the
entry file, the same shape as BIGQMT_FORCE_TRANSPORT) to a source path and
loads that file UNDER the canonical name. The other two sites keep importing
"bigqmt_signal_trader_local_config" literally, which is what makes every
reload path keep working:

    (a) _local_import / _local_import_module for the canonical name
    (b) importlib.reload(_local_config) in _detect_account_id
    (c) reload_deployment's _purge_package_modules
    (d) _clear_local_modules() at the next entry run

all four are pinned below.

Entry files are exec'd by QMT with its own globals, so the loader half of the
source is exec'd here on its own -- the same technique test_corrupt_source_
message.py uses -- and stops short of the QMT-only bottom half.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

ENTRY = os.path.join(SRC, "BIGQMT_REDIS_DRYRUN.py")
NO_REDIS_ENTRY = os.path.join(ROOT, "bigqmt_no_redis", "DRYRUN_no_redis.py")
CANONICAL = "bigqmt_signal_trader_local_config"
CREDIT = "bigqmt_signal_trader_local_config_credit"
RUNTIME = "bigqmt_signal_trader_redis_rpc_runtime"

# The stub stands in for the real runtime module while the loader is under
# test: same import lines, nothing else. Its point is that the two lines the
# runtime really uses are exercised through the real _local_import.
RUNTIME_STUB = (
    "from bigqmt_signal_trader_local_config import "
    "BIGQMT_ACCOUNT_ID, BIGQMT_REDIS_CONFIG\n"
    "from bigqmt_signal_trader_local_config import BIGQMT_ACCOUNT_TYPE\n"
    "ACCOUNT_ID = BIGQMT_ACCOUNT_ID\n"
    "ACCOUNT_TYPE = BIGQMT_ACCOUNT_TYPE\n"
)

CONFIG_TEMPLATE = (
    "# coding: utf-8\n"
    "BIGQMT_ACCOUNT_ID = %r\n"
    "BIGQMT_ACCOUNT_TYPE = %r\n"
    "BIGQMT_REDIS_CONFIG = {'transport': 'zmq', 'account_type': %r}\n"
)


LOCAL_ROOTS = (
    "bigqmt_signal_trader",
    "bigqmt_signal_trader_strategy",
    RUNTIME,
    CANONICAL,
)


def _local_module_names():
    return [name for name in list(sys.modules)
            if any(name == root or name.startswith(root + ".")
                   for root in LOCAL_ROOTS)]


def _entry_source(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def _loader_namespace(path):
    """Exec the loader half of an entry file, stopping before the QMT half.

    Everything below ``_stop_previous_rpc_service()`` touches the live process
    (sys.modules, importlib, the RPC service), so the cut is exactly there.
    """
    source = _entry_source(path)
    prefix = source[:source.index("\n_stop_previous_rpc_service()\n")]
    namespace = {"__file__": path, "__name__": "bigqmt_entry_under_test"}
    exec(compile(prefix, path, "exec"), namespace)
    return namespace


class _LoaderCase(unittest.TestCase):
    ENTRY_PATH = ENTRY

    STOCK_ACCOUNT = "1000000011"
    CREDIT_ACCOUNT = "2000000022"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bigqmt261-")
        self._write(CANONICAL + ".py",
                    CONFIG_TEMPLATE % (self.STOCK_ACCOUNT, "STOCK", "STOCK"))
        self._write(CREDIT + ".py",
                    CONFIG_TEMPLATE % (self.CREDIT_ACCOUNT, "CREDIT", "CREDIT"))
        self._write(RUNTIME + ".py", RUNTIME_STUB)
        # Put sys.modules back exactly afterwards. Two of the loader entry
        # points under test clear the shared import cache -- _clear_local_
        # modules() drops the whole package, _purge_package_modules() drops
        # bigqmt_signal_trader.* -- and a test that does that without
        # restoring breaks whatever runs after it: modules already holding
        # references get objects the fresh import no longer produces. The same
        # omission cost 15 failures in test_transports.py /
        # test_xtquant_compat.py while this file was being written, and is why
        # test_reload_deployment.py has RestoresImports.
        self._saved_modules = dict(
            (name, sys.modules[name]) for name in _local_module_names())
        # A real runtime already imported by an earlier test would be returned
        # as-is by the loader, so the stub below would never run.
        for name in (CANONICAL, CREDIT, RUNTIME):
            sys.modules.pop(name, None)
        self.ns = _loader_namespace(self.ENTRY_PATH)
        # The fixture directory stands in for the QMT python directory.
        self.ns["_SOURCE_ROOT"] = self.tmpdir

    def tearDown(self):
        for name in _local_module_names():
            sys.modules.pop(name, None)
        sys.modules.update(self._saved_modules)
        strategy = sys.modules.get("bigqmt_signal_trader_strategy")
        if strategy is not None:
            strategy._rebind_module_level_imports()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, name, text):
        path = os.path.join(self.tmpdir, name)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _resolve(self, chosen=None):
        if chosen is not None:
            self.ns["BIGQMT_LOCAL_CONFIG_MODULE"] = chosen
        return self.ns["_resolve_local_config_module"]()

    def _import_config(self):
        return self.ns["_local_import"](CANONICAL, self.ns, None, ("*",), 0)


class ResolutionTest(_LoaderCase):
    def test_the_default_still_reads_the_canonical_module(self):
        """No global set: one-account deployments must not change at all."""
        self.assertEqual(self._resolve(), CANONICAL)

        config = self._import_config()

        self.assertEqual(config.BIGQMT_ACCOUNT_ID, self.STOCK_ACCOUNT)
        self.assertEqual(config.__file__,
                         os.path.join(self.tmpdir, CANONICAL + ".py"))

    def test_the_named_module_is_read_instead(self):
        self.assertEqual(self._resolve(CREDIT), CREDIT)

        config = self._import_config()

        self.assertEqual(config.BIGQMT_ACCOUNT_ID, self.CREDIT_ACCOUNT)
        self.assertEqual(config.BIGQMT_ACCOUNT_TYPE, "CREDIT")

    def test_it_is_registered_under_the_canonical_name(self):
        """The whole point: the runtime and the strategy import that name
        literally, so the alias -- not a second module -- is what they get."""
        self._resolve(CREDIT)

        config = self._import_config()

        self.assertEqual(config.__name__, CANONICAL)
        self.assertIs(sys.modules[CANONICAL], config)
        self.assertEqual(config.__file__,
                         os.path.join(self.tmpdir, CREDIT + ".py"))

    def test_an_empty_value_means_the_default(self):
        for value in ("", "   ", None):
            self.assertEqual(self._resolve(value), CANONICAL, repr(value))

    def test_naming_the_canonical_module_is_not_special_cased_into_a_miss(self):
        self.assertEqual(self._resolve(CANONICAL), CANONICAL)

        self.assertEqual(self._import_config().BIGQMT_ACCOUNT_ID,
                         self.STOCK_ACCOUNT)

    def test_a_py_suffix_is_tolerated(self):
        """"...config_credit.py" is the obvious slip and its intent is not
        ambiguous -- failing on it would only cost a restart."""
        self.assertEqual(self._resolve(CREDIT + ".py"), CREDIT)

        self.assertEqual(self._import_config().BIGQMT_ACCOUNT_ID,
                         self.CREDIT_ACCOUNT)


class ImportSitesTest(_LoaderCase):
    """(a) Both sites that import the name by hand get the chosen file."""

    def test_the_runtime_import_line_sees_the_chosen_account(self):
        self._resolve(CREDIT)

        runtime = self.ns["_local_import"](RUNTIME, self.ns, None, ("*",), 0)

        self.assertEqual(runtime.ACCOUNT_ID, self.CREDIT_ACCOUNT)
        self.assertEqual(runtime.ACCOUNT_TYPE, "CREDIT")

    def test_importlib_import_module_sees_it_too(self):
        """account_type_map.py reaches the config this way."""
        self._resolve(CREDIT)

        config = self.ns["_local_import_module"](CANONICAL)

        self.assertEqual(config.BIGQMT_ACCOUNT_ID, self.CREDIT_ACCOUNT)


class SurvivesReloadTest(_LoaderCase):
    def test_b_importlib_reload_in_detect_account_id(self):
        """_detect_account_id reloads the config on every init(); under the
        sandbox that is _local_reload."""
        self._resolve(CREDIT)
        config = self._import_config()

        reloaded = self.ns["_local_reload"](config)

        self.assertIs(reloaded, config)
        self.assertEqual(reloaded.BIGQMT_ACCOUNT_ID, self.CREDIT_ACCOUNT)

    def test_c_reload_deployment_leaves_the_config_alone(self):
        """The purge takes bigqmt_signal_trader[.*] -- the config module's name
        starts with 'bigqmt_signal_trader_', which is not that prefix. Pinned
        because a purge widened to a plain startswith would silently drop the
        alias and the next import would read the other account's file.

        setUp/tearDown put sys.modules back: the purge clears a cache the rest
        of the suite shares.
        """
        import bigqmt_signal_trader_strategy as strategy

        self._resolve(CREDIT)
        config = self._import_config()

        purged = strategy._purge_package_modules()

        self.assertNotIn(CANONICAL, purged)
        self.assertIs(sys.modules.get(CANONICAL), config)
        self.assertEqual(sys.modules[CANONICAL].BIGQMT_ACCOUNT_ID,
                         self.CREDIT_ACCOUNT)

    def test_d_the_next_entry_run_drops_it_cleanly(self):
        """Switching a running terminal from one config to the other has to
        work, and _clear_local_modules is what makes the next run re-read."""
        self._resolve(CREDIT)
        self._import_config()

        self.ns["_clear_local_modules"]()

        self.assertNotIn(CANONICAL, sys.modules)

        second = _loader_namespace(self.ENTRY_PATH)
        second["_SOURCE_ROOT"] = self.tmpdir
        second["_resolve_local_config_module"]()

        config = second["_local_import"](CANONICAL, second, None, ("*",), 0)
        self.assertEqual(config.BIGQMT_ACCOUNT_ID, self.STOCK_ACCOUNT)


class BadValueTest(_LoaderCase):
    def test_a_missing_file_names_the_file_it_looked_for(self):
        with self.assertRaises(RuntimeError) as caught:
            self._resolve(CANONICAL + "_nope")

        message = str(caught.exception)
        self.assertIn(CANONICAL + "_nope.py", message)
        self.assertIn(self.tmpdir, message)

    def test_a_missing_file_refuses_rather_than_falling_back(self):
        """A fallback here starts the instance on the OTHER account."""
        try:
            self._resolve(CANONICAL + "_nope")
        except RuntimeError:
            pass

        self.assertEqual(self.ns["_LOCAL_MODULE_SOURCE_OVERRIDES"], {})
        self.assertNotIn(CANONICAL, sys.modules)

    def test_a_path_is_rejected_with_a_message_about_module_names(self):
        for value in (os.path.join(self.tmpdir, CREDIT + ".py"),
                      "sub/" + CREDIT,
                      "package." + CREDIT):
            with self.assertRaises(RuntimeError) as caught:
                self._resolve(value)
            self.assertIn("module name", str(caught.exception))

    def test_resolving_again_clears_a_previous_choice(self):
        self._resolve(CREDIT)

        self.assertEqual(self._resolve(""), CANONICAL)
        self.assertEqual(self.ns["_LOCAL_MODULE_SOURCE_OVERRIDES"], {})


class NoRedisEntryTest(ResolutionTest):
    """The no-redis fork is hand-maintained and drifts silently (see
    CLAUDE.md); it needs the same knob or no-redis deployments -- the ones
    that cannot use redis channel isolation either -- have no multi-account
    story at all."""

    ENTRY_PATH = NO_REDIS_ENTRY


class SourceTest(unittest.TestCase):
    def test_neither_entry_assigns_the_global(self):
        """BIGQMT_ZMQ_DRYRUN.py execs the entry source into its OWN globals, so
        a default assignment in the entry would overwrite what a copy of that
        wrapper set -- exactly why BIGQMT_FORCE_TRANSPORT is only ever read
        with globals().get()."""
        for path in (ENTRY, NO_REDIS_ENTRY):
            for line in _entry_source(path).splitlines():
                self.assertFalse(
                    line.startswith("BIGQMT_LOCAL_CONFIG_MODULE"),
                    "%s assigns BIGQMT_LOCAL_CONFIG_MODULE at module level; "
                    "read it with globals().get() instead"
                    % os.path.basename(path))

    def test_the_startup_line_names_the_module_that_was_read(self):
        """Two instances in one terminal: the startup line is the only place a
        config that did not take effect shows up."""
        self.assertIn("local rpc config loaded module=%s", _entry_source(ENTRY))
        self.assertIn("no-redis mode: module=%s", _entry_source(NO_REDIS_ENTRY))

    def test_the_two_hardcoded_import_sites_stay_canonical(self):
        """They are what the alias exists for. Parameterising them instead
        would break the reload paths pinned above."""
        runtime = _entry_source(os.path.join(SRC, RUNTIME + ".py"))
        self.assertIn(
            "from bigqmt_signal_trader_local_config import "
            "BIGQMT_ACCOUNT_ID, BIGQMT_REDIS_CONFIG", runtime)
        strategy = _entry_source(
            os.path.join(SRC, "bigqmt_signal_trader_strategy.py"))
        self.assertIn(
            "import bigqmt_signal_trader_local_config as _local_config",
            strategy)


if __name__ == "__main__":
    unittest.main()
