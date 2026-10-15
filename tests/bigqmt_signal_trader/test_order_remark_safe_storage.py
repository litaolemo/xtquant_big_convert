# coding: utf-8
"""Real Lua replies pass the same response-wide filter as QMT identity reads."""

import json
from types import SimpleNamespace

import pytest

from test_order_identity_safe_storage import DataError, _check_response
from test_order_remark_recovery import (
    NO_RETRY, client, handler, recover, redis_socket, row,
)


REMARK = "ab12cd34ef56"
LEGACY = json.dumps(["600036.SH", "SELL", REMARK], separators=(",", ":"))


class FilteredRedis(object):
    def __init__(self, client):
        self.client = client
        self.replies = []
        self.rejected = 0

    def eval(self, *args):
        result = self.client.eval(*args)
        self.replies.append(result)
        try:
            return _check_response(result)
        except DataError:
            self.rejected += 1
            raise


def _row(remark=REMARK, **fields):
    return row(remark, stock_code="600036.SH", **fields)


def _key(client):
    return client.keys("bigqmt:order_remark:*")[0]


@pytest.mark.parametrize("as_bytes", [False, True])
def test_filter_rejects_original_eval_reply(as_bytes):
    value = LEGACY.encode("utf-8") if as_bytes else LEGACY
    with pytest.raises(DataError, match="Sensitive Data Detected"):
        _check_response([value])


@pytest.mark.parametrize("decode_responses", [False, True])
@pytest.mark.parametrize("query", ["orders", "trades", "execution_snapshot"])
def test_filtered_eval_recovers_after_fresh_handler(client, redis_socket, decode_responses, query):
    import redis

    writer = FilteredRedis(client)
    recover(writer, [_row()])
    fresh = redis.Redis(unix_socket_path=redis_socket, decode_responses=decode_responses,
                        socket_timeout=0.25, **NO_RETRY)
    reader = FilteredRedis(fresh)
    missing = _row("")
    try:
        handlers = handler(reader, [missing], [missing])
        result = getattr(handlers, "_handle_query_" + query)({})
        recovered = result["orders"][0] if query == "execution_snapshot" else result[0]
        assert recovered.user_order_id == recovered.remark == REMARK
        assert vars(missing) == vars(_row(""))
        assert writer.rejected == reader.rejected == 0
        assert all(not any(c.isdigit() for c in str(value))
                   for reply in writer.replies + reader.replies for value in reply)
    finally:
        fresh.close()


@pytest.mark.parametrize("ttl_ms", [120000, None])
@pytest.mark.parametrize("native", [False, True])
def test_legacy_json_recovers_without_any_storage_or_expiry_change(client, ttl_ms, native):
    recover(client, [_row()])
    key = _key(client)
    client.set(key, LEGACY, px=ttl_ms)
    before = client.pttl(key)
    filtered = FilteredRedis(client)
    rows = [_row("")]
    if native:
        rows.append(_row())

    assert recover(filtered, rows)[0].user_order_id == REMARK

    assert filtered.rejected == 0
    assert client.get(key) == LEGACY.encode("utf-8")
    after = client.pttl(key)
    if ttl_ms is None:
        assert after == -1
    else:
        assert 0 < after <= before and before - after < 1000


@pytest.mark.parametrize("kind", ["order", "trade"])
def test_filtered_native_callback_records_and_recovers_with_push_disabled(client, monkeypatch, kind):
    import bigqmt_signal_trader_strategy as strategy

    filtered = FilteredRedis(client)
    monkeypatch.setattr(strategy, "_rpc_service", SimpleNamespace(handlers=handler(filtered, [], [])))
    monkeypatch.setattr(strategy, "_build_config", lambda: {"exec_events": {"enabled": False}})
    monkeypatch.setattr(strategy, "forward_order_event", lambda _: None)
    monkeypatch.setattr(strategy, "forward_trade_event", lambda _: None)
    native = SimpleNamespace(
        m_strAccountID="account-a", m_strOrderSysID="000123", m_strRemark=REMARK,
        m_strInstrumentID="600036", m_strExchangeID="SH", m_nOffsetFlag=49,
        m_strInsertDate="20261014", m_strInsertTime="100000",
        m_strTradeDate="20261014", m_strTradeTime="100000", m_strTradeID="test-deal")
    callback = strategy.order_callback if kind == "order" else strategy.deal_callback
    callback(None, native)
    native.m_strRemark = ""
    recovered = strategy._recover_callback_remark(kind, native)
    assert recovered["user_order_id"] == recovered["remark"] == REMARK
    assert native.m_strRemark == ""
    assert filtered.rejected == 0


def test_filtered_batch_handles_conflict_missing_and_unicode_without_false_matches(client):
    filtered = FilteredRedis(client)
    text = "  中文备注_12\\\"\n  "
    recover(filtered, [_row(text), _row(order_sys_id="conflict")])
    batch = [_row(""), _row("", order_sys_id="conflict"),
             _row("other", order_sys_id="conflict"), _row("", order_sys_id="missing")]
    result = recover(filtered, batch)
    assert [item.user_order_id for item in result] == [text, "", "other", ""]
    assert filtered.rejected == 0
    assert recover(filtered, [_row("", order_sys_id="conflict")])[0].user_order_id == ""


def test_expired_legacy_binding_is_not_recreated_by_filtered_read(client):
    recover(client, [_row()])
    key = _key(client)
    client.pexpire(key, 0)
    assert recover(FilteredRedis(client), [_row("")])[0].user_order_id == ""
    assert client.pttl(key) == -2


@pytest.mark.parametrize("failure", ["eval", "response", "decode", "short"])
def test_failed_filtered_batch_keeps_every_native_row(client, failure):
    recover(client, [_row(), _row(order_sys_id="second")])

    class Broken(FilteredRedis):
        def eval(self, *args):
            if failure == "eval":
                raise RuntimeError("NOPERM EVAL is disabled")
            values = super().eval(*args)
            if failure == "response":
                raise DataError("Sensitive Data Detected, Forbidden!")
            if failure == "decode":
                values[1] = b"bqidentity:invalid"
            if failure == "short":
                values.pop()
            return values

    rows = [_row(""), _row("", order_sys_id="second"), _row("native", order_sys_id="third")]
    assert recover(Broken(client), rows) == rows
    assert [item.user_order_id for item in rows] == ["", "", "native"]
