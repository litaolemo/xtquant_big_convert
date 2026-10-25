"""#310/#351：0 等待保留订阅与分组；路由诊断必须读回实际生效值。"""
import json
import threading
from unittest import mock

import pytest

from bigqmt_signal_trader.adapters import market_bigqmt
from test_full_tick_subscription_warmup import SubscribedOnlyContext
from test_heavy_read_worker import _service, _request, _wait


def test_zero_wait_subscribes_without_sleep_or_reread():
    context = SubscribedOnlyContext()
    provider = market_bigqmt.BigQmtMarketDataProvider(context, native_xtdata_enabled=False)
    provider.full_tick_warmup_wait_seconds = 0
    with mock.patch.object(market_bigqmt.time, "sleep") as sleep:
        assert provider.get_ticks(["600052.SH"]) == {}
        sleep.assert_not_called()
    assert context.subscribe_calls == [["600052.SH"]]
    assert len(context.asked) == 1
    assert provider.tick_subscription_status()[0]["codes"] == ["600052.SH"]
    assert provider.get_ticks(["600052.SH"])["600052.SH"]["lastPrice"] == 3.69
    assert len(context.subscribe_calls) == 1


def test_default_still_waits_up_to_two_seconds():
    context = SubscribedOnlyContext()
    context.get_full_tick = mock.Mock(return_value={})
    provider = market_bigqmt.BigQmtMarketDataProvider(context, native_xtdata_enabled=False)
    assert getattr(provider, "full_tick_warmup_wait_seconds", 2.0) == 2.0
    now, waits = [0.0], []
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    with mock.patch.object(market_bigqmt.time, "monotonic", side_effect=lambda: now[0]), \
         mock.patch.object(market_bigqmt.time, "sleep", side_effect=sleep):
        assert provider.get_ticks(["600052.SH"]) == {}
    assert 2.0 <= sum(waits) <= 2.1
    assert context.subscribe_calls == [["600052.SH"]]
    assert context.get_full_tick.call_count > 1


@pytest.mark.parametrize("count,heavy", [(64, False), (65, True)])
def test_probe_reports_effective_threshold_and_strict_boundary(count, heavy):
    provider = market_bigqmt.BigQmtMarketDataProvider(SubscribedOnlyContext(), native_xtdata_enabled=False)
    provider.full_tick_warmup_wait_seconds = 0
    _, service = _service(market_data=provider, heavy_codes_threshold=64)
    report = service.handlers._probe_thread_routing()
    assert report["heavy_codes_threshold"] == 64
    assert report["full_tick_warmup_wait_seconds"] == 0
    codes = ["%06d.SZ" % i for i in range(count)]
    assert service.is_heavy_read("get_full_tick", {"codes": codes}) is heavy


@pytest.mark.parametrize("count,heavy", [(64, False), (65, True)])
def test_configured_boundary_routes_actual_requests(count, heavy):
    redis, service = _service(heavy_codes_threshold=64)
    service.start()
    try:
        codes = ["%06d.SZ" % i for i in range(count)]
        service.enqueue_payload(_request("get_full_tick", {"codes": codes}, request_id="boundary"))
        if heavy:
            assert _wait(lambda: service._heavy_replies.qsize() == 1)
            assert "resp:boundary" not in redis.kv
            assert service.handlers.market_data.threads["get_ticks"] == "bigqmt-rpc-heavy"
            service.drain_pending()
        else:
            assert service.handlers.market_data.threads["get_ticks"] == threading.current_thread().name
        response = json.loads(redis.kv["resp:boundary"])
        assert response["ok"] and response["request_id"] == "boundary"
    finally:
        service.stop()


@pytest.mark.parametrize("wait", [-1, float("nan"), float("inf")])
def test_invalid_warmup_wait_is_rejected(wait):
    with pytest.raises(ValueError):
        market_bigqmt.BigQmtMarketDataProvider(None, full_tick_warmup_wait_seconds=wait)
