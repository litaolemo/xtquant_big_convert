"""迟到回包必须认连接对象；#431 的整数句柄复用不能成为回包路由。"""
import json
import threading
import time
from unittest import mock

import pytest

from bigqmt_signal_trader.transports.base import TransportError
from bigqmt_signal_trader.transports.pipe_transport import NamedPipeTransport, _ServerPipeConnection
from test_heavy_read_worker import _service, _ThreadNamingMarketData


def attach(server, handle):
    # RED 阶段也走旧实现的真实 drain；不靠「新增符号不存在」证明缺陷。
    if hasattr(server, "_register_server_connection"):
        connection = server._register_server_connection(handle)
    else:
        server._server_handles.append(handle)
        connection = handle
    server._drain_handles.append(connection)
    return connection


def close_connection(server, connection):
    if hasattr(server, "_close_server_connection"):
        server._close_server_connection(connection)
    else:
        server._forget_drain_handle(connection)


def fake_wire(server, writes):
    incoming = []
    server._dll = lambda: mock.Mock()
    server._accept_loop = lambda: None
    server._peek_available = lambda handle: bool(incoming)
    server._read = lambda handle: json.dumps(incoming.pop(0)).encode()
    server._write = lambda handle, data: writes.append((handle, json.loads(data)))
    return incoming


class BlockingMarket(_ThreadNamingMarketData):
    def __init__(self):
        super().__init__()
        self.entered, self.release = (threading.Event() for _ in range(2))

    def get_financial_data(self, *args, **kwargs):
        self.entered.set()
        assert self.release.wait(5), "test did not release heavy read"
        return {"old": True}


def request(request_id, method="ping"):
    return dict(request_id=request_id, account_id="acct", method=method, params={})


@pytest.mark.parametrize("stop_service", [False, True])
def test_late_heavy_reply_cannot_reach_reused_handle(stop_service):
    writes = []
    old = NamedPipeTransport()
    incoming = fake_wire(old, writes)
    market = BlockingMarket()
    _, service = _service(market_data=market, transport=old)
    service.start()
    connection = attach(old, 42)
    incoming.append(request("old-request", "get_financial_data"))
    old.drain_request_queue()
    assert market.entered.wait(2)
    worker = service._heavy_thread
    new = old
    try:
        if stop_service:
            service.stop()  # heavy 未结束；不能等它返回才让模型停止。
            new = NamedPipeTransport()
            incoming = fake_wire(new, writes)
            new.start_receiving(lambda req: {"request_id": req["request_id"], "ok": True},
                                background_threads=False)
        else:
            close_connection(old, connection)
        attach(new, 42)  # Windows 把相同整数分配给新连接。
        incoming.append(request("new-request"))
        new.drain_request_queue()
        market.release.set()
        if stop_service:
            worker.join(2)
        else:
            deadline = time.monotonic() + 2
            while service._heavy_replies.empty() and time.monotonic() < deadline:
                time.sleep(0.005)
            assert not service._heavy_replies.empty()
        service.flush_heavy_replies()
        assert [reply["request_id"] for _, reply in writes] == ["new-request"], writes
        incoming.append(request("next-request"))
        new.drain_request_queue()
        assert [reply["request_id"] for _, reply in writes] == ["new-request", "next-request"]
    finally:
        market.release.set()
        service.stop()
        if new is not old: new.stop()
        worker.join(2)


def test_drain_delivers_exactly_one_response():
    server = NamedPipeTransport()
    writes = []
    incoming = fake_wire(server, writes)
    server.start_receiving(lambda req: {"request_id": req["request_id"], "ok": True},
                           background_threads=False)
    try:
        attach(server, 42)
        incoming.extend([request("one"), request("two")])
        server.drain_request_queue()
        assert [reply["request_id"] for _, reply in writes] == ["one", "two"]
    finally:
        server.stop()


@pytest.mark.parametrize("response", [{"request_id": "other", "ok": True}, {"ok": True}])
def test_native_client_rejects_wrong_request_id_without_resending(response):
    client = NamedPipeTransport()
    client._client_handle = mock.Mock(return_value=42)
    client._write = mock.Mock()
    client._read_with_watchdog = mock.Mock(return_value=json.dumps(response).encode())
    client._drop_client_handle = mock.Mock()
    with pytest.raises(TransportError):
        client.send_request(request("mine", "order_stock"), 1)
    client._write.assert_called_once()
    client._drop_client_handle.assert_called_once()


def test_close_between_identity_check_and_write_cannot_target_reused_handle():
    server, writes = NamedPipeTransport(), []
    fake_wire(server, writes)
    server.start_receiving(lambda req: None, background_threads=False)
    old = attach(server, 42)
    checked, resume = threading.Event(), threading.Event()
    original = old.call
    def delayed_call(*args):
        checked.set()
        assert resume.wait(2)
        return original(*args)
    old.call = delayed_call
    writer = threading.Thread(target=lambda: server.send_response(
        {"_pipe_connection": old}, {"request_id": "old"}))
    writer.start()
    try:
        assert checked.wait(1)
        close_connection(server, old)
        new = attach(server, 42)
        server.send_response({"_pipe_connection": new}, {"request_id": "new"})
        resume.set()
        writer.join(2)
        assert not writer.is_alive()
        assert writes == [(42, {"request_id": "new"})]
        close_connection(server, old)  # 旧 worker 的 finally 再跑一次。
        assert server._connection_active(new)
        assert not new.closing.is_set()
    finally:
        resume.set()
        writer.join(2)
        server.stop()


def test_closed_background_outbox_is_never_flushed_to_reused_handle():
    server, writes = NamedPipeTransport(), []
    fake_wire(server, writes)
    server.start_receiving(lambda req: None, background_threads=True)
    try:
        old = server._register_server_connection(42)
        server.send_response({"_pipe_connection": old}, {"request_id": "old"})
        assert old.outbox.qsize() == 1
        close_connection(server, old)
        new = server._register_server_connection(42)
        server._serve_connection(old)  # 旧 worker 退出，不能补发它队列里的回包。
        server.send_response({"_pipe_connection": old}, {"request_id": "later"})
        assert writes == [] and new.outbox.empty()
        assert server._connection_active(new)
    finally:
        server.stop()


def test_close_retries_cancel_if_first_cancel_precedes_kernel_io():
    connection = _ServerPipeConnection(42)
    checked, entered, finished = (threading.Event() for _ in range(3))
    dll = mock.Mock()
    def io_call(handle):
        checked.set()  # call 已检查 closing，但还没有进 ReadFile/ConnectNamedPipe。
        assert entered.wait(2)
        assert finished.wait(2)
    def cancel(*args):
        if not entered.is_set():
            entered.set()  # 第一次取消落空，随后才进入内核。
        else:
            finished.set()
    dll.CancelIoEx.side_effect = cancel
    worker = threading.Thread(target=lambda: connection.call(io_call))
    worker.start()
    assert checked.wait(1)
    try:
        connection.close(dll)
        worker.join(2)
        assert not worker.is_alive()
        assert dll.CancelIoEx.call_count >= 2
        dll.CloseHandle.assert_called_once_with(42)
        connection.cancel(dll)  # 关闭后不能按旧整数取消可能已复用的句柄。
        connection.close(dll)
        dll.CloseHandle.assert_called_once_with(42)
    finally:
        entered.set()
        finished.set()
        worker.join(2)
