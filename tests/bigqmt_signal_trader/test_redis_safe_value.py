# coding: utf-8
"""The shared codec bounds QMT's response-wide stock regex backtracking."""

import json
import re
import time
from types import SimpleNamespace

import pytest

import test_order_identity_safe_storage as vendor
from bigqmt_signal_trader.redis_safe_value import PREFIX, LUA_ENCODE, LUA_RESEGMENT, decode_json, encode_json
from test_order_identity_redis_integration import real_redis, _isolated_redis


@pytest.mark.parametrize("as_bytes", [False, True])
@pytest.mark.parametrize("value", [None, "", "x" * 14, "x" * 15, "x" * 16,
                                   ["600036.SH", "SELL", "中文_123456" * 500]])
def test_large_encoded_response_bounds_runs_and_never_checks_operations(real_redis, monkeypatch, value, as_bytes):
    stored = encode_json(value)
    assert not re.search(r"\d", stored)
    raw = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    token = real_redis.eval(LUA_ENCODE + LUA_RESEGMENT +
                            "return safe_resegment(safe_encode(ARGV[1], ARGV[2]), ARGV[2])",
                            0, raw, PREFIX).decode("ascii")
    assert len(re.findall(r"(?<=\D)0(?=\D)", token)) == token.count("0") > 0
    rows = [token.encode("ascii") if as_bytes else token] * 200
    vendor._assert_bounded_response(rows)

    def unexpected_operations(text):
        pytest.fail("stock regex must not match; operation regex must not run")

    monkeypatch.setattr(vendor, "_OP_RE", SimpleNamespace(findall=unexpected_operations))
    start = time.monotonic()
    assert vendor._check_response(rows) == rows
    assert vendor._check_response(rows[0]) == rows[0]
    assert time.monotonic() - start < 5  # allow slow CI; run length is the hard bound
    assert decode_json(rows[0]) == value


@pytest.mark.parametrize("kind", [str, lambda text: text.encode("ascii"),
                                 lambda text: bytearray(text.encode("ascii"))])
def test_old_unseparated_and_plain_json_values_still_decode(kind):
    value = {"strategy_name": "中文_T4_D1", "stock_code": "600036.SH",
             "remark": "000123", "created_at_ts": 1792052880.123456}
    raw = json.dumps(value, ensure_ascii=True)
    legacy = PREFIX + raw.encode("utf-8").hex().translate(str.maketrans("0123456789", "ghijklmnop"))
    assert decode_json(kind(legacy)) == value
    assert decode_json(kind(raw)) == value
    assert decode_json(kind(encode_json(value))) == value
