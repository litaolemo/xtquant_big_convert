"""真正 exec 构建产物：#202 注入绑定、#420/#433 禁文件/禁网络门禁。

子进程隔离 flat loader 对 sys.modules/importlib 的替换。禁调用即使被日志或
journal 吞掉，也会留在 forbidden 中；仅仅「没抛异常」不能证明零 I/O。
"""
import os
import subprocess
import sys

import pytest

from test_single_file_build import build


RUN_FLAT = r'''
import builtins
import io
import json
import os
import socket
import sys

source = open(sys.argv[1], encoding="gbk").read()
source = source.replace('"YOUR_ACCOUNT_ID"', '"flat-test"')
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

calls = []
def download(*args, **kwargs):
    calls.append(args)
    return True
def detail(*args):
    return []
injected = dict(passorder=lambda *a: None, cancel=lambda *a: True,
                get_trade_detail_data=detail, download_history_data=download,
                down_history_data=download)
ns = dict(injected)
exec(code, ns)
strategy = sys.modules["bigqmt_signal_trader_strategy"]
# 非 Windows 的替身只接管内核 I/O，真实 pipe drain / 服务分发仍照常执行。
pipe = ns["_load_local_module"]("bigqmt_signal_trader.transports.pipe_transport")
pipe.NamedPipeTransport._accept_loop = lambda self: None
class Context:
    def set_account(self, *args): pass
    def run_time(self, *args): pass
    def get_full_tick(self, *args): return {}
context = Context()
ns["init"](context)
service = strategy._rpc_service
assert service is not None
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
    # pipe flat 默认关闭，no-redis flat 默认仍保留 journal。
    expected = strategy._config["rpc"].get("order_identity_persistence", True)
    assert (getattr(handlers, "_order_identity_journals", None) is not None) == expected
transport = service._transport
incoming, replies = [], []
transport._drain_handles = [123]
transport._peek_available = lambda handle: bool(incoming)
transport._read = lambda handle: json.dumps(incoming.pop(0)).encode()
transport._write = lambda handle, payload: replies.append(json.loads(payload))
def send(method, params):
    request = dict(request_id=method, account_id="flat-test", method=method, params=params)
    incoming.append(request)
    transport.drain_request_queue()
    return request
try:
    send("ping", {})
    assert replies[-1]["ok"] and replies[-1]["request_id"] == "ping", replies
    before = len(replies)
    send("query_account_infos", {})
    assert len(replies) == before and service.pending.qsize() == 1
    ns["adjust"](context)
    assert len(replies) == before + 1 and replies[-1]["ok"], replies
    # 下载在主线程 deferred；终端没有批量全局，必须逐票调用注入函数。
    send("download_history_data2", dict(stock_list=["000001.SZ", "600000.SH"],
         period="1d", start_time="20260101", end_time="20261023"))
    ns["adjust"](context)
    assert replies[-1]["ok"], replies[-1]
    assert calls == [("000001.SZ", "1d", "20260101", "20261023"),
                     ("600000.SH", "1d", "20260101", "20261023")], calls
finally:
    transport._drain_handles = []
    strategy.reset_app()
assert forbidden == [], forbidden
print("FLAT_GATE_OK")
'''


@pytest.mark.parametrize("kind", ["pipe_flat", "no_redis_flat"])
def test_flat_captures_actual_terminal_globals(kind):
    path, _ = build(kind)
    # no-redis 版只验证同一个入口绑定和真实 handlers，传输用 pipe 替身。
    script = RUN_FLAT
    if kind == "no_redis_flat":
        script = script.replace('ns["init"](context)',
            'strategy._config["rpc"]["transport"] = "pipe"\nns["init"](context)')
    result = subprocess.run([sys.executable, "-c", script, path, "binding"],
                            capture_output=True, text=True, timeout=30,
                            env=dict(os.environ, BIGQMT_LOG_ENABLED="0"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FLAT_GATE_OK" in result.stdout


def test_pipe_flat_zero_file_and_socket_calls():
    path, _ = build("pipe_flat")
    env = dict(os.environ)
    env.pop("BIGQMT_LOG_ENABLED", None)  # 配置开关必须自己有效，不能靠环境变量救场。
    result = subprocess.run([sys.executable, "-c", RUN_FLAT, path, "sandbox"],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FLAT_GATE_OK" in result.stdout
