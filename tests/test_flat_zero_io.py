"""真正 exec 构建产物：#202 注入绑定、#420/#433 禁文件/禁网络门禁。

子进程隔离 flat loader 对 sys.modules/importlib 的替换。禁调用即使被日志或
journal 吞掉，也会留在 forbidden 中；仅仅「没抛异常」不能证明零 I/O。
"""
import json
import os
import subprocess
import sys

import pytest

from test_single_file_build import build


RUN_FLAT = r'''
import builtins
import importlib
import io
import json
import math
import os
import socket
import sys

source = open(sys.argv[1], encoding="gbk").read()
source = source.replace('"YOUR_ACCOUNT_ID"', '"flat-test"')
settings = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
for key, value in settings.items():
    source = source.replace('    "rpc_background_threads": False,',
        '    "%s": %r,\n    "rpc_background_threads": False,' % (key, value), 1)
code = compile(source, "<qmt-flat>", "exec")
forbidden = []
def deny(name):
    def fail(*args, **kwargs):
        forbidden.append(name)
        raise AssertionError("forbidden: " + name)
    return fail
if sys.argv[2] == "sandbox":
    for module, names in ((builtins, ("open",)), (io, ("open",)),
                         (os, ("open", "makedirs", "mkdir", "replace", "remove")),
                         (socket, ("socket", "create_connection"))):
        for name in names:
            setattr(module, name, deny(module.__name__ + "." + name))

calls, details = [], []
def download(*args, **kwargs):
    calls.append(args)
    return True
def detail(*args):
    details.append(args)
    return [{"m_strAccountID": "flat-test", "m_dAvailable": 12345.0}] if args[2] == "ACCOUNT" else []
injected = dict(passorder=lambda *a: None, cancel=lambda *a: True,
                get_trade_detail_data=detail, download_history_data=download,
                down_history_data=download)
class Context:
    def set_account(self, *args): pass
    def run_time(self, *args): pass
    def get_full_tick(self, *args): return {}
class Kernel:
    def __init__(self): self.closed = []
    def CancelIoEx(self, *args): pass
    def DisconnectNamedPipe(self, *args): pass
    def CloseHandle(self, handle): self.closed.append(handle)

ns = dict(injected)
previous, previous_kernel = None, None
for generation in range(2):
    # 第一代保持服务运行，让第二次 exec 走入口的真实 stop/clear/rebind 链路。
    exec(code, ns)
    assert importlib.import_module("math") is math
    assert importlib.reload(math) is math
    if previous is not None:
        assert not previous._running.is_set()
        assert not previous._transport._running
        assert previous_kernel.closed == [123]
    strategy = sys.modules["bigqmt_signal_trader_strategy"]
    pipe = ns["_load_local_module"]("bigqmt_signal_trader.transports.pipe_transport")
    # 只替换 Windows 内核 I/O，真实 accept 启停、drain、服务分发照常执行。
    pipe.NamedPipeTransport._accept_loop = lambda self: None
    kernel = Kernel()
    pipe.NamedPipeTransport._dll = lambda self: kernel
    context = Context()
    if sys.argv[2] == "binding_no_redis":
        strategy._config["rpc"]["transport"] = "pipe"
    ns["init"](context)
    service = strategy._rpc_service
    assert service is not None and service is not previous and service._running.is_set()
    handlers = service.handlers
    assert all(handlers.qmt_api.get(k) is v for k, v in injected.items()), handlers.qmt_api
    assert handlers.qmt_api.get("download_history_data2") is None
    if sys.argv[2] == "sandbox":
        assert getattr(handlers, "_order_identity_journals", None) is None
        logging_setup = ns["_load_local_module"]("bigqmt_signal_trader.logging_setup")
        logging_setup.get_logger("gate").warning("stdout only")
        logging_setup._FallbackLogger("gate").warning("fallback stdout only")
        assert logging_setup.log_file_path() is None
        handlers._remember_order_identity_local("flat-test", "remark-test", "strategy-test")
        assert handlers._order_identity_local[("flat-test", "remark-test")][1] == "strategy-test"
        assert getattr(handlers, "_order_identity_journals", None) is None
    else:
        expected = strategy._config["rpc"].get("order_identity_persistence", True)
        assert (getattr(handlers, "_order_identity_journals", None) is not None) == expected
    transport = service._transport
    incoming, replies = [], []
    if hasattr(transport, "_register_server_connection"):
        transport._drain_handles.append(transport._register_server_connection(123))
    else:
        transport._server_handles.append(123)
        transport._drain_handles.append(123)
    transport._peek_available = lambda handle: bool(incoming)
    transport._read = lambda handle: json.dumps(incoming.pop(0)).encode()
    transport._write = lambda handle, payload: replies.append(json.loads(payload))
    def send(method, params):
        request = dict(request_id=method, account_id="flat-test", method=method, params=params)
        incoming.append(request)
        transport.drain_request_queue()
        return request
    send("ping", {})
    assert len(replies) == 1 and replies[-1]["ok"] and replies[-1]["request_id"] == "ping", replies
    before = len(replies)
    send("query_account_infos", {})
    assert len(replies) == before and service.pending.qsize() == 1
    ns["adjust"](context)
    assert len(replies) == before + 1 and replies[-1]["ok"], replies
    assert replies[-1]["data"][0]["m_dAvailable"] == 12345.0 and details
    # 真实分发下载 RPC；终端没有批量全局，必须逐票调用注入函数。
    calls.clear()
    send("download_history_data2", dict(stock_list=["000001.SZ", "600000.SH"],
         period="1d", start_time="20260101", end_time="20261023"))
    ns["adjust"](context)
    assert replies[-1]["ok"], replies[-1]
    assert calls == [("000001.SZ", "1d", "20260101", "20261023"),
                     ("600000.SH", "1d", "20260101", "20261023")], calls
    if settings:
        send("probe_capabilities", {"download_probe": False})
        ns["adjust"](context)
        assert replies[-1]["request_id"] == "probe_capabilities" and replies[-1]["ok"], replies[-1]
        report = replies[-1]["data"]["thread_routing"]
        assert report["heavy_codes_threshold"] == settings["rpc_heavy_codes_threshold"]
        assert report["full_tick_warmup_wait_seconds"] == settings["full_tick_warmup_wait_seconds"]
        assert strategy._runner.get_app().market_data.full_tick_warmup_wait_seconds == settings["full_tick_warmup_wait_seconds"]
    assert forbidden == [], forbidden
    previous, previous_kernel = service, kernel
strategy.reset_app()
assert kernel.closed == [123]
assert forbidden == [], forbidden
print("FLAT_GATE_OK: two execs, real RPC results, forbidden calls=0")
'''


@pytest.mark.parametrize("kind", ["pipe_flat", "no_redis_flat"])
def test_flat_captures_actual_terminal_globals(kind):
    path, _ = build(kind)
    mode = "binding_no_redis" if kind == "no_redis_flat" else "binding"
    result = subprocess.run([sys.executable, "-c", RUN_FLAT, path, mode],
                            capture_output=True, text=True, timeout=30,
                            env=dict(os.environ, BIGQMT_LOG_ENABLED="0"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FLAT_GATE_OK" in result.stdout


@pytest.mark.parametrize("settings", [{}, {"rpc_heavy_codes_threshold": 64,
                                          "full_tick_warmup_wait_seconds": 0}])
def test_pipe_flat_zero_file_and_socket_calls(settings):
    path, _ = build("pipe_flat")
    env = dict(os.environ)
    env.pop("BIGQMT_LOG_ENABLED", None)  # 配置开关必须自己有效，不能靠环境变量救场。
    result = subprocess.run([sys.executable, "-c", RUN_FLAT, path, "sandbox", json.dumps(settings)],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FLAT_GATE_OK" in result.stdout
