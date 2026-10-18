# coding: utf-8
"""Batch replies must be bounded without changing stored bytes or TTL."""
import json
import re
import time

import pytest

from bigqmt_signal_trader.exec_events import (
    order_identity_key, order_identity_map,
)
from bigqmt_signal_trader.redis_safe_value import PREFIX
from test_order_identity_redis_integration import real_redis, _isolated_redis, _FilteredRedis
from test_order_identity_safe_storage import _JsonBytesEncoder, _STOCK_RE, _check_response


def assert_bounded_reply(reply):
    text = json.dumps(reply, cls=_JsonBytesEncoder)
    assert max(map(len, re.findall(r"\D+", text)), default=0) <= 47
    assert all(len(digits) == 1 for digits in re.findall(r"\d+", text))
    assert not _STOCK_RE.findall(text)
    start = time.monotonic()
    _check_response(reply)
    assert time.monotonic() - start < 5


class BoundedRedis(_FilteredRedis):
    def __init__(self, client):
        super().__init__(client)
        self.replies = []
        self.direct_reads = []

    def _filtered(self, value):
        self.replies.append(value)
        assert_bounded_reply(value)
        return super()._filtered(value)

    def get(self, key):
        self.direct_reads.append("GET")
        return super().get(key)

    def mget(self, keys):
        self.direct_reads.append("MGET")
        return super().mget(keys)


def old_identity(remark):
    value = dict(account_id="synthetic-account", user_order_id=remark,
                 strategy_name="T4_D1", stock_code="600036.SH")
    token = json.dumps(value).encode("utf-8").hex().translate(str.maketrans("0123456789", "ghijklmnop"))
    return value, (PREFIX + token).encode("ascii")


@pytest.mark.parametrize("count", [31, 63, 126])
@pytest.mark.parametrize("as_text", [False, True])
def test_old_identity_batch_is_bounded_before_guard_without_storage_changes(real_redis, count, as_text):
    import redis
    options = real_redis.connection_pool.connection_kwargs
    reader = redis.Redis(host=options["host"], port=options["port"],
                         decode_responses=as_text, socket_timeout=0.25)
    expected = {}
    stored = {}
    for i in range(count):
        remark = "synthetic-" + str(i)
        value, raw = old_identity(remark)
        key = order_identity_key("synthetic-account", remark)
        real_redis.set(key, raw, px=120000)
        expected[remark] = value
        stored[key] = (raw, time.monotonic() * 1000 + real_redis.pttl(key))
    filtered = BoundedRedis(reader)
    try:
        assert order_identity_map(filtered, "synthetic-account", list(expected)) == expected
        assert filtered.direct_reads == []
        assert len(filtered.evals) == 1
        for key, (raw, deadline) in stored.items():
            assert real_redis.get(key) == raw
            assert abs(time.monotonic() * 1000 + real_redis.pttl(key) - deadline) < 50
    finally:
        reader.close()


def test_large_missing_identity_batch_is_bounded(real_redis):
    filtered = BoundedRedis(real_redis)
    remarks = ["synthetic-" + str(i) for i in range(500)]
    assert order_identity_map(filtered, "synthetic-account", remarks) == {}
    assert filtered.replies == [[0] * 500]
    assert filtered.direct_reads == [] and len(filtered.evals) == 1
    assert real_redis.dbsize() == 0


def test_long_old_identity_and_malformed_prefixed_values_stay_bounded(real_redis):
    value, _ = old_identity("synthetic-remark")
    value["strategy_name"] = "中文_600036" * 10000
    raw = (PREFIX + json.dumps(value, ensure_ascii=False).encode("utf-8").hex()
           .translate(str.maketrans("0123456789", "ghijklmnop"))).encode("ascii")
    key = order_identity_key("synthetic-account", "synthetic-remark")
    real_redis.set(key, raw, px=120000)
    bad_key = order_identity_key("synthetic-account", "bad")
    invalid = PREFIX + "600036_1" * 1000
    real_redis.set(bad_key, invalid)
    filtered = BoundedRedis(real_redis)
    assert order_identity_map(filtered, "synthetic-account", ["synthetic-remark", "bad"]) == {
        "synthetic-remark": value}
    assert real_redis.get(key) == raw
    assert real_redis.get(bad_key) == invalid.encode("ascii")


@pytest.mark.parametrize("kind", ["missing", "conflict"])
@pytest.mark.parametrize("count", [200, 400, 800])
def test_remark_sentinels_are_bounded_and_keep_native_semantics(real_redis, kind, count, monkeypatch, caplog):
    from bigqmt_signal_trader import order_remarks
    rows = [dict(order_sys_id="synthetic-" + str(i), stock_code="600036.SH", action="SELL",
                 order_time=1792029600, remark="", user_order_id="") for i in range(count)]
    keys = [order_remarks._descriptor("synthetic-account", "STOCK", row)[0] for row in rows]
    if kind == "conflict":
        for key in keys:
            real_redis.set(key, "!conflict", px=120000)
    filtered = BoundedRedis(real_redis)
    monkeypatch.setattr(order_remarks, "_last_warning", -float("inf"))
    result = order_remarks.recover_order_remarks(filtered, "synthetic-account", "STOCK", rows, 604800)
    assert result == rows and all(row["remark"] == "" for row in rows)
    assert filtered.replies == [[0 if kind == "missing" else -1] * count]
    assert len(filtered.evals) == 1
    if kind == "conflict":
        assert "Conflicting native order remark association" in caplog.text
        assert real_redis.mget(keys) == [b"!conflict"] * count
        assert all(0 < real_redis.pttl(key) <= 120000 for key in keys)
    else:
        assert "Conflicting" not in caplog.text
        assert real_redis.mget(keys) == [None] * count
