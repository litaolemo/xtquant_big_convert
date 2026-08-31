"""A file that is not Python must say so (issue #102).

huliangyu's strategy file had arrived corrupted -- line 1 was a 200-character
token, not code -- and the loader reported it as:

    File "...\\bigqmt_signal_trader_strategy.py", line 1, in <module>
        MiFBOecYoHXT4UUBBOIr3m5aTVbA5Rbt6OnG52cfBT5EAtPG9kA7kQnEsKuDUOORy...
    NameError: name 'MiFBOecYoHXT4UUB...' is not defined

Python read line 1 as a variable name because that is all it was. Nothing in
that message says the file is broken, so the first answer they got was "maybe
a version problem, redeploy" -- which cannot help: every version fails the same
way on that file.

The check is deliberately narrow. A Python line 40+ characters long with no
whitespace and nothing outside the base64 alphabet would have to be a bare
identifier, which is already broken, so a false positive only means a clearer
message on code that was going to fail anyway.
"""

import io
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

ENTRIES = ("BIGQMT_REDIS_DRYRUN.py", "BIGQMT_ZMQ_BACKTEST.py")

# The real one, from the reporter's screenshot.
REAL_TOKEN = ("MiFBOecYoHXT4UUBBOIr3m5aTVbA5Rbt6OnG52cfBT5EAtPG9kA7kQnEsKuDUOO"
              "RyAIDLm4V4QJ66slR3f9yxa78SEyEn4MHPBmB_yap_dtN-dqis9Wx_VkUyEt8f")


def _detector(entry):
    """Pull the check out of a standalone entry script.

    These files bootstrap the package, so they cannot import from it and the
    helper is inlined in each. Exec'ing just that function keeps the test
    honest about what actually ships.
    """
    path = os.path.join(SRC, entry)
    encoding = "gbk" if entry.endswith("BACKTEST.py") else "utf-8"
    with io.open(path, encoding=encoding) as handle:
        source = handle.read()
    start = source.index("_TOKEN_CHARS = set(")
    end = source.index("def _load_local_module(name):", start)
    namespace = {}
    exec(compile(source[start:end], path, "exec"), namespace)
    return namespace["_looks_like_a_token_not_python"]


class DetectsTest(unittest.TestCase):
    def test_the_reporters_actual_file(self):
        for entry in ENTRIES:
            self.assertTrue(_detector(entry)(REAL_TOKEN), entry)

    def test_a_leading_blank_line_does_not_hide_it(self):
        self.assertTrue(_detector(ENTRIES[0])("\n\n   \n" + REAL_TOKEN))

    def test_bytes_are_handled(self):
        """The loader reads the file in binary."""
        self.assertTrue(_detector(ENTRIES[0])(REAL_TOKEN.encode("utf-8")))


class LeavesRealPythonAloneTest(unittest.TestCase):
    """A false positive here would replace a real traceback with a wrong story."""

    def _check(self, source):
        return _detector(ENTRIES[0])(source)

    def test_the_real_strategy_file(self):
        path = os.path.join(SRC, "bigqmt_signal_trader_strategy.py")
        with io.open(path, encoding="utf-8") as handle:
            self.assertFalse(self._check(handle.read()))

    def test_every_shipped_entry_script(self):
        for entry in ENTRIES:
            encoding = "gbk" if entry.endswith("BACKTEST.py") else "utf-8"
            with io.open(os.path.join(SRC, entry), encoding=encoding) as handle:
                self.assertFalse(self._check(handle.read()), entry)

    def test_a_module_that_opens_with_a_long_import(self):
        source = ("from bigqmt_signal_trader.adapters.market_bigqmt import "
                  "BigQmtMarketDataProvider\n")
        self.assertFalse(self._check(source))

    def test_a_module_that_opens_with_a_long_dotted_expression(self):
        """Long, but it has dots -- outside the alphabet."""
        self.assertFalse(self._check("a.b.c.d.e.f.g.h.i.j.k.l.m.n.o.p.q.r.s.t.u\n"))

    def test_a_long_comment(self):
        self.assertFalse(self._check("# " + "x" * 200))

    def test_an_empty_file(self):
        self.assertFalse(self._check(""))

    def test_a_short_identifier_alone(self):
        """Broken code, but not this kind of broken -- let the NameError
        speak for itself."""
        self.assertFalse(self._check("undefined_name\n"))

    def test_it_never_raises(self):
        for value in (None, 12345, object()):
            self.assertFalse(self._check(value))


class MessageTest(unittest.TestCase):
    def _entry_source(self, entry):
        encoding = "gbk" if entry.endswith("BACKTEST.py") else "utf-8"
        with io.open(os.path.join(SRC, entry), encoding=encoding) as handle:
            return handle.read()

    def test_it_names_the_file(self):
        for entry in ENTRIES:
            self.assertIn("% source_path", self._entry_source(entry), entry)

    def test_it_says_a_different_version_will_not_help(self):
        """The answer the reporter was given first."""
        for entry in ENTRIES:
            self.assertIn("A different version will not help",
                          self._entry_source(entry), entry)

    def test_the_backtest_entry_stays_isolated(self):
        """It must not mention the live bridge -- pinned by
        test_qmt_runtime.py, and easy to break when adding shared wording."""
        self.assertNotIn("bigqmt_signal_trader",
                         self._entry_source("BIGQMT_ZMQ_BACKTEST.py"))


if __name__ == "__main__":
    unittest.main()
