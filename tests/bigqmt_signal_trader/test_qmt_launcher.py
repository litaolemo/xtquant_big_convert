# coding: utf-8
"""qmt_launcher tests (issue #45).

The property that matters most is directory scoping: this machine runs several
QMT installs side by side, so a name-only match would stop the wrong account's
terminal. Process enumeration is stubbed so the tests never touch real ones.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import qmt_launcher
from bigqmt_signal_trader.qmt_launcher import (
    QmtLauncherError,
    close_qmt,
    find_qmt_processes,
    is_qmt_running,
    resolve_install_dir,
)


LEMO = os.path.normpath("D:/qmt_lemo/bin.x64")
OTHER = os.path.normpath("D:/qmt_other/bin.x64")

FAKE_PROCESSES = [
    (100, "XtItClient.exe", os.path.join(LEMO, "XtItClient.exe")),
    (101, "miniquote.exe", os.path.join(LEMO, "miniquote.exe")),
    (200, "XtItClient.exe", os.path.join(OTHER, "XtItClient.exe")),  # another account
    (300, "notepad.exe", os.path.join(LEMO, "notepad.exe")),         # not ours
    (400, "XtItClient.exe", ""),                                     # unreadable path
]


class ProcessStub(object):
    """Patch process enumeration and record what gets terminated."""

    def __init__(self, processes=None):
        self.processes = list(FAKE_PROCESSES if processes is None else processes)
        self.terminated = []
        self._orig_iter = None
        self._orig_term = None
        self._orig_isdir = None

    def __enter__(self):
        self._orig_iter = qmt_launcher._iter_processes
        self._orig_term = qmt_launcher._terminate
        self._orig_isdir = os.path.isdir

        def _terminate(pid, force=False):
            self.terminated.append((pid, force))
            self.processes = [p for p in self.processes if p[0] != pid]
            return True

        qmt_launcher._iter_processes = lambda: list(self.processes)
        qmt_launcher._terminate = _terminate
        os.path.isdir = lambda p: True
        return self

    def __exit__(self, *exc):
        qmt_launcher._iter_processes = self._orig_iter
        qmt_launcher._terminate = self._orig_term
        os.path.isdir = self._orig_isdir
        return False


class ResolveInstallDirTest(unittest.TestCase):
    def test_accepts_root_bin_and_exe_paths(self):
        expected = os.path.abspath("D:/qmt_lemo/bin.x64")
        orig = os.path.isdir
        os.path.isdir = lambda p: os.path.basename(os.path.normpath(p)).lower() == "bin.x64"
        try:
            for given in ("D:/qmt_lemo", "D:/qmt_lemo/bin.x64",
                          "D:/qmt_lemo/bin.x64/XtItClient.exe"):
                self.assertEqual(os.path.normpath(resolve_install_dir(given)), expected, given)
        finally:
            os.path.isdir = orig

    def test_strips_surrounding_quotes(self):
        self.assertTrue(resolve_install_dir('"D:/qmt_lemo/bin.x64"').endswith("bin.x64"))

    def test_empty_is_rejected(self):
        for value in ("", None, "   "):
            with self.assertRaises(QmtLauncherError):
                resolve_install_dir(value)


class FindProcessesTest(unittest.TestCase):
    def test_only_matches_the_requested_install(self):
        with ProcessStub():
            pids = sorted(p[0] for p in find_qmt_processes("D:/qmt_lemo"))
        self.assertEqual(pids, [100, 101])  # not 200 (other install)

    def test_other_install_is_found_independently(self):
        with ProcessStub():
            pids = sorted(p[0] for p in find_qmt_processes("D:/qmt_other"))
        self.assertEqual(pids, [200])

    def test_ignores_unrelated_process_names(self):
        with ProcessStub():
            names = [p[1] for p in find_qmt_processes("D:/qmt_lemo")]
        self.assertNotIn("notepad.exe", names)

    def test_skips_processes_with_no_readable_path(self):
        """Matching a QMT name with an unknown path would be a coin flip on
        which install it belongs to -- skip rather than guess."""
        with ProcessStub():
            pids = [p[0] for p in find_qmt_processes("D:/qmt_lemo")]
        self.assertNotIn(400, pids)

    def test_is_qmt_running_reflects_scope(self):
        with ProcessStub():
            self.assertTrue(is_qmt_running("D:/qmt_lemo"))
            self.assertFalse(is_qmt_running("D:/qmt_unused"))


class CloseQmtTest(unittest.TestCase):
    def test_closes_only_the_requested_install(self):
        with ProcessStub() as stub:
            closed = close_qmt("D:/qmt_lemo")
        self.assertEqual(closed, 2)
        self.assertEqual(sorted(pid for pid, _ in stub.terminated), [100, 101])
        self.assertNotIn(200, [pid for pid, _ in stub.terminated])

    def test_terminates_gracefully_before_forcing(self):
        """A hard kill skips the terminal's data flush and truncates the
        local K-line store."""
        with ProcessStub() as stub:
            close_qmt("D:/qmt_lemo")
        self.assertTrue(all(force is False for _, force in stub.terminated))

    def test_escalates_to_force_when_terminate_is_ignored(self):
        with ProcessStub() as stub:
            def _stubborn(pid, force=False):
                stub.terminated.append((pid, force))
                if force:
                    stub.processes = [p for p in stub.processes if p[0] != pid]
                return True

            qmt_launcher._terminate = _stubborn
            close_qmt("D:/qmt_lemo", timeout_seconds=6.0, force_after_seconds=1.0)

        self.assertTrue(any(force for _, force in stub.terminated))

    def test_no_processes_is_not_an_error(self):
        with ProcessStub(processes=[]):
            self.assertEqual(close_qmt("D:/qmt_lemo"), 0)


class ReadinessTest(unittest.TestCase):
    def test_wait_until_ready_raises_on_timeout(self):
        """A scheduled restart must fail loudly, not hand the next step a
        terminal that never came up."""
        orig = qmt_launcher.port_is_listening
        qmt_launcher.port_is_listening = lambda *a, **k: False
        try:
            with self.assertRaises(QmtLauncherError):
                qmt_launcher.wait_until_ready(port=1, timeout_seconds=0.3, poll_interval=0.1)
        finally:
            qmt_launcher.port_is_listening = orig

    def test_wait_until_ready_returns_elapsed_seconds(self):
        orig = qmt_launcher.port_is_listening
        qmt_launcher.port_is_listening = lambda *a, **k: True
        try:
            self.assertIsInstance(
                qmt_launcher.wait_until_ready(port=1, timeout_seconds=5.0), float)
        finally:
            qmt_launcher.port_is_listening = orig


class OpenQmtTest(unittest.TestCase):
    def _stub_spawn(self):
        calls = []
        orig_spawn = qmt_launcher._spawn
        orig_ready = qmt_launcher.wait_until_ready
        qmt_launcher._spawn = lambda cmd, cwd=None, shell=False: calls.append((cmd, cwd, shell))
        qmt_launcher.wait_until_ready = lambda *a, **k: 1.0
        return calls, orig_spawn, orig_ready

    def test_linkmini_mode_passes_the_passwordless_flag(self):
        calls, orig_spawn, orig_ready = self._stub_spawn()
        orig_isdir, orig_isfile = os.path.isdir, os.path.isfile
        os.path.isdir = lambda p: True
        os.path.isfile = lambda p: p.lower().endswith("xtminiqmt.exe")
        try:
            qmt_launcher.open_qmt("D:/qmt_lemo", mode="linkmini")
        finally:
            qmt_launcher._spawn, qmt_launcher.wait_until_ready = orig_spawn, orig_ready
            os.path.isdir, os.path.isfile = orig_isdir, orig_isfile

        self.assertEqual(len(calls), 1)
        self.assertIn("linkMini", calls[0][0])

    def test_login_mode_without_credentials_is_rejected(self):
        calls, orig_spawn, orig_ready = self._stub_spawn()
        orig_isdir, orig_isfile = os.path.isdir, os.path.isfile
        os.path.isdir = lambda p: True
        os.path.isfile = lambda p: True
        try:
            with self.assertRaises(QmtLauncherError):
                qmt_launcher.open_qmt("D:/qmt_lemo", mode="login", credentials={})
        finally:
            qmt_launcher._spawn, qmt_launcher.wait_until_ready = orig_spawn, orig_ready
            os.path.isdir, os.path.isfile = orig_isdir, orig_isfile

    def test_unknown_mode_is_rejected(self):
        orig = os.path.isdir
        os.path.isdir = lambda p: True
        try:
            with self.assertRaises(QmtLauncherError):
                qmt_launcher.open_qmt("D:/qmt_lemo", mode="teleport")
        finally:
            os.path.isdir = orig

    def test_bat_mode_requires_an_existing_bat(self):
        orig = os.path.isdir
        os.path.isdir = lambda p: True
        try:
            with self.assertRaises(QmtLauncherError):
                qmt_launcher.open_qmt("D:/qmt_lemo", mode="bat", bat_path="D:/nope.bat")
        finally:
            os.path.isdir = orig


class LoginWindowDetectionTest(unittest.TestCase):
    def test_known_styles_do_not_depend_on_dpi_or_window_size(self):
        self.assertEqual(qmt_launcher.classify_qmt_window(0x96000000), "login")
        self.assertEqual(qmt_launcher.classify_qmt_window(0x960b0000), "main")
        self.assertEqual(qmt_launcher.classify_qmt_window(0x970b0000), "main")
        self.assertEqual(qmt_launcher.classify_qmt_window(0xb60b0000), "main")
        self.assertEqual(qmt_launcher.classify_qmt_window(0x96000000 - 2**32), "login")

    def test_unknown_styles_cannot_be_treated_as_login_or_main(self):
        for style in (None, "bad", 0, 0x90000000, 0x96080000, 0x960f0000, 0xd6000000):
            self.assertEqual(qmt_launcher.classify_qmt_window(style), "unknown", style)

    def test_completion_waits_through_login_unknown_and_missing_windows(self):
        handles = iter((10, 0, 30, 20))
        styles = {10: 0x96000000, 30: 0, 20: 0x960b0000}
        result = qmt_launcher._wait_for_main_window(
            lambda: next(handles),
            lambda h: qmt_launcher.classify_qmt_window(styles[h]),
            timeout_seconds=1.0, poll_interval=0.0,
        )
        self.assertEqual(result, 20)

    def test_completion_rejects_persistent_login_and_unknown_window(self):
        for style in (0x96000000, 0):
            result = qmt_launcher._wait_for_main_window(
                lambda: 10,
                lambda h: qmt_launcher.classify_qmt_window(style),
                timeout_seconds=0.0, poll_interval=0.0,
            )
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
