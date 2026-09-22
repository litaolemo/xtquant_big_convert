# coding: utf-8
"""#343: a reply from the background listener thread must be ONE Redis round
trip, not eight.

Every Redis command on the listener thread releases the GIL for the socket
and has to win it back from QMT's main thread afterwards, and that costs
about one adjust tick each (#104). ``send_response`` used to run
SETEX + RPUSH + EXPIRE + PUBLISH on the response client *and again* on the
listen client -- eight round trips, eight GIL re-acquisitions. Measured on
the live terminal (0.3.50, redis, rpc_background_threads=True,
100nMilliSecond): ``ping breakdown ... publish=500-700ms`` for a handler
that took 0.0ms, and every inline read answered in 0.5-0.8s while the
deferred ones answered from the adjust thread in 7ms.

One pipeline on one client is one round trip; the second client is a
fallback, not a second copy.
"""
import json
import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from bigqmt_signal_trader.transports.redis_transport import RedisTransport  # noqa: E402


class _Pipeline(object):
    def __init__(self, owner):
        self.owner = owner
        self.commands = []

    def setex(self, key, ttl, value):
        self.commands.append(("setex", key, ttl, value))
        return self

    def set(self, key, value):
        self.commands.append(("set", key, value))
        return self

    def rpush(self, key, value):
        self.commands.append(("rpush", key, value))
        return self

    def expire(self, key, ttl):
        self.commands.append(("expire", key, ttl))
        return self

    def publish(self, channel, value):
        self.commands.append(("publish", channel, value))
        return self

    def execute(self):
        self.owner.round_trips += 1
        if self.owner.fail:
            raise ConnectionError("redis down")
        results = []
        for command in self.commands:
            results.append(self.owner._apply(command))
        return results


class _CountingRedis(object):
    """Counts network round trips: one per direct command, one per
    pipeline.execute()."""

    def __init__(self, fail=False):
        self.round_trips = 0
        self.fail = fail
        self.store = {}
        self.lists = {}
        self.published = []

    def _apply(self, command):
        name = command[0]
        if name == "setex":
            self.store[command[1]] = command[3]
            return True
        if name == "set":
            self.store[command[1]] = command[2]
            return True
        if name == "rpush":
            self.lists.setdefault(command[1], []).append(command[2])
            return len(self.lists[command[1]])
        if name == "expire":
            return True
        if name == "publish":
            self.published.append((command[1], command[2]))
            return 1
        raise AssertionError(command)

    def _direct(self, *command):
        self.round_trips += 1
        if self.fail:
            raise ConnectionError("redis down")
        return self._apply(command)

    def setex(self, key, ttl, value):
        return self._direct("setex", key, ttl, value)

    def set(self, key, value):
        return self._direct("set", key, value)

    def rpush(self, key, value):
        return self._direct("rpush", key, value)

    def expire(self, key, ttl):
        return self._direct("expire", key, ttl)

    def publish(self, channel, value):
        return self._direct("publish", channel, value)

    def pipeline(self, transaction=True):
        return _Pipeline(self)


class _LegacyRedis(_CountingRedis):
    """A client with no pipeline() at all (the older test fakes)."""

    pipeline = None


def _request():
    return {
        "request_id": "req-1",
        "account_id": "acc",
        "reply_key": "bigqmt:rpc:resp:acc:req-1",
        "reply_list": "bigqmt:rpc:respq:acc:req-1",
        "reply_channel": "bigqmt:rpc:resp:acc:req-1",
        "ttl_seconds": 60,
    }


def _response():
    return {"request_id": "req-1", "account_id": "acc", "ok": True, "data": {"pong": True}}


def test_reply_is_one_round_trip_on_one_client():
    response_client = _CountingRedis()
    listen_client = _CountingRedis()
    transport = RedisTransport(listen_client, account_id="acc",
                               response_redis_client=response_client)

    transport.send_response(_request(), _response())

    assert response_client.round_trips == 1, response_client.round_trips
    # The listen client is a fallback, not a second copy of every write.
    assert listen_client.round_trips == 0, listen_client.round_trips
    payload = response_client.store["bigqmt:rpc:resp:acc:req-1"]
    assert json.loads(payload)["data"] == {"pong": True}
    # Exactly one item for the client's BLPOP -- the double push left a
    # stray copy in the list until its TTL.
    assert response_client.lists["bigqmt:rpc:respq:acc:req-1"] == [payload]
    assert response_client.published == [("bigqmt:rpc:resp:acc:req-1", payload)]
    assert transport._published_count == 1


def test_reply_falls_back_to_listen_client_when_response_client_fails():
    response_client = _CountingRedis(fail=True)
    listen_client = _CountingRedis()
    transport = RedisTransport(listen_client, account_id="acc",
                               response_redis_client=response_client)

    transport.send_response(_request(), _response())

    assert response_client.round_trips == 1
    assert listen_client.round_trips == 1
    assert "bigqmt:rpc:resp:acc:req-1" in listen_client.store
    assert len(listen_client.lists["bigqmt:rpc:respq:acc:req-1"]) == 1


def test_reply_raises_when_every_client_fails():
    response_client = _CountingRedis(fail=True)
    listen_client = _CountingRedis(fail=True)
    transport = RedisTransport(listen_client, account_id="acc",
                               response_redis_client=response_client)

    with pytest.raises(ConnectionError):
        transport.send_response(_request(), _response())


def test_reply_without_pipeline_support_still_lands():
    """A client without pipeline() (older fakes, exotic wrappers) keeps the
    per-command path and the reply still arrives."""
    client = _LegacyRedis()
    transport = RedisTransport(client, account_id="acc")

    transport.send_response(_request(), _response())

    assert "bigqmt:rpc:resp:acc:req-1" in client.store
    assert len(client.lists["bigqmt:rpc:respq:acc:req-1"]) == 1
    assert len(client.published) == 1


def test_reply_only_writes_the_targets_the_request_named():
    client = _CountingRedis()
    transport = RedisTransport(client, account_id="acc")
    request = _request()
    request.pop("reply_list")

    transport.send_response(request, _response())

    assert client.round_trips == 1
    assert client.lists == {}
    assert "bigqmt:rpc:resp:acc:req-1" in client.store
    assert len(client.published) == 1
