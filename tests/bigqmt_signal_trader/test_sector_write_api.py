# coding: utf-8
"""The sector write family must not fail silently (#143, from #142).

Enumerating the three channels on a live Guojin 2.1.19.0 terminal
(probe_capabilities -> sector_probe) gives:

    ContextInfo         create_sector, get_sector, get_stock_list_in_sector
    QMT injected globals  -- none at all --
    native xtdata SDK   add_sector, remove_sector, get_sector_list,
                        get_stock_list_in_sector, download_sector_data

and native get_sector_list() raises `Exception: 无法连接行情服务！`.

Two things follow, and they correct issue #143's own text:

  * create_sector_folder / add_stock_to_sector / reset_sector_stock_list /
    remove_stock_from_sector are NOT QMT globals waiting to be captured in
    _EXTRA_QMT_GLOBAL_FUNCS -- they exist on no channel. So they are composed
    from add_sector here rather than forwarded to something that isn't there.
  * the documented create_sector(parent_node, sector_name, overwrite) is
    absent everywhere too, so the signature stays (sector_name, stock_list),
    which is the shape the real SDK offers.

What actually makes these honest is the read-back: ContextInfo.create_sector
accepts the call, returns None, and changes nothing (13 sectors before, 13
after, measured). Every write now re-reads the sector and raises when nothing
changed -- a false "it failed" beats a silent "it worked".
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider


class Context(object):
    """A ContextInfo that behaves like Big QMT's: create_sector does nothing."""

    def __init__(self, sectors=None, honour_writes=False):
        self.sectors = dict(sectors or {})
        self.honour_writes = honour_writes
        self.create_calls = []

    def get_stock_list_in_sector(self, sector_name, real_timetag=-1):
        return list(self.sectors.get(sector_name, []))

    def create_sector(self, sector_name, stock_list):
        self.create_calls.append((sector_name, list(stock_list)))
        if self.honour_writes:
            self.sectors[sector_name] = list(stock_list)
        return None                      # what Big QMT actually returns


class NativeSdk(object):
    """The bundled xtdata, whose add_sector replaces the member list."""

    def __init__(self, sectors=None, fail=None, merges=False):
        self.sectors = dict(sectors or {})
        self.fail = fail
        self.merges = merges
        self.add_calls = []
        self.removed = []

    def add_sector(self, sector_name, stock_list):
        self.add_calls.append((sector_name, list(stock_list)))
        if self.fail is not None:
            raise self.fail
        if self.merges:
            existing = self.sectors.get(sector_name, [])
            merged = list(existing)
            merged.extend(c for c in stock_list if c not in existing)
            self.sectors[sector_name] = merged
        else:
            self.sectors[sector_name] = list(stock_list)
        return True

    def remove_sector(self, sector_name):
        self.removed.append(sector_name)
        self.sectors.pop(sector_name, None)
        return True

    def get_stock_list_in_sector(self, sector_name):
        return list(self.sectors.get(sector_name, []))


def _provider(context=None, native=None):
    provider = BigQmtMarketDataProvider(context_info=context or Context())
    provider._native = lambda: native
    if native is not None:
        # The read-back goes through the adapter's own reader, which prefers
        # the SDK when there is one.
        provider.get_stock_list_in_sector = (
            lambda name, real_timetag=-1: native.get_stock_list_in_sector(name))
    else:
        ctx = context or Context()
        provider.get_stock_list_in_sector = (
            lambda name, real_timetag=-1: ctx.get_stock_list_in_sector(name))
    return provider


class SilentNoOpTest(unittest.TestCase):
    """The behaviour issue #142 reported: it says nothing and does nothing."""

    def test_create_sector_raises_when_the_sector_did_not_appear(self):
        context = Context(honour_writes=False)
        provider = _provider(context)

        with self.assertRaises(RuntimeError) as caught:
            provider.create_sector("_probe", ["600000.SH"])

        self.assertIn("did not change", str(caught.exception))
        self.assertEqual(len(context.create_calls), 1, "it must still try")

    def test_the_error_names_the_cause_not_just_the_symptom(self):
        provider = _provider(Context(honour_writes=False))

        with self.assertRaises(RuntimeError) as caught:
            provider.create_sector("_probe", ["600000.SH"])

        message = str(caught.exception)
        self.assertIn("create_sector", message)
        self.assertIn("#143", message)

    def test_add_sector_is_no_longer_a_silent_no_op_either(self):
        """It used to swallow the native failure and fall through to
        create_sector, which does nothing -- so it failed silently too."""
        native = NativeSdk(fail=Exception("无法连接行情服务！"))
        provider = _provider(Context(honour_writes=False), native)

        with self.assertRaises(RuntimeError):
            provider.add_sector("_probe", ["600000.SH"])

    def test_a_terminal_with_no_channel_at_all_says_so(self):
        """NotImplementedError, not RuntimeError: "impossible here" and
        "attempted but ineffective" are different answers."""

        class Bare(object):
            def get_stock_list_in_sector(self, name, real_timetag=-1):
                return []

        provider = _provider(Bare())

        with self.assertRaises(NotImplementedError):
            provider.create_sector("_probe", ["600000.SH"])


