"""The "order not found" message must name the cause it usually has (#122).

chinapsu's order came back SUBMITTED and then could not be found, 62 lookups
running. The cause was QMT's 模型交易 run mode: the 运行模式 column defaults to
模拟, and in that mode passorder matches internally and never reaches the
broker. The call succeeds, the id never arrives, positions and cash do not
move.

The message led with "check price range / permissions". adrian-liuc followed
that for two hours before finding the mode, then asked for the hint to be in
the text -- which is the whole point of a diagnostic that fires on a path the
caller cannot see.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import io

SOURCE_PATH = os.path.join(ROOT, "src", "bigqmt_signal_trader", "redis_rpc.py")


def _message_block():
    with io.open(SOURCE_PATH, encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("passorder submitted but order not found in system")
    return source[start:start + 900]


class LeadsWithTheRunModeTest(unittest.TestCase):
    def test_it_mentions_the_run_mode(self):
        self.assertIn("运行模式", _message_block())

    def test_it_names_both_settings_by_their_ui_labels(self):
        """A reader has to find them in QMT's own window."""
        block = _message_block()

        self.assertIn("模型交易", block)
        self.assertIn("模拟", block)
        self.assertIn("实盘", block)

    def test_the_run_mode_comes_before_price_and_permissions(self):
        """Order matters: the old text put the rare cause first and cost a
        reporter two hours."""
        block = _message_block()

        self.assertLess(block.index("运行模式"), block.index("price range"))

    def test_it_says_a_simulated_account_stays_simulated(self):
        """Otherwise "switch to 实盘" reads as "risk real money"."""
        self.assertIn("simulated account stays simulated", _message_block())

    def test_price_and_permissions_are_still_offered(self):
        """They are real causes once the mode is right -- demoted, not dropped."""
        block = _message_block()

        self.assertIn("price range", block)
        self.assertIn("permissions", block)

    def test_the_editor_window_is_named_too(self):
        """QMT's own docs: orders placed from the editor never become real."""
        self.assertIn("editor", _message_block())


class KeepsTheEvidenceTest(unittest.TestCase):
    """The numbers that let someone confirm which order this was."""

    def test_it_still_reports_the_order(self):
        block = _message_block()

        for field in ("stock=%s", "action=%s", "price=%s", "volume=%d"):
            self.assertIn(field, block, field)
        self.assertNotIn("price=%.2f", block)

    def test_it_still_reports_the_lookup_count(self):
        self.assertIn("lookup(s)", _message_block())


if __name__ == "__main__":
    unittest.main()
