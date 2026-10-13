"""Real Redis integration for exact QMT remark recovery (issue #413).

Only an isolated Unix socket is used; no terminal, credentials or trades.
"""
import datetime
import shutil
import subprocess
import tempfile
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

redis = pytest.importorskip("redis")
try:
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    NO_RETRY = {"retry": Retry(NoBackoff(), 0)}
except ImportError:
    NO_RETRY = {}

from bigqmt_signal_trader import exec_events
from bigqmt_signal_trader.order_remarks import bounded_identity_client, recover_order_remarks
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers


STAMP = datetime.datetime(2026, 10, 14, 2, 0, tzinfo=datetime.timezone.utc).timestamp()
TTL = exec_events.ORDER_IDENTITY_TTL_SECONDS


@pytest.fixture(scope="module")
def redis_socket(tmp_path_factory):
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server required for atomic Lua integration")
    temporary = tempfile.TemporaryDirectory(prefix="bqr-", dir="/tmp")
    root = Path(temporary.name)
    socket = str(root / "redis.sock")
    proc = subprocess.Popen([binary, "--port", "0", "--unixsocket", socket,
                             "--save", "", "--appendonly", "no"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = redis.Redis(unix_socket_path=socket, socket_timeout=0.25, **NO_RETRY)
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except redis.ConnectionError:
                time.sleep(0.02)
        else:
            pytest.fail("isolated Redis did not start")
        yield socket
    finally:
        client.close()
        proc.terminate()
        proc.wait(timeout=3)
        temporary.cleanup()


@pytest.fixture
def client(redis_socket):
    result = redis.Redis(unix_socket_path=redis_socket, socket_timeout=0.25, **NO_RETRY)
    result.flushdb()
    yield result
    result.close()


def row(remark="intent-1", **fields):
    values = dict(order_sys_id="000123", stock_code="600000.SH", action="SELL",
                  user_order_id=remark, remark=remark, order_time=STAMP,
                  status="56", volume=100, traded_volume=100, price=12.34)
    values.update(fields)
    return SimpleNamespace(**values)


def recover(client, rows, account="account-a", account_type="STOCK"):
    return recover_order_remarks(client, account, account_type, rows, TTL)


def handler(client, orders, trades):
    instance = BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)
    instance.account_id = "account-a"
    instance.order_gateway = SimpleNamespace(
        account_type="STOCK", query_orders=lambda *_: orders, query_trades=lambda *_: trades)
    instance.order_remark_redis_client = client
    instance.default_strategy_name = ""
    return instance


def test_recreated_handler_recovers_exact_association_without_changing_facts(client, redis_socket):
    original = row()
    handler(client, [original], [])._handle_query_execution_snapshot({})
    # A fresh client/handler has no Python state from the original observation.
    fresh = redis.Redis(unix_socket_path=redis_socket, socket_timeout=0.25, **NO_RETRY)
    missing = row("", order_time=STAMP + 1)
    result = handler(fresh, [missing], [])._handle_query_execution_snapshot({})["orders"][0]
    assert result.user_order_id == result.remark == "intent-1"
    assert {k: v for k, v in vars(result).items() if k not in ("remark", "user_order_id")} == {
        k: v for k, v in vars(missing).items() if k not in ("remark", "user_order_id")}
    assert missing.user_order_id == ""  # no recycled native evidence
    assert 0 < client.ttl(client.keys("bigqmt:order_remark:*")[0]) <= TTL
    fresh.close()


def test_whole_batch_observed_before_blank_order_and_deal_recovery(client):
    trade = row(order_time=0, traded_time=STAMP)
    result = handler(client, [row("")], [trade])._handle_query_execution_snapshot({})
    assert result["orders"][0].user_order_id == "intent-1"
    result = handler(client, [row()], [row("", order_time=0, traded_time=STAMP)])._handle_query_execution_snapshot({})
    assert result["trades"][0].user_order_id == "intent-1"


def test_later_conflicting_deal_prevents_earlier_blank_order_recovery(client):
    recover(client, [row()])
    result = handler(client, [row("")], [row("another", order_time=0, traded_time=STAMP)])._handle_query_execution_snapshot({})
    assert result["orders"][0].user_order_id == ""
    assert result["trades"][0].user_order_id == "another"
    assert recover(client, [row(), row("")])[1].user_order_id == ""


@pytest.mark.parametrize("kwargs,account,kind", [
    ({}, "account-b", "STOCK"), ({}, "account-a", "CREDIT"),
    ({"order_time": STAMP + 86400}, "account-a", "STOCK"),
    ({"stock_code": "600000.SZ"}, "account-a", "STOCK"),
    ({"order_sys_id": "123"}, "account-a", "STOCK"),
])
def test_account_type_day_exchange_and_leading_zero_isolation(client, kwargs, account, kind):
    recover(client, [row()])
    assert recover(client, [row("", **kwargs)], account, kind)[0].user_order_id == ""


@pytest.mark.parametrize("fields", [
    {"stock_code": "600001.SH"}, {"action": "BUY"},
])
def test_dimensions_conflict_poison_even_a_native_blank(client, fields):
    recover(client, [row()])
    assert recover(client, [row("", **fields)])[0].user_order_id == ""
    assert recover(client, [row("")])[0].user_order_id == ""


@pytest.mark.parametrize("fields", [
    {"order_time": 0}, {"order_time": None}, {"order_time": float("nan")},
    {"order_sys_id": ""}, {"order_sys_id": "-1"}, {"account_id": "other"},
    {"stock_code": "600000"}, {"action": ""},
])
def test_missing_source_dimensions_never_use_receipt_time(client, fields):
    recover(client, [row(created_at_ts=STAMP, **fields)])
    assert not client.keys("bigqmt:order_remark:*")
    assert recover(client, [row("")])[0].user_order_id == ""


def test_concurrent_conflicts_do_not_last_writer_win(client):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda value: recover(client, [row(value)]), ["first", "second"]))
    assert recover(client, [row("")])[0].user_order_id == ""


