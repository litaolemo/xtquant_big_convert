"""get_full_tick on a terminal that answers only subscribed codes (issue #310).

Jianghai big-QMT 2.1.19.0 returns {} from ContextInfo.get_full_tick for any
code without a live quote subscription -- explicit codes and whole-market
tokens alike -- while the GUI ticks and every other RPC is healthy. The
reporter proved it: subscribe_whole_quote(["600052.SH"]), wait, ask again,
full five-level book. Guojin's build answers unsubscribed codes, so the
warm-up must be invisible there.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters import market_bigqmt


QUOTES = {
    "600052.SH": {"lastPrice": 3.69, "askPrice": [3.70, 3.71]},
    "000001.SZ": {"lastPrice": 11.2, "askPrice": [11.21, 11.22]},
    "601398.SH": {"lastPrice": 8.13, "askPrice": [8.14, 8.15]},
}


class SubscribedOnlyContext(object):
    """Answers a code only while it (or its exchange token) is subscribed."""

    def __init__(self, answers_unsubscribed=False, subscribe_result=None):
        self.answers_unsubscribed = answers_unsubscribed
        self.subscribe_result = subscribe_result
        self.subscribed = set()
        self._by_handle = {}
        self.subscribe_calls = []
        self.unsubscribe_calls = []
        self.asked = []
        self._next_handle = 100

    def _live(self, code):
        if self.answers_unsubscribed:
            return True
        return code in self.subscribed or code.split(".")[-1] in self.subscribed

    def get_full_tick(self, codes):
        self.asked.append(list(codes))
        out = {}
        for code in codes:
            if "." not in code:                      # exchange token
                for quote_code, row in QUOTES.items():
                    if quote_code.endswith("." + code) and self._live(quote_code):
                        out[quote_code] = row
            elif code in QUOTES and self._live(code):
                out[code] = QUOTES[code]
        return out

    def subscribe_whole_quote(self, codes, callback=None):
        self.subscribe_calls.append(list(codes))
        if self.subscribe_result is not None:
            return self.subscribe_result
        self.subscribed.update(codes)
        self._next_handle += 1
        self._by_handle[self._next_handle] = list(codes)
        return self._next_handle

    def unsubscribe_quote(self, handle):
        self.unsubscribe_calls.append(handle)
        self.subscribed.difference_update(self._by_handle.pop(handle, []))


def _provider(context):
    provider = market_bigqmt.BigQmtMarketDataProvider.__new__(
        market_bigqmt.BigQmtMarketDataProvider)
    provider.context_info = context
    provider.get_stock_list_in_sector = lambda name: []
    # Keep the suite fast; the live default is 2s.
    provider.TICK_SUBSCRIBE_WAIT_SECONDS = 0.3
    provider.TICK_SUBSCRIBE_POLL_SECONDS = 0.01
    return provider


class WarmUpTest(unittest.TestCase):
    def test_an_unanswered_code_is_subscribed_and_read_back(self):
        context = SubscribedOnlyContext()
        provider = _provider(context)

        result = provider.get_ticks(["600052.SH"])

        self.assertEqual(context.subscribe_calls, [["600052.SH"]])
        self.assertEqual(result["600052.SH"]["askPrice"], [3.70, 3.71])
        self.assertGreaterEqual(len(context.asked), 2, "must re-read after subscribing")

    def test_a_terminal_that_answers_unsubscribed_codes_never_subscribes(self):
        context = SubscribedOnlyContext(answers_unsubscribed=True)
        provider = _provider(context)

        result = provider.get_ticks(["600052.SH", "000001.SZ"])

        self.assertEqual(context.subscribe_calls, [])
        self.assertEqual(sorted(result), ["000001.SZ", "600052.SH"])
        self.assertEqual(len(context.asked), 1)

    def test_a_second_call_reuses_the_subscription(self):
        context = SubscribedOnlyContext()
        provider = _provider(context)
        provider.get_ticks(["600052.SH"])
        asked_before = len(context.asked)

        result = provider.get_ticks(["600052.SH"])

        self.assertEqual(len(context.subscribe_calls), 1)
        self.assertEqual(len(context.asked), asked_before + 1, "warm code: one read, no wait")
        self.assertIn("600052.SH", result)

    def test_only_the_new_codes_of_a_batch_are_subscribed(self):
        context = SubscribedOnlyContext()
        provider = _provider(context)
        provider.get_ticks(["600052.SH"])

        result = provider.get_ticks(["600052.SH", "000001.SZ"])

        self.assertEqual(context.subscribe_calls, [["600052.SH"], ["000001.SZ"]])
        self.assertEqual(sorted(result), ["000001.SZ", "600052.SH"])

    def test_a_market_token_subscribes_the_token_not_the_listing(self):
        context = SubscribedOnlyContext()
        provider = _provider(context)

        result = provider.get_ticks(["SH"], types=["all"])

        self.assertEqual(context.subscribe_calls, [["SH"]])
        self.assertEqual(sorted(result), ["600052.SH", "601398.SH"])

    def test_a_code_qmt_really_has_nothing_for_waits_once(self):
        context = SubscribedOnlyContext()
        provider = _provider(context)

        first = provider.get_ticks(["600000.SH"])      # not in QUOTES: never answered
        asked_after_first = len(context.asked)
        second = provider.get_ticks(["600000.SH"])

        self.assertEqual(first, {})
        self.assertEqual(second, {})
        self.assertEqual(context.subscribe_calls, [["600000.SH"]])
        self.assertGreater(asked_after_first, 1, "first call polls until the deadline")
        self.assertEqual(len(context.asked), asked_after_first + 1, "second call does not wait")

    def test_a_failed_subscribe_returns_the_native_answer_without_waiting(self):
        context = SubscribedOnlyContext(subscribe_result=-1)
        provider = _provider(context)

        result = provider.get_ticks(["600052.SH"])

        self.assertEqual(result, {})
        self.assertEqual(len(context.asked), 1)

    def test_a_context_without_subscribe_whole_quote_is_left_alone(self):
        class Bare(object):
            def __init__(self):
                self.asked = []

            def get_full_tick(self, codes):
                self.asked.append(list(codes))
                return {}

        context = Bare()
        provider = _provider(context)

        self.assertEqual(provider.get_ticks(["600052.SH"]), {})
        self.assertEqual(len(context.asked), 1)

    def test_the_caller_s_spelling_is_still_echoed_after_the_warm_up(self):
        context = SubscribedOnlyContext()
        provider = _provider(context)

        result = provider.get_ticks(["600052.sh"])

        self.assertEqual(context.subscribe_calls, [["600052.SH"]])
        self.assertEqual(result["600052.sh"]["lastPrice"], 3.69)


class PruneTest(unittest.TestCase):
    def test_an_idle_group_is_unsubscribed_and_resubscribed_on_demand(self):
        context = SubscribedOnlyContext()
        provider = _provider(context)
        provider.TICK_SUBSCRIBE_IDLE_SECONDS = 10.0
        provider.get_ticks(["600052.SH"])
        handle = provider.tick_subscription_status()[0]["handle"]

        import time
        closed = provider.prune_tick_subscriptions(now=time.monotonic() + 11.0)

        self.assertEqual(closed, 1)
        self.assertEqual(context.unsubscribe_calls, [handle])
        self.assertEqual(provider.tick_subscription_status(), [])

        provider.get_ticks(["600052.SH"])
        self.assertEqual(len(context.subscribe_calls), 2, "pruned code is subscribed again")

    def test_a_touched_group_survives_the_prune(self):
        context = SubscribedOnlyContext()
        provider = _provider(context)
        provider.TICK_SUBSCRIBE_IDLE_SECONDS = 10.0
        provider.get_ticks(["600052.SH"])

        import time
        self.assertEqual(provider.prune_tick_subscriptions(now=time.monotonic() + 5.0), 0)
        self.assertEqual(context.unsubscribe_calls, [])

    def test_prune_before_any_subscription_is_a_no_op(self):
        provider = _provider(SubscribedOnlyContext())
        self.assertEqual(provider.prune_tick_subscriptions(), 0)


if __name__ == "__main__":
    unittest.main()


class AdjustLoopPruneHookTest(unittest.TestCase):
    """_drain_rpc_service reaches the provider through service.handlers.
    The first cut read ``service.market_data`` -- an attribute the service
    does not have -- so the periodic prune silently never ran."""

    def test_the_strategy_drain_calls_prune_on_the_handlers_provider(self):
        import bigqmt_signal_trader_strategy as strategy

        calls = []

        class Provider(object):
            def prune_tick_subscriptions(self):
                calls.append(1)
                return 0

        class Handlers(object):
            market_data = Provider()

        class Service(object):
            handlers = Handlers()

            def drain_request_queue(self, max_items=20):
                return 0

            def drain_pending(self, max_items=20, budget_seconds=None):
                return 0

        saved = strategy._rpc_service
        strategy._rpc_service = Service()
        try:
            strategy._drain_rpc_service({})
        finally:
            strategy._rpc_service = saved
        self.assertEqual([1], calls, "prune_tick_subscriptions was not reached")
