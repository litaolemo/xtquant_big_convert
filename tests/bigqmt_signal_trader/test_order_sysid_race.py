# coding: utf-8
"""A live order must never be reported as a rejection (#152).

Reported by @willzhqiang with a live reproduction on Guojin 2.1.19.0:

    13:09:42 order_stock(510300.SH, BUY, 100, FIX_PRICE, 4.216, ...) -> -1

and an immediate readback by that exact remark found the order at the broker:

    order_sysid 635093411   status 50/REPORTED   cancelable true   frozen 421.72

He did not retry. A caller who treats -1 as ORDER_REJECTED and retries would
have double-ordered -- which is why this is the dangerous shape of the bug and
not merely a wrong return value.

The window: QMT publishes the order row before m_strOrderSysID is populated.
_apply_order_lookup found the remark, and settled anyway --

    if by_remark:
        sysid = str(getattr(by_remark[0], "order_sys_id", "") or "")
        if sysid:
            settlement.result.order_sys_id = sysid
        return True          # <- even with no id

-- so the reply went out with order_sys_id=None, and BigQmtXtTrader._order_id
turns that into -1.

The row existing is proof the order reached the broker, so the fix is to keep
waiting for the id rather than to answer without one. At the deadline it says
so in the loudest terms available: the client raises on server_error, so a
persistent empty id becomes an exception naming the remark, not a silent -1.
"""

import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.models import OrderSnapshot
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers, RedisPubSubRpcService

from test_redis_rpc import (  # noqa: E402  -- reuse the established fakes
    FakeMarketData,
    FakePositionProvider,
    FakeRedis,
)
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway


class IdArrivesLateGateway(DryRunOrderGateway):
    """The row appears immediately; the system id does not.

    ``sysid_after`` is the lookup on which m_strOrderSysID becomes populated,
    counting from 1. Before that the row is present with an empty id -- the
    exact state the reproduction caught.
    """

    def __init__(self, sysid_after=2, remark="ord-1"):
        super().__init__()
        self.sysid_after = sysid_after
        self.remark = remark
        self.lookups = 0

    def query_orders(self, account_id, strategy_name):
        self.lookups += 1
        sysid = "635093411" if self.lookups >= self.sysid_after else ""
        return [
            OrderSnapshot(
                order_sys_id=sysid,
                user_order_id=self.remark,
                stock_code="510300.SH",
                action="BUY",
                volume=100,
                traded_volume=0,
                status="50",
                price=4.216,
            )
        ]


class _Service(unittest.TestCase):
    def _service(self, gateway, timeout=5.0):
        redis_client = FakeRedis()
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway,
            allow_order_methods=True, order_settle_timeout_seconds=timeout,
        )
        return redis_client, RedisPubSubRpcService(
            redis_client, handlers, account_id="acct")

    def _submit(self, service, request_id="ord-1"):
        service.enqueue_payload({
            "request_id": request_id, "account_id": "acct", "method": "order_stock",
            "params": {"stock_code": "510300.SH", "order_type": 23,
                       "order_volume": 100, "price_type": 11, "price": 4.216,
                       "order_remark": request_id},
        })

    def _response(self, redis_client, request_id="ord-1"):
        key = "bigqmt:rpc:resp:acct:%s" % request_id
        return json.loads(redis_client.kv[key]) if key in redis_client.kv else None


class WaitsForTheIdTest(_Service):
    def test_a_remark_match_with_no_id_yet_stays_parked(self):
        """The fix, stated directly: an empty id is not a settled order."""
        gateway = IdArrivesLateGateway(sysid_after=2)
        redis_client, service = self._service(gateway)
        self._submit(service)

        service.drain_pending()

        self.assertIsNone(self._response(redis_client),
                          "answered before QMT assigned the id")
        self.assertEqual(service.pending_settlement_count(), 1)

    def test_the_next_tick_answers_with_the_real_id(self):
        gateway = IdArrivesLateGateway(sysid_after=2)
        redis_client, service = self._service(gateway)
        self._submit(service)

        service.drain_pending()
        service.drain_pending()

        response = self._response(redis_client)
        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["data"]["order_sys_id"], "635093411")

    def test_it_never_publishes_a_none_id_for_a_live_order(self):
        """The reproduction in one assertion: the client maps None to -1."""
        gateway = IdArrivesLateGateway(sysid_after=3)
        redis_client, service = self._service(gateway)
        self._submit(service)

        for _ in range(3):
            service.drain_pending()

        response = self._response(redis_client)
        self.assertIsNotNone(response)
        self.assertTrue(response["data"]["order_sys_id"])

    def test_an_id_present_on_the_first_look_still_settles_at_once(self):
        """No extra round trip for the normal case."""
        gateway = IdArrivesLateGateway(sysid_after=1)
        redis_client, service = self._service(gateway)
        self._submit(service)

        service.drain_pending()

        self.assertEqual(self._response(redis_client)["data"]["order_sys_id"],
                         "635093411")
        self.assertEqual(gateway.lookups, 1)


class DeadlineTest(_Service):
    """A persistent empty id must not read as a rejection."""

    def _timed_out(self):
        gateway = IdArrivesLateGateway(sysid_after=999)
        redis_client, service = self._service(gateway, timeout=0.0)
        self._submit(service)
        service.drain_pending()
        return self._response(redis_client)

    def test_it_answers_rather_than_parking_forever(self):
        self.assertIsNotNone(self._timed_out())

    def test_it_reports_the_order_as_live_not_rejected(self):
        response = self._timed_out()

        error = response.get("server_error") or ""
        self.assertIn("ORDER IS LIVE", error)
        self.assertIn("DO NOT RESUBMIT", error)

    def test_it_names_the_remark_so_the_order_can_be_found(self):
        response = self._timed_out()

        self.assertIn("ord-1", response.get("server_error") or "")

    def test_it_does_not_reuse_the_not_in_system_wording(self):
        """That message means the opposite -- the order never reached the
        broker -- and leads with QMT's 模拟 run mode (issue #122). Saying it
        here would send the reporter looking in exactly the wrong place."""
        error = self._timed_out().get("server_error") or ""

        self.assertNotIn("not found in system", error)
        self.assertNotIn("运行模式", error)

    def test_the_client_turns_that_into_an_exception_not_a_minus_one(self):
        """xtquant_compat raises on server_error, which is the whole reason
        the deadline path sets one. RpcServerRepliedError is a RuntimeError
        subclass, so callers catching RuntimeError see no change; the distinct
        type is what lets the batch path tell a refused batch (nothing ran)
        from a timed-out one (may be running)."""
        import inspect

        from bigqmt_signal_trader import xtquant_compat

        source = inspect.getsource(xtquant_compat)
        self.assertIn('raise RpcServerRepliedError(', source)
        self.assertIn('"Big QMT %s server_error: %s"', source)
        self.assertTrue(issubclass(xtquant_compat.RpcServerRepliedError, RuntimeError))


class MissingOrderIsStillMissingTest(_Service):
    """The neighbouring branch must keep its own meaning (#41, #122)."""

    def test_no_remark_match_at_the_deadline_still_says_not_in_system(self):
        class NeverLands(DryRunOrderGateway):
            def query_orders(self, account_id, strategy_name):
                return []

        redis_client, service = self._service(NeverLands(), timeout=0.0)
        self._submit(service)
        service.drain_pending()

        error = self._response(redis_client).get("server_error") or ""
        self.assertIn("not found in system", error)
        self.assertIn("运行模式", error)


if __name__ == "__main__":
    unittest.main()
