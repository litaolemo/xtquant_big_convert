# coding: utf-8
"""The download readiness poll must not re-submit the download it waits for.

Issue #275, reported by @pujfei with the mechanism traced out: a single-code
``download_history_data("600418.SH", "1d", "20200101")`` took over a minute
through the bridge while the same call inside Big QMT took under a second,
and the server-side download leg itself measured 0.1--0.6s. The minute went
into the client's readiness poll.

``download_history_data2`` submits the download (QMT's global returns at
once; bars land asynchronously, #66) and then polls ``get_market_data_ex``
every 1.5s until every code has rows. That read goes through
``_heal_adjusted``, whose job is to answer an unready raw store by
triggering a raw download. In the poll the raw store is unready *by
definition* -- that is what is being waited for -- so heal fired every
round: ``_ensure_server_raw`` re-submitted the identical download, slept
2s, re-read. The landing being waited for kept getting re-queued behind a
fresh copy of itself, and the 60s budget always ran out.

The single-code case is the worst: heal's majority guard is
``missing < max(1, len(codes) // 2)``, which for one code is ``missing < 1``,
so one missing code is a majority. That guard exists to stop full-market
reads paying a heal per delisted code; it was never meant to fire on a
one-code wait.

The fix is the one the report proposed: the poll's read passes
``heal=False``. The download is still submitted exactly once (leg A), and
the wait still confirms visibility before returning (#47 / #66 unchanged).
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat  # noqa: E402
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData  # noqa: E402


class LandsLateClient(object):
    """Bars appear only on the Nth ``get_market_data_ex`` read.

    Every ``download_history_data2`` RPC is counted: the bridge must send
    exactly one per ``download_history_data2`` call, whatever the poll does.
    """

    def __init__(self, cache_dir, lands_on_read=3, not_landed="missing"):
        self.account_id = "acct"
        self.lands_on_read = lands_on_read
        # How an unready raw store answers. "missing": the code is absent
        # (trips heal's none-adjusted majority guard). "zeros": adjusted
        # bars come back as a frame of zeros (trips the all-zero detector
        # the front/back branch uses). Both are real shapes from #275.
        self.not_landed = not_landed
        self.reads = 0
        self.download_submits = []
        # The poll only runs with the local cache on -- the default, and the
        # configuration the report was filed against.
        self.local_cache_config = {"enabled": True, "dir": cache_dir, "fallback_rpc": False}

    def _redis(self):
        return None

    def call(self, method, params=None, account_id=None, timeout_seconds=None, **kw):
        params = params or {}
        if method == "download_history_data2":
            self.download_submits.append(dict(params))
            return True
        if method == "get_market_data_ex":
            self.reads += 1
            codes = list(params.get("stock_list") or [])
            import pandas as pd
            if self.reads < self.lands_on_read:
                if self.not_landed == "zeros":
                    return {c: pd.DataFrame({"stime": ["20260910"], "open": [0.0],
                                             "close": [0.0], "openInterest": [0.0]})
                            for c in codes}
                return {}                       # every code missing
            return {
                c: pd.DataFrame({"stime": ["20260910"], "close": [10.0],
                                 "openInterest": [0.0]})
                for c in codes
            }
        raise AssertionError("unexpected rpc: %s" % method)


def _no_sleep(*_args, **_kwargs):
    return None


class DownloadPollDoesNotReHealTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, codes, not_landed="missing", **kwargs):
        client = LandsLateClient(self.dir, lands_on_read=3, not_landed=not_landed)
        xt = BigQmtXtData(client)
        with mock.patch.object(xtquant_compat.time, "sleep", _no_sleep):
            xt.download_history_data2(codes, "1d", start_time="20200101", **kwargs)
        return client

    def test_a_single_code_download_is_submitted_exactly_once(self):
        """The #275 shape: one code, bars land on the third poll round."""
        client = self._run(["600418.SH"])

        self.assertEqual(1, len(client.download_submits),
                         "the poll re-submitted the download it was waiting for: %d submits"
                         % len(client.download_submits))

    def test_the_poll_still_waits_for_the_bars_to_land(self):
        """Turning heal off must not turn the visibility wait off (#66)."""
        client = self._run(["600418.SH"])

        self.assertGreaterEqual(client.reads, 3, "returned before the bars landed")

    def test_the_adjusted_branch_is_covered_too(self):
        """front/back reads hit heal through the all-zero detector instead;
        the poll is the same loop, so the same one-submit rule applies."""
        client = self._run(["600418.SH"], not_landed="zeros", dividend_type="front")

        self.assertEqual(1, len(client.download_submits),
                         "front-adjusted poll re-submitted: %d submits"
                         % len(client.download_submits))

    def test_a_direct_read_still_heals_by_default(self):
        """Only the poll opts out. A caller reading an unready code directly
        keeps the self-heal that populates the raw store (#66 unchanged)."""
        client = LandsLateClient(self.dir, lands_on_read=2)
        xt = BigQmtXtData(client)
        with mock.patch.object(xtquant_compat.time, "sleep", _no_sleep):
            xt.get_market_data_ex(stock_list=["600418.SH"], period="1d",
                                  dividend_type="none", fill_data=False)

        self.assertEqual(1, len(client.download_submits),
                         "heal must still fire on a direct unready read")


if __name__ == "__main__":
    unittest.main()
