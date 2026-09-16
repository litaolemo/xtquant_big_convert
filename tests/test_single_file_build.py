"""The single-file QMT builds must actually build and load (issues #56, #76).

Some brokers' QMT sandboxes reject external imports and file loading outright,
so the only way in is one self-contained strategy file. tools/build_single_file
and tools/build_no_redis_single_file_flat generate those.

The flat builder execs every embedded module inside a ``def _mod_N():`` body.
That is precisely the shape that made ``from xtquant.xtconstant import *`` a
SyntaxError in issue #76 -- a bug the entire rest of the suite stayed green
through, because nothing else compiles a module inside a function. These tests
exist so the single-file builds are checked rather than silently rotting: they
run the real builders and compile what comes out.
"""

import io
import os
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")

BUILDERS = {
    "redis": ("build_single_file.py", "utf-8"),
    "no_redis_flat": ("build_no_redis_single_file_flat.py", "gbk"),
}

_built = {}


def build(kind):
    """Run a builder once per session and return (path, source)."""
    if kind in _built:
        return _built[kind]
    script, encoding = BUILDERS[kind]
    handle, out_path = tempfile.mkstemp(suffix="_%s.py" % kind)
    os.close(handle)
    env = dict(os.environ, BIGQMT_BUILD_OUT=out_path)
    completed = subprocess.run(
        [sys.executable, os.path.join(TOOLS, script)],
        cwd=TOOLS, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if completed.returncode != 0:
        raise AssertionError("%s failed:\n%s"
                             % (script, completed.stdout.decode("utf-8", "replace")))
    with io.open(out_path, encoding=encoding) as stream:
        source = stream.read()
    _built[kind] = (out_path, source)
    return _built[kind]


class BuildsSucceedTest(unittest.TestCase):
    def test_redis_build_compiles(self):
        path, source = build("redis")
        compile(source, path, "exec")

    def test_no_redis_flat_build_compiles(self):
        """The one that wraps every module in a function body."""
        path, source = build("no_redis_flat")
        compile(source, path, "exec")

    def test_flat_build_really_does_wrap_modules_in_functions(self):
        """If this stops being true, the compile test above stops being a
        regression guard for #76 and nobody would notice."""
        _path, source = build("no_redis_flat")
        match = re.search(
            r'"bigqmt_signal_trader/xtquant_compat\.py": (_mod_\d+),', source)
        self.assertIsNotNone(match, "xtquant_compat not embedded")
        self.assertIn("def %s():" % match.group(1), source)

    def test_no_star_imports_survive_into_either_build(self):
        for kind in BUILDERS:
            _path, source = build(kind)
            offenders = re.findall(r"^\s*from \S+ import \*\s*$", source, re.M)
            self.assertEqual(offenders, [], "%s build: %s" % (kind, offenders))


class ConfigBlockTest(unittest.TestCase):
    """The template config ships to users; a contributor's own settings must
    not ride along. The flat builder arrived carrying a real account id and
    rpc_allow_order_methods=True."""

    def test_account_id_is_a_placeholder(self):
        for kind in BUILDERS:
            _path, source = build(kind)
            self.assertIn('BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"', source, kind)

    def test_no_bare_digit_account_id(self):
        for kind in BUILDERS:
            _path, source = build(kind)
            leaked = re.findall(r'BIGQMT_ACCOUNT_ID = "(\d{4,})"', source)
            self.assertEqual(leaked, [], "%s build leaked an account id" % kind)

    def test_order_rpc_is_off_by_default(self):
        """Remote order/cancel must be opt-in, matching the example config."""
        for kind in BUILDERS:
            _path, source = build(kind)
            self.assertIn('"rpc_allow_order_methods": False,', source, kind)
            self.assertNotIn('"rpc_allow_order_methods": True,', source, kind)

    def test_background_threads_stay_off(self):
        """get_trade_detail_data returns empty off the main strategy thread."""
        for kind in BUILDERS:
            _path, source = build(kind)
            self.assertIn('"rpc_background_threads": False,', source, kind)

    def test_account_type_is_carried_through(self):
        """Added in #68; the templates arrived without it."""
        for kind in BUILDERS:
            _path, source = build(kind)
            self.assertIn("BIGQMT_ACCOUNT_TYPE", source, kind)

    def test_template_config_matches_the_documented_example(self):
        """Drift between the two silently gives single-file users a different
        default than everyone else."""
        example = os.path.join(
            ROOT, "src", "bigqmt_signal_trader_local_config.example.py")
        with io.open(example, encoding="utf-8") as stream:
            example_source = stream.read()
        keys = re.findall(r'^\s{4}"([a-z_]+)":', example_source, re.M)
        self.assertTrue(keys, "no keys parsed from the example config")

        for kind in BUILDERS:
            _path, source = build(kind)
            missing = [key for key in keys if '"%s":' % key not in source]
            self.assertEqual(missing, [], "%s build missing %s" % (kind, missing))


class EmbeddedContentTest(unittest.TestCase):
    def test_both_builds_embed_the_whole_package(self):
        sys.path.insert(0, TOOLS)
        import build_single_file

        package = os.path.join(ROOT, "src", "bigqmt_signal_trader")
        expected = sum(
            1
            for directory, dirs, files in os.walk(package)
            for name in files
            if name.endswith(".py")
            and "__pycache__" not in directory
            and name not in build_single_file.EXCLUDED_MODULES
        )
        for kind in BUILDERS:
            _path, source = build(kind)
            found = len(re.findall(r'"bigqmt_signal_trader/[\w/]+\.py"', source))
            self.assertGreaterEqual(
                found, expected, "%s build embedded %d of %d package modules"
                % (kind, found, expected))

    def test_setup_tooling_is_kept_out_of_the_strategy_builds(self):
        """init_config imports subprocess and getpass. QMT's whitelist has
        already rejected socket here, so setup tooling has no business riding
        along in a file that runs inside the sandbox."""
        for kind in BUILDERS:
            _path, source = build(kind)
            self.assertNotIn('"bigqmt_signal_trader/init_config.py"', source, kind)

    def test_excluding_a_module_something_imports_fails_the_build(self):
        """An earlier attempt at trimming this build excluded a module that was
        imported at the top level of another, and it died on load instead of at
        build time."""
        sys.path.insert(0, TOOLS)
        import build_single_file

        top_dir, package_dir = build_single_file.resolve_source_dirs()
        with self.assertRaises(SystemExit):
            build_single_file.collect_package(
                package_dir, top_dir, excluded=("redis_common.py",))

    def test_constant_backfill_survives_the_indentation_pass(self):
        """The flat builder re-indents sources into function bodies; the #76
        fix must come out the other side intact."""
        _path, source = build("no_redis_flat")
        self.assertIn("for _const_name in dir(_xtconstant):", source)


def tearDownModule():
    for path, _source in _built.values():
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
