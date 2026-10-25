"""三个服务端 CreateNamedPipeW 调用都必须限制在同机，能力探测也不例外。"""
import ctypes
from types import SimpleNamespace
from unittest import mock

from bigqmt_signal_trader.transports.pipe_transport import (
    NamedPipeTransport, INVALID_HANDLE_VALUE, PIPE_REJECT_REMOTE_CLIENTS,
)
from bigqmt_signal_trader.transports.pipe_push_channel import PipeQuotePushChannel
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers


def test_rpc_pipe_rejects_remote_clients():
    server = NamedPipeTransport(account_id="test")
    server._running = True
    def create(*args):
        assert args[2] & PIPE_REJECT_REMOTE_CLIENTS
        server._running = False
        return INVALID_HANDLE_VALUE
    dll = SimpleNamespace(CreateNamedPipeW=mock.Mock(side_effect=create))
    server._dll = lambda: dll
    server._accept_loop()
    dll.CreateNamedPipeW.assert_called_once()


def test_event_pipe_rejects_remote_clients():
    server = PipeQuotePushChannel(bind_address=r"\\.\pipe\test")
    server._running = True
    def create(*args):
        assert args[2] & PIPE_REJECT_REMOTE_CLIENTS
        server._running = False
        return INVALID_HANDLE_VALUE
    dll = SimpleNamespace(CreateNamedPipeW=mock.Mock(side_effect=create))
    server._dll = lambda: (dll, None)
    server._accept_loop()
    dll.CreateNamedPipeW.assert_called_once()


def test_capability_probe_rejects_remote_clients():
    dll = mock.Mock()
    dll.CreateNamedPipeW.return_value = 42
    with mock.patch.object(ctypes, "WinDLL", return_value=dll, create=True):
        report = BigQmtRpcHandlers._probe_ctypes_named_pipe(None)
    assert report["create_pipe"], report
    assert dll.CreateNamedPipeW.call_args.args[2] & PIPE_REJECT_REMOTE_CLIENTS
    dll.CloseHandle.assert_called_once_with(42)