def test_repeated_observation_does_not_extend_ttl(client):
    recover(client, [row()])
    key = client.keys("bigqmt:order_remark:*")[0]
    client.expire(key, 3)
    recover(client, [row()])
    assert client.ttl(key) <= 3
    client.delete(key)
    assert recover(client, [row("")])[0].user_order_id == ""


def test_failure_is_native_only_and_never_partially_recovers(client):
    recover(client, [row()])
    class Broken:
        def eval(self, *args):
            raise TimeoutError("fixture")
    rows = [row(""), row("native", order_sys_id="another")]
    assert recover(Broken(), rows) == rows
    assert rows[0].user_order_id == ""
    assert recover(None, rows) == rows


def test_bounded_pool_preserves_endpoint_and_does_not_mutate_rpc_pool(client):
    original = dict(client.connection_pool.connection_kwargs)
    bounded = bounded_identity_client(client)
    assert bounded.ping()
    assert bounded.connection_pool.connection_kwargs["socket_timeout"] <= 0.25
    assert bounded.connection_pool.connection_kwargs.get("socket_connect_timeout", 0.25) <= 0.25
    assert client.connection_pool.connection_kwargs == original
    bounded.close()


@pytest.mark.parametrize("kind", ["order", "trade"])
def test_native_callback_records_when_push_is_disabled(client, monkeypatch, kind):
    import bigqmt_signal_trader_strategy as strategy
    native = SimpleNamespace(
        m_strAccountID="account-a", m_strOrderSysID="000123", m_strRemark="intent-1",
        m_strInstrumentID="600000", m_strExchangeID="SH", m_nOffsetFlag=49,
        m_strInsertDate="20261014", m_strInsertTime="100000",
        m_strTradeDate="20261014", m_strTradeTime="100000", m_strTradeID="deal-1")
    monkeypatch.setattr(strategy, "_rpc_service", SimpleNamespace(handlers=handler(client, [], [])))
    monkeypatch.setattr(strategy, "_build_config", lambda: {"exec_events": {"enabled": False}})
    monkeypatch.setattr(strategy, "forward_order_event", lambda _: None)
    monkeypatch.setattr(strategy, "forward_trade_event", lambda _: None)
    callback = strategy.order_callback if kind == "order" else strategy.deal_callback
    callback(None, native)
    assert recover(client, [row("")])[0].user_order_id == "intent-1"


def test_association_survives_python_process_exit(client, redis_socket):
    import os
    import sys
    script = """
import json, redis, sys
from bigqmt_signal_trader.order_remarks import recover_order_remarks
client = redis.Redis(unix_socket_path=sys.argv[1], socket_timeout=0.25)
row = json.loads(sys.argv[2])
recover_order_remarks(client, 'account-a', 'STOCK', [row], 604800)
"""
    subprocess.run([sys.executable, "-c", script, redis_socket, __import__("json").dumps(vars(row()))],
                   env=os.environ.copy(), check=True, timeout=5)
    assert recover(client, [row("")])[0].user_order_id == "intent-1"