class WorkingChannelTest(unittest.TestCase):
    """On a terminal whose quote service is up, the family must work."""

    def test_create_sector_writes_through_the_native_sdk(self):
        native = NativeSdk()
        provider = _provider(Context(), native)

        result = provider.create_sector("mine", ["600000.SH", "000001.SZ"])

        self.assertEqual(result, "mine")
        self.assertEqual(native.add_calls, [("mine", ["600000.SH", "000001.SZ"])])

    def test_reset_replaces_the_member_list(self):
        native = NativeSdk({"mine": ["600000.SH"]})
        provider = _provider(Context(), native)

        provider.reset_sector_stock_list("mine", ["000001.SZ"])

        self.assertEqual(native.sectors["mine"], ["000001.SZ"])

    def test_add_stock_keeps_the_existing_members(self):
        """Read-merge-write: a bare append would drop everything already
        there on a channel that replaces."""
        native = NativeSdk({"mine": ["600000.SH"]})
        provider = _provider(Context(), native)

        provider.add_stock_to_sector("mine", "000001.SZ")

        self.assertEqual(native.sectors["mine"], ["600000.SH", "000001.SZ"])

    def test_add_stock_is_correct_when_the_channel_merges_instead(self):
        """The two SDK generations disagree about replace-vs-merge, and this
        terminal cannot be asked -- so be right under either."""
        native = NativeSdk({"mine": ["600000.SH"]}, merges=True)
        provider = _provider(Context(), native)

        provider.add_stock_to_sector("mine", "000001.SZ")

        self.assertEqual(native.sectors["mine"], ["600000.SH", "000001.SZ"])

    def test_adding_a_code_that_is_already_there_writes_nothing(self):
        native = NativeSdk({"mine": ["600000.SH"]})
        provider = _provider(Context(), native)

        self.assertTrue(provider.add_stock_to_sector("mine", "600000.SH"))
        self.assertEqual(native.add_calls, [])

    def test_remove_stock_drops_only_that_code(self):
        native = NativeSdk({"mine": ["600000.SH", "000001.SZ"]})
        provider = _provider(Context(), native)

        provider.remove_stock_from_sector("mine", "600000.SH")

        self.assertEqual(native.sectors["mine"], ["000001.SZ"])

    def test_remove_stock_raises_when_a_merging_channel_kept_it(self):
        """The silent failure this family exists to stop: on a merge-only
        channel the removal writes cleanly and changes nothing."""
        native = NativeSdk({"mine": ["600000.SH", "000001.SZ"]}, merges=True)
        provider = _provider(Context(), native)

        with self.assertRaises(RuntimeError) as caught:
            provider.remove_stock_from_sector("mine", "600000.SH")

        self.assertIn("still present", str(caught.exception))

    def test_removing_a_code_that_is_not_there_writes_nothing(self):
        native = NativeSdk({"mine": ["600000.SH"]})
        provider = _provider(Context(), native)

        self.assertTrue(provider.remove_stock_from_sector("mine", "000001.SZ"))
        self.assertEqual(native.add_calls, [])

    def test_remove_sector_uses_the_only_channel_that_has_it(self):
        native = NativeSdk({"mine": ["600000.SH"]})
        provider = _provider(Context(), native)

        provider.remove_sector("mine")

        self.assertEqual(native.removed, ["mine"])

    def test_case_differences_do_not_confuse_membership(self):
        native = NativeSdk({"mine": ["600000.sh"]})
        provider = _provider(Context(), native)

        self.assertTrue(provider.add_stock_to_sector("mine", "600000.SH"))
        self.assertEqual(native.add_calls, [], "already a member")

    def test_an_empty_stock_code_is_rejected_rather_than_written(self):
        provider = _provider(Context(), NativeSdk({"mine": []}))

        for method in ("add_stock_to_sector", "remove_stock_from_sector"):
            with self.assertRaises(ValueError, msg=method):
                getattr(provider, method)("mine", "  ")


class NoChannelAnywhereTest(unittest.TestCase):
    def test_create_sector_folder_says_it_is_unavailable(self):
        """Named in the RPC whitelist since #130 with nothing behind it."""
        provider = _provider(Context(), NativeSdk())

        with self.assertRaises(NotImplementedError) as caught:
            provider.create_sector_folder("", "folder")

        self.assertIn("create_sector_folder", str(caught.exception))

    def test_remove_sector_without_a_native_sdk_is_unavailable(self):
        provider = _provider(Context(), None)

        with self.assertRaises(NotImplementedError):
            provider.remove_sector("mine")


