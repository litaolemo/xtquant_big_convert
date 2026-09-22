# coding: utf-8
"""#339: a failed server-side download that the client pull could NOT save
must not come back as {finished: total}.

download_history_data2 treats the server-side download as best-effort while
the local cache is enabled, because the pull that follows can still save it
when the bars are already on the server. When that pull comes back with zero
rows the download really did not happen -- reporting {finished: total} there
is the fake progress of #47 again, just one branch over (#339).

A code that is legitimately empty (suspended, delisted) after a download that
did NOT fail keeps the old tolerant behaviour: the poll times out and the
code is reported finished.
"""
import shutil
import tempfile
import unittest

from tests.bigqmt_signal_trader.test_local_cache import FakeClient


class _EmptyPullClient(FakeClient):
    """Server-side download raises; the pull answers zero rows (the #339
    shape: nothing landed on the terminal, nothing for the client to save)."""

    def __init__(self, cache_dir, download_error=True):
        super(_EmptyPullClient, self).__init__(cache_dir)
        self.download_error = download_error

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append(method)
        self.call_params.append((method, params))
        if method == "download_history_data2":
            if self.download_error:
                raise RuntimeError("download_history_data2 unavailable on this terminal")
            return False
        if method == "get_market_data_ex":
            import pandas as pd
            codes = (params or {}).get("stock_list") or []
            return {c: pd.DataFrame({"stime": [], "close": []}) for c in codes}
        raise AssertionError("unexpected rpc: %s" % method)


class FailedDownloadWithEmptyPullTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _xt(self, download_error=True):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData
        return BigQmtXtData(_EmptyPullClient(self.dir, download_error=download_error))

    def test_failed_download_and_empty_pull_raises_instead_of_finished(self):
        xt = self._xt()
        seen = []
        with self.assertRaisesRegex(RuntimeError, "unavailable on this terminal") as caught:
            xt.download_history_data2(["600519.SH"], "tick", "20260910", "20260910",
                                      callback=seen.append, data_wait_seconds=0)
        # The message names the code that got nothing, so the caller can tell
        # which part of a batch is missing.
        self.assertIn("600519.SH", str(caught.exception))
        self.assertEqual(seen, [], "no per-code progress for a download that did not happen")

    def test_failed_download_but_pull_has_rows_still_finishes(self):
        """The best-effort contract stands when the pull saves it."""
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData
        client = FakeClient(self.dir)
        original = client.call

        def failing(method, params=None, account_id=None, timeout_seconds=None):
            if method == "download_history_data2":
                raise RuntimeError("global not available")
            return original(method, params, account_id=account_id, timeout_seconds=timeout_seconds)

        client.call = failing
        xt = BigQmtXtData(client)
        result = xt.download_history_data2(["600000.SH"], "1d", data_wait_seconds=0)
        self.assertEqual(result, {"finished": 1, "total": 1})

    def test_download_ok_and_empty_pull_keeps_tolerant_finish(self):
        """A download that did not fail plus an empty code (suspended /
        delisted) is the case the poll timeout was written for: unchanged."""
        xt = self._xt(download_error=False)
        result = xt.download_history_data2(["600519.SH"], "tick", "20260910", "20260910",
                                           data_wait_seconds=0)
        self.assertEqual(result, {"finished": 1, "total": 1})


if __name__ == "__main__":
    unittest.main()


class PollBudgetDefaultTest(unittest.TestCase):
    """#339: one code with no data held its whole batch for the old 60s
    default. The server download returns after the data landed, so a few
    polls is the budget; the RPC timeout of a pull is not shrunk with it."""

    def test_the_default_wait_is_ten_seconds(self):
        import inspect
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData
        sig = inspect.signature(BigQmtXtData.download_history_data2)
        self.assertEqual(10.0, sig.parameters["data_wait_seconds"].default)

    def test_a_short_wait_does_not_shorten_the_pull_rpc_timeout(self):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData
        seen = {}
        xt = BigQmtXtData.__new__(BigQmtXtData)

        class Client(object):
            account_id = "acct"

            def call(self_, method, params=None, account_id=None, timeout_seconds=None, use_formula=True, request_id=None):
                return True

        xt.client = Client()
        xt._local_cache = lambda: object()
        xt.get_market_data_ex = lambda **kw: seen.setdefault("timeout", kw.get("timeout_seconds")) and {}
        xt._pull_and_cache = lambda *a, **k: {}
        try:
            xt.download_history_data2(["600000.SH"], "1d", data_wait_seconds=0)
        except Exception:
            pass
        self.assertGreaterEqual(seen.get("timeout") or 0, 60.0)
