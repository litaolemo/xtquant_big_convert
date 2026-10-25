"""真 Windows 管道 e2e；非 Windows 按原生 pipe 测试的约定跳过。"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

import pytest

from bigqmt_signal_trader.transports.pipe_relay import read_frame, write_frame
from bigqmt_signal_trader.transports.pipe_transport import NamedPipeTransport


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows named pipes required")


@pytest.fixture
def pair():
    server = NamedPipeTransport(pipe_name="bigqmt_relay_test_" + uuid.uuid4().hex)
    process = subprocess.Popen([sys.executable, "-B", "-m", "bigqmt_signal_trader.transports.pipe_relay"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    responses = queue.Queue()
    def read():
        try:
            while True:
                payload = read_frame(process.stdout)
                responses.put(payload)
                if payload is None: return
        except Exception as exc:
            responses.put(exc)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    yield server, process, responses
    if process.poll() is None: process.terminate()
    process.wait(timeout=5)
    reader.join(timeout=2)
    process.stdin.close()
    process.stdout.close()
    process.stderr.close()
    server.stop()


def handshake(server, process, responses, delay=False):
    write_frame(process.stdin, json.dumps(dict(pipe_path=server.path, connect_timeout_seconds=3)).encode())
    if delay: time.sleep(0.2)  # FILE_NOT_FOUND 启动竞态：relay 先等，服务端后起。
    server.start_receiving(lambda request: {"ok": True, "echo": request})
    assert responses.get(timeout=5) == b""


def test_real_relay_large_unicode_messages_and_eof(pair):
    server, process, responses = pair
    handshake(server, process, responses, delay=True)
    for request in ({"method": "ping"}, {"text": "测试\r\n\x1a" * (1 << 19)}):
        write_frame(process.stdin, json.dumps(request, ensure_ascii=False).encode())
        result = json.loads(responses.get(timeout=10))
        assert result == {"ok": True, "echo": request}
    process.stdin.close()
    assert process.wait(timeout=3) == 0
    assert process.stderr.read() == b""


def test_real_pipe_disconnect_exits_relay(pair):
    server, process, responses = pair
    handshake(server, process, responses)
    server.stop()
    write_frame(process.stdin, b'{"method":"ping"}')
    assert responses.get(timeout=5) is None
    assert process.wait(timeout=3) != 0
    assert len(process.stderr.read().splitlines()) == 1


def test_stdin_eof_interrupts_pending_native_read(pair):
    server, process, responses = pair
    handshake(server, process, responses)
    received = threading.Event()
    server._on_request = lambda request: received.set()  # None: 永远不回包。
    write_frame(process.stdin, b'{"method":"order_stock"}')
    assert received.wait(timeout=3)
    process.stdin.close()
    assert process.wait(timeout=3) == 0


def test_partial_stdin_is_never_forwarded(pair):
    server, process, responses = pair
    handshake(server, process, responses)
    received = []
    server._on_request = lambda request: received.append(request)
    process.stdin.write(b'\x00\x00\x00\x20{"method":')
    process.stdin.flush()
    process.stdin.close()
    assert process.wait(timeout=3) != 0
    assert received == []