class GetSectorListHonestyTest(unittest.TestCase):
    """It used to hand back 13 hardcoded names that look exactly real."""

    def test_it_raises_rather_than_returning_the_hardcoded_list(self):
        provider = _provider(Context(), None)

        with self.assertRaises(NotImplementedError) as caught:
            provider.get_sector_list()

        message = str(caught.exception)
        self.assertIn("allow_fallback", message, "the error must say the way out")
        self.assertIn("#143", message)

    def test_the_curated_names_are_still_available_on_request(self):
        provider = _provider(Context(), None)

        names = provider.get_sector_list(allow_fallback=True)

        self.assertEqual(list(names), list(provider._FALLBACK_SECTORS))

    def test_a_string_true_from_json_rpc_also_opts_in(self):
        provider = _provider(Context(), None)

        self.assertTrue(provider.get_sector_list(allow_fallback="true"))

    def test_a_real_listing_still_wins_and_needs_no_flag(self):
        native = NativeSdk()
        native.get_sector_list = lambda: ["我的自选", "沪深A股"]
        provider = _provider(Context(), native)

        self.assertEqual(provider.get_sector_list(), ["我的自选", "沪深A股"])

    def test_the_fallback_list_is_not_silently_returned_as_a_real_one(self):
        """The regression that made me give a wrong answer on #130."""
        provider = _provider(Context(), None)

        try:
            answer = provider.get_sector_list()
        except NotImplementedError:
            return
        self.assertNotEqual(list(answer), list(provider._FALLBACK_SECTORS))


class FallbackNamesAreTheOnesThisTerminalAnswersTest(unittest.TestCase):
    """Each fallback name was fed to get_stock_list_in_sector on a live
    terminal (国金 Big QMT 2.1.19.0, 2026-09-11). Two of the original thirteen
    answered with nothing.

        沪市A股  0 rows        上证A股  2318 rows
        深市A股  0 rows        深证A股  2902 rows

    2318 + 2902 = 5220 = 沪深A股, so 上证 / 深证 is the terminal's spelling.
    Funds are spelt the other way round (沪市基金 / 深市基金 answer, 上证基金 /
    深证基金 do not), so nothing here can be inferred from a rule -- which is
    exactly why a curated list needs pinning to a measurement.
    """

    def test_the_a_share_halves_use_the_spelling_that_returns_rows(self):
        provider = _provider(Context(), None)
        names = provider.get_sector_list(allow_fallback=True)

        self.assertIn("上证A股", names)
        self.assertIn("深证A股", names)

    def test_the_spellings_that_return_nothing_are_not_offered(self):
        """A fallback name that answers empty is the #143 problem in miniature."""
        provider = _provider(Context(), None)
        names = provider.get_sector_list(allow_fallback=True)

        for wrong in provider._EMPTY_ON_BIG_QMT:
            self.assertNotIn(wrong, names, "%s returns 0 rows on Big QMT" % wrong)

    def test_the_count_the_error_message_quotes_is_still_right(self):
        """get_sector_list() names the number of curated entries in its error."""
        provider = _provider(Context(), None)

        with self.assertRaises(NotImplementedError) as caught:
            provider.get_sector_list()
        self.assertIn("%d well-known names" % len(provider._FALLBACK_SECTORS),
                      str(caught.exception))

    def test_the_fund_spelling_is_left_alone(self):
        """沪市基金 / 深市基金 are the ones that answer; do not 'fix' them to 上证/深证."""
        provider = _provider(Context(), None)
        names = provider.get_sector_list(allow_fallback=True)

        self.assertIn("沪市基金", names)
        self.assertIn("深市基金", names)


class ReachableOverRpcTest(unittest.TestCase):
    def test_the_new_methods_are_whitelisted(self):
        from bigqmt_signal_trader.redis_rpc import MARKET_DATA_METHODS, READ_METHODS

        for name in ("create_sector", "create_sector_folder", "add_stock_to_sector",
                     "remove_stock_from_sector", "reset_sector_stock_list",
                     "add_sector", "remove_sector"):
            self.assertIn(name, MARKET_DATA_METHODS, name)
            self.assertIn(name, READ_METHODS, name)

    def test_the_client_layer_exposes_them(self):
        from bigqmt_signal_trader import xtquant_compat

        for name in ("create_sector", "create_sector_folder", "add_stock_to_sector",
                     "remove_stock_from_sector", "reset_sector_stock_list"):
            self.assertTrue(
                callable(getattr(xtquant_compat.BigQmtXtData, name, None)), name)

    def test_the_xtquant_shim_exposes_them(self):
        from xtquant import xtdata

        for name in ("create_sector", "create_sector_folder", "add_stock_to_sector",
                     "remove_stock_from_sector", "reset_sector_stock_list"):
            self.assertTrue(callable(getattr(xtdata, name, None)), name)


if __name__ == "__main__":
    unittest.main()