def test_raw_passorder_association_uses_observed_system_id(client):
    from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway
    calls = []
    gateway = BigQmtOrderGateway(context_info=None, account_id="account-a", account_type="STOCK",
                                 passorder_func=lambda *args: calls.append(args))
    handlers = BigQmtRpcHandlers(account_id="account-a", market_data=None, position_provider=None,
                                 order_gateway=gateway, allow_order_methods=True)
    handlers.handle("passorder", dict(opType=24, orderType=1101, account_id="account-a",
                                     orderCode="600000.SH", prType=11, price=12.34, volume=100,
                                     strategyName="test", quickTrade=2, userOrderId="raw-intent"))
    assert len(calls) == 1 and calls[0][9] == "raw-intent"
    assert recover(client, [row("")])[0].user_order_id == ""  # submit alone cannot guess a sysid
    recover(client, [row(calls[0][9])])  # native callback/query assigned the real sysid
    assert handler(client, [row("")], [])._handle_query_orders({})[0].user_order_id == "raw-intent"
    assert handler(client, [], [row("", order_time=0, traded_time=STAMP)])._handle_query_trades({})[0].user_order_id == "raw-intent"


def test_conflicting_native_fields_and_corrupt_binding_never_restore(client):
    recover(client, [row()])
    assert recover(client, [row(user_order_id="other"), row("")])[1].user_order_id == ""
    client.flushdb()
    recover(client, [row()])
    key = client.keys("bigqmt:order_remark:*")[0]
    client.set(key, "invalid-json", ex=TTL)
    assert recover(client, [row("")])[0].user_order_id == ""


def test_bounded_tcp_timeout_does_not_retry_or_block_callback():
    import socket
    import threading
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stopped = threading.Event()
    def stall():
        conn, _ = listener.accept()
        try:
            stopped.wait(2)
        finally:
            conn.close()
    thread = threading.Thread(target=stall, daemon=True)
    thread.start()
    original = redis.Redis(host="127.0.0.1", port=listener.getsockname()[1], socket_timeout=30,
                           socket_connect_timeout=30, **NO_RETRY)
    bounded = bounded_identity_client(original)
    try:
        start = time.monotonic()
        assert recover(bounded, [row("")])[0].user_order_id == ""
        assert time.monotonic() - start < 0.8
    finally:
        stopped.set()
        thread.join(2)
        listener.close()
        bounded.close()
        original.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_initial_batch_dimension_conflict_is_order_independent(client, reverse):
    rows = [row(""), row("other-stock", stock_code="600001.SH")]
    recover(client, rows[::-1] if reverse else rows)
    assert recover(client, [row("", stock_code="600001.SH")])[0].user_order_id == ""


def test_raw_remark_whitespace_is_preserved_and_counts_as_conflict(client):
    recover(client, [row(" intent ")])
    assert recover(client, [row("")])[0].user_order_id == " intent "
    recover(client, [row("intent")])
    assert recover(client, [row("")])[0].user_order_id == ""


@pytest.mark.parametrize("custom_loader", [False, True])
def test_rpc_init_recovers_after_optional_early_event_import_failed(client, monkeypatch, custom_loader):
    import importlib
    import bigqmt_signal_trader_strategy as strategy
    monkeypatch.setattr(strategy, "_exec_events", None)
    monkeypatch.setattr(strategy, "_load_bridge_module", importlib.import_module if custom_loader else None)
    monkeypatch.setattr(strategy, "_build_quote_subscription_service", lambda *args: None)
    app = SimpleNamespace(order_gateway=SimpleNamespace(account_type="STOCK"), position_sync_sink=None)
    config = {"enable_rpc": True, "account_id": "account-a", "exec_events": {"enabled": False},
              "rpc": {"enabled": True, "redis_client": client, "response_redis_client": client}}
    service = strategy._build_rpc_service(None, app, config)
    monkeypatch.setattr(strategy, "_rpc_service", service)
    assert service.handlers.order_remark_redis_client.ping()
    event = SimpleNamespace(m_strAccountID="account-a", m_strOrderSysID="000123", m_strRemark="intent-1",
                            m_strInstrumentID="600000", m_strExchangeID="SH", m_nOffsetFlag=49,
                            m_strInsertDate="20261014", m_strInsertTime="100000")
    assert strategy._recover_callback_remark("order", event)["user_order_id"] == "intent-1"
    assert recover(client, [row("")])[0].user_order_id == "intent-1"
    service.handlers.order_remark_redis_client.close()
