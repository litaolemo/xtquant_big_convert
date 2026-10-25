"""停止落在 CreateNamedPipeW 与登记之间：旧 accept 必须退出再交还名字。"""
import threading
from unittest import mock

import pytest

from bigqmt_signal_trader.transports.pipe_transport import NamedPipeTransport
from bigqmt_signal_trader.transports.pipe_push_channel import PipeQuotePushChannel


@pytest.mark.parametrize("events", [False, True])
def test_stop_joins_accept_even_when_creation_is_not_registered(events):
    created, release, wake, stopped = (threading.Event() for _ in range(4))
    dll = mock.Mock()
    def create(*args):
        created.set()
        assert release.wait(4)
        return 42
    def connect(*args):
        assert wake.wait(4)
        return True
    dll.CreateNamedPipeW.side_effect = create
    dll.ConnectNamedPipe.side_effect = connect
    dll.CreateFileW.side_effect = lambda *args: (wake.set(), 43)[1]
    server = PipeQuotePushChannel(bind_address=r"\\.\pipe\test") if events else NamedPipeTransport()
    server._dll = (lambda: (dll, None)) if events else (lambda: dll)
    if events:
        server.start_publisher()
        listener = server._accept_thread
    else:
        server.start_receiving(lambda req: None, background_threads=False)
        listener = server._listener
    assert created.wait(2)
    stopper = threading.Thread(target=lambda: (server.stop(), stopped.set()))
    stopper.start()
    try:
        # 原事件管道只 join(1s)，超时后也不能假报停止。
        returned_before_creation_finished = stopped.wait(1.1)
        release.set()
        stopper.join(2)
        assert not returned_before_creation_finished
        assert stopped.is_set() and not listener.is_alive()
        dll.ConnectNamedPipe.assert_not_called()
        assert sum(call.args == (42,) for call in dll.CloseHandle.call_args_list) == 1
        assert not (server._sub_handles if events else server._server_handles)
    finally:
        release.set()
        wake.set()
        stopper.join(2)
        listener.join(2)


@pytest.mark.parametrize("events", [False, True])
def test_stop_wakes_blocked_accept_and_joins_before_return(events):
    connected, wake = threading.Event(), threading.Event()
    dll = mock.Mock()
    dll.CreateNamedPipeW.return_value = 42
    def connect(*args):
        connected.set()
        assert wake.wait(3)
        return True
    dll.ConnectNamedPipe.side_effect = connect
    dll.CreateFileW.side_effect = lambda *args: (wake.set(), 43)[1]
    server = PipeQuotePushChannel(bind_address=r"\\.\pipe\test") if events else NamedPipeTransport()
    server._dll = (lambda: (dll, None)) if events else (lambda: dll)
    if events:
        server.start_publisher()
        listener = server._accept_thread
    else:
        server.start_receiving(lambda req: None, background_threads=False)
        listener = server._listener
    assert connected.wait(2)
    try:
        server.stop()
        assert not listener.is_alive()
        assert dll.CreateFileW.called
        assert not (server._sub_handles if events else server._server_handles)
        assert sum(call.args == (42,) for call in dll.CloseHandle.call_args_list) == 1
    finally:
        wake.set()
        listener.join(2)
