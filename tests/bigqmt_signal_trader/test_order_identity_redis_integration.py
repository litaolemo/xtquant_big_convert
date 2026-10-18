# coding: utf-8
"""Exercise identity migration with Redis's real Lua and expiry semantics."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.exec_events import (
    enrich_order_identity,
    order_identity_key,
    order_identity_map,
    remember_order_identity,
)
from bigqmt_signal_trader.redis_safe_value import LUA_ENCODE, LUA_RESEGMENT, decode_json, encode_json
from test_order_identity_safe_storage import _assert_bounded_response


ACCOUNT = "integration-account"
REMARK = "GW01741471201"
NAME = "中文策略_T4_D1"
PREFIX = b"bqidentity:"
_ALPHA_TO_DIGIT = str.maketrans("ghijklmnop", "0123456789")

# Guojin QMT's redis/client.py response-wide filters (lines 926-927). Only
# GET/MGET and real Lua EVAL replies are filtered before Python consumes them.
_STOCK_RE = re.compile(
    r"(^|[^\d])+([36]0[\d]{4}|00(000[1-9]|[1-9][\d]{3}|[\d][1-9][\d]{2}|[\d]{2}[1-9][\d]))([^\d]|$)+"
)
_OP_RE = re.compile(r"(^|[^\d])(7[0-5]|6[0-1]|[1345]\d|2[3-9]|\d)([^\d]|$)")


class _JsonBytesEncoder(json.JSONEncoder):
    def default(self, value):
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return super().default(value)


class _FilteredRedis(object):
    def __init__(self, client, before_eval=None):
        self.client = client
        self.before_eval = before_eval
        self.evals = []

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _filtered(self, value):
        text = (json.dumps(value, cls=_JsonBytesEncoder)
                if isinstance(value, (list, dict)) else str(value))
        if _STOCK_RE.findall(text) and _OP_RE.findall(text):
            from redis.exceptions import DataError
            raise DataError("Sensitive Data Detected, Forbidden!")
        return value

    def get(self, key):
        return self._filtered(self.client.get(key))

    def mget(self, keys):
        return self._filtered(self.client.mget(keys))

    def eval(self, script, numkeys, *args):
        self.evals.append((script, numkeys, args))
        if self.before_eval is not None:
            self.before_eval(self.client, args[0])
        return self._filtered(self.client.eval(script, numkeys, *args))


@pytest.fixture(scope="module")
def _isolated_redis(tmp_path_factory):
    redis = pytest.importorskip("redis")
    try:
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except ImportError:
        no_retry = {"retry_on_timeout": False}
    else:
        no_retry = {"retry": Retry(NoBackoff(), 0)}

    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is unavailable for isolated Lua integration tests")
    # Reserve a loopback port, then release it immediately before starting our
    # own server. Never inspect or reuse an existing Redis/account database.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    directory = tmp_path_factory.mktemp("identity-redis")
    logfile = directory / "redis.log"
    client = redis.Redis(host="127.0.0.1", port=port, db=0,
                         socket_connect_timeout=0.2, socket_timeout=0.5,
                         **no_retry)
    options = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    with logfile.open("wb") as output:
        process = subprocess.Popen(
            [executable, "--bind", "127.0.0.1", "--port", str(port),
             "--save", "", "--appendonly", "no", "--dir", str(directory)],
            stdout=output, stderr=subprocess.STDOUT, **options
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("isolated Redis exited at startup: " +
                                logfile.read_text(encoding="utf-8", errors="replace"))
                try:
                    if client.ping():
                        break
                except redis.exceptions.RedisError:
                    pass
                time.sleep(0.02)
            else:
                pytest.fail("isolated Redis did not become ready: " +
                            logfile.read_text(encoding="utf-8", errors="replace"))
            yield client
        finally:
            if process.poll() is None:
                try:
                    client.shutdown(nosave=True)
                except redis.exceptions.RedisError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            client.connection_pool.disconnect()


@pytest.fixture
def real_redis(_isolated_redis):
    _isolated_redis.flushdb()
    return _isolated_redis


def _legacy(account=ACCOUNT, remark=REMARK):
    # Preserve original UTF-8, layout, and unknown fields, rather than checking
    # only a JSON round trip that could hide Lua cjson's lossy reserialization.
    payload = {
        "account_id": account,
        "user_order_id": remark,
        "strategy_name": NAME,
        "stock_code": "600036.SH",
        "created_at_ts": 1792052880.123456,
        "extra": {"原始字段": ["cu2610.SF", "literal\\escape", 0.123456789012345]},
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return payload, raw


def _decode_storage(raw):
    assert raw.startswith(PREFIX)
    encoded = raw[len(PREFIX):].decode("ascii")
    assert not any(character.isdigit() for character in encoded)
    return bytes.fromhex(encoded.translate(_ALPHA_TO_DIGIT))


def _expiry_deadline(client, key):
    # Compare expiry deadlines, not durations: migration must not renew TTL.
    remaining = client.pttl(key)
    assert remaining > 0
    return time.monotonic() * 1000 + remaining


@pytest.mark.parametrize("reader", ["query", "callback", "remember"])
def test_real_lua_migrates_filtered_legacy_without_changing_bytes_or_ttl(real_redis, reader):
    from redis.exceptions import DataError

    payload, raw = _legacy()
    key = order_identity_key(ACCOUNT, REMARK)
    real_redis.set(key, raw, px=120000)
    filtered = _FilteredRedis(real_redis)
    with pytest.raises(DataError, match="Sensitive Data Detected"):
        filtered.get(key)
    before = _expiry_deadline(real_redis, key)

    if reader == "query":
        assert order_identity_map(filtered, ACCOUNT, [REMARK]) == {REMARK: payload}
    elif reader == "callback":
        event = {"remark": REMARK, "strategy_name": "大QMT服务", "stock_code": ""}
        enriched = enrich_order_identity(filtered, ACCOUNT, event)
        assert enriched["strategy_name"] == NAME
        assert enriched["stock_code"] == "600036.SH"
        assert enriched["remark"] == REMARK
    else:
        assert remember_order_identity(filtered, ACCOUNT, REMARK,
                                       "another strategy", "000001.SZ") is None

    assert _decode_storage(real_redis.get(key)) == raw
    assert abs(_expiry_deadline(real_redis, key) - before) < 50
    assert sum("SETRANGE" in script for script, _, _ in filtered.evals) == 1
    # A fresh client with no submit-time memory reads the migrated record.
    assert order_identity_map(_FilteredRedis(real_redis), ACCOUNT, [REMARK]) == {REMARK: payload}


def test_real_lua_keeps_a_persistent_legacy_record_persistent(real_redis):
    payload, raw = _legacy()
    key = order_identity_key(ACCOUNT, REMARK)
    real_redis.set(key, raw)

    assert order_identity_map(_FilteredRedis(real_redis), ACCOUNT, [REMARK]) == {REMARK: payload}

    assert real_redis.pttl(key) == -1
    assert _decode_storage(real_redis.get(key)) == raw


@pytest.mark.parametrize("field", ["account", "remark"])
def test_real_lua_does_not_rewrite_a_mismatched_identity(real_redis, field):
    values = {"account": ACCOUNT, "remark": REMARK}
    values[field] = "different"
    _, raw = _legacy(**values)
    key = order_identity_key(ACCOUNT, REMARK)
    real_redis.set(key, raw, px=120000)
    before = _expiry_deadline(real_redis, key)
    filtered = _FilteredRedis(real_redis)

    assert order_identity_map(filtered, ACCOUNT, [REMARK]) == {}

    assert real_redis.get(key) == raw
    assert abs(_expiry_deadline(real_redis, key) - before) < 50
    assert sum("SETRANGE" in script for script, _, _ in filtered.evals) == 1


def test_real_lua_does_not_resurrect_a_record_that_expires_before_eval(real_redis):
    _, raw = _legacy()
    key = order_identity_key(ACCOUNT, REMARK)
    real_redis.set(key, raw, px=120000)

    def expire_before_eval(client, identity_key):
        client.pexpire(identity_key, 1)
        deadline = time.monotonic() + 1
        while client.exists(identity_key) and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not client.exists(identity_key)

    filtered = _FilteredRedis(real_redis, before_eval=expire_before_eval)

    assert order_identity_map(filtered, ACCOUNT, [REMARK]) == {}

    assert len(filtered.evals) == 1
    assert real_redis.pttl(key) == -2
    assert real_redis.get(key) is None


def test_real_redis_new_record_survives_a_fresh_filtered_client(real_redis):
    key = order_identity_key(ACCOUNT, REMARK)
    payload = remember_order_identity(_FilteredRedis(real_redis), ACCOUNT,
                                      REMARK, NAME, "cu2610.SF")

    assert payload is not None
    assert order_identity_map(_FilteredRedis(real_redis), ACCOUNT, [REMARK]) == {REMARK: payload}
    stored = json.loads(_decode_storage(real_redis.get(key)).decode("utf-8"))
    assert stored == payload
    assert stored["strategy_name"] == NAME
    assert stored["stock_code"] == "cu2610.SF"


@pytest.mark.parametrize("size", [0, 1, 13, 14, 15, 16, 17, 30, 31, 32, 33, 10000])
def test_real_lua_codec_matches_python_at_chunk_boundaries(real_redis, size):
    value = "x" * size
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    encoded = real_redis.eval(LUA_ENCODE + "return safe_encode(ARGV[1], ARGV[2])",
                              0, raw, PREFIX)
    assert not re.search(rb"\d", encoded)
    reply = real_redis.eval(LUA_RESEGMENT + "return safe_resegment(ARGV[1], ARGV[2])",
                            0, encoded, PREFIX)
    _assert_bounded_response([reply] * 200)
    assert decode_json(reply) == value
    assert encoded == encode_json(value).encode("ascii")
    assert decode_json(encoded) == value
    assert len(encoded) > len(raw)  # SETRANGE must overwrite every old byte


def test_real_lua_batch_migration_preserves_long_original_bytes_and_expiry(real_redis):
    before = {}
    for i in range(200):
        remark = "synthetic-" + str(i)
        _, raw = _legacy(remark=remark)
        if i == 199:
            raw = raw[:-1] + b', "padding": "' + b"x" * 10000 + b'"}'
        key = order_identity_key(ACCOUNT, remark)
        real_redis.set(key, raw, px=120000)
        before[key] = (raw, _expiry_deadline(real_redis, key))
    # Exercise the actual migration script without first returning legacy
    # values through the guard. Existing per-key fallback semantics stay intact.
    from bigqmt_signal_trader.exec_events import _MIGRATE_ORDER_IDENTITY
    replies = [real_redis.eval(_MIGRATE_ORDER_IDENTITY, 1, key, ACCOUNT,
                              json.loads(raw.decode("utf-8"))["user_order_id"], PREFIX)
               for key, (raw, _) in before.items()]
    assert all(not re.search(rb"\d", reply) for reply in replies)
    for key, (raw, deadline) in before.items():
        stored = real_redis.get(key)
        assert len(stored) > len(raw)
        assert _decode_storage(stored) == raw
        assert abs(_expiry_deadline(real_redis, key) - deadline) < 50


def test_real_redis_reads_old_unseparated_identity_without_rewriting(real_redis):
    payload, raw = _legacy()
    key = order_identity_key(ACCOUNT, REMARK)
    old = PREFIX + raw.hex().translate(str.maketrans("0123456789", "ghijklmnop")).encode("ascii")
    real_redis.set(key, old, px=120000)
    deadline = _expiry_deadline(real_redis, key)
    reader = _FilteredRedis(real_redis)
    assert order_identity_map(reader, ACCOUNT, [REMARK]) == {REMARK: payload}
    assert real_redis.get(key) == old
    assert abs(_expiry_deadline(real_redis, key) - deadline) < 50
    assert len(reader.evals) == 1


def test_real_redis_large_new_identity_batch_needs_no_migration(real_redis):
    filtered = _FilteredRedis(real_redis)
    expected = {}
    for i in range(200):
        remark = "synthetic-" + str(i)
        expected[remark] = remember_order_identity(filtered, ACCOUNT, remark, NAME, "600036.SH")
    values = real_redis.mget([order_identity_key(ACCOUNT, remark) for remark in expected])
    assert all(not re.search(rb"\d", value) for value in values)
    assert all(value is not None for value in expected.values())
    assert order_identity_map(filtered, ACCOUNT, list(expected)) == expected
    assert len(filtered.evals) == 1
