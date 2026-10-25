# coding: utf-8
"""非 Windows 客户端的按线程 stdio 中继（#431 的生命周期，#195 的不重发边界）。

与 NamedPipeTransport 分开：这里管理 POSIX 子进程/非阻塞 stdio，原生类管理
Windows 句柄及服务端 drain；把两套失败/关闭机制混进一个类更难审计。
"""
import json
import math
import os
import select
import subprocess
import threading
import time

from .base import RpcTransport, TransportError, TransportTimeout
from .pipe_transport import pipe_path, DEFAULT_PIPE_NAME
from .pipe_relay import frame_header, frame_size


class _IncompleteWrite(TransportError):
    """完整帧尚未交给中继，才允许重建并重发一次。"""


class _RelayProcess(object):
    def __init__(self, command):
        # 不用 shell，不把账号放进 argv；stderr 继承，避免诊断管道填满卡死。
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, bufsize=0)
        self.closed = False
        self._closing = threading.Event()
        self._lock = threading.Lock()
        try:
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
        except BaseException:
            self.close()
            raise

    def close(self):
        # 先通知 I/O 循环退出，避免它在短 select 之间反复抢锁、饿死关闭线程。
        self._closing.set()
        with self._lock:
            if self.closed:
                return
            self.closed = True
            process = self.process
            # stdin EOF 让 Windows interop 另一端也退出，不能只依赖 WSL 的信号。
            process.stdin.close()
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class _RelayFinalizer(object):
    def __init__(self, transport, ident, token):
        self.transport, self.ident, self.token = transport, ident, token

    def __del__(self):
        try:
            self.transport._release(self.ident, self.token)
        except Exception:
            pass


class PipeRelayTransport(RpcTransport):
    """每条调用线程常驻一个中继；一次 RPC 的期限含启动、握手、写入和读回。"""
    name = "pipe"

    def __init__(self, relay_command, account_id="", print_prefix="[bigqmt_rpc]",
                 pipe_name=DEFAULT_PIPE_NAME, connect_timeout_seconds=5.0):
        super(PipeRelayTransport, self).__init__(account_id, print_prefix)
        if (not isinstance(relay_command, (list, tuple)) or not relay_command
                or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in relay_command)):
            raise ValueError("pipe.relay_command must be a non-empty argv list")
        self.command = list(relay_command)
        self.path = pipe_path(pipe_name, account_id)
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        if not math.isfinite(self.connect_timeout_seconds) or self.connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be finite and positive")
        self._local = threading.local()
        self._processes = {}  # ident -> (process, token), #431 防 ident 复用误关。
        self._lock = threading.RLock()
        self._closed = False

    def _release(self, ident, token):
        with self._lock:
            entry = self._processes.get(ident)
            if entry is None or entry[1] is not token:
                return
            del self._processes[ident]
        entry[0].close()

    def _sweep(self):
        with self._lock:
            # 快照必须与注册同锁：锁外取到的 live 可能漏掉刚注册的新线程。
            live = set(t.ident for t in threading.enumerate())
            dead = [(ident, entry[1]) for ident, entry in self._processes.items() if ident not in live]
        for ident, token in dead:
            self._release(ident, token)

    @staticmethod
    def _io(relay, data, writing, deadline):
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TransportTimeout("pipe relay rpc timeout; outcome may be unknown")
            # stop 与正在 select 的线程并发：不能关闭 fd 后仍按旧整数读写，
            # fd 可能已分给另一条连接。短轮询把关闭等待限制在 50ms，锁内完成
            # readiness + 非阻塞 I/O，跨线程关闭永远不会误用复用的 fd（#431）。
            with relay._lock:
                if relay.closed or relay._closing.is_set():
                    raise TransportError("pipe relay closed")
                stream = relay.process.stdin if writing else relay.process.stdout
                fd = stream.fileno()
                wait = 0.05 if remaining is None else min(0.05, remaining)
                readable, writable, _ = select.select(
                    [] if writing else [fd], [fd] if writing else [], [], wait)
                if not (writable if writing else readable):
                    continue
                try:
                    return os.write(fd, data) if writing else os.read(fd, data)
                except (BlockingIOError, InterruptedError):
                    continue

    def _write_frame(self, relay, payload, deadline):
        data = memoryview(frame_header(len(payload)) + payload)
        try:
            while data:
                written = self._io(relay, data, True, deadline)
                if written <= 0:
                    raise OSError("stdin closed")
                data = data[written:]
        except TransportTimeout:
            raise  # TimeoutError 也是 OSError；超时绝不能掉进安全重试分支。
        except (OSError, ValueError) as exc:
            # os.write 成功返回全部字节后绝不再进此分支。relay 只转发完整帧，
            # 半帧加 EOF 不可能成为 WriteFile；这是唯一可安全重试的边界。
            raise _IncompleteWrite("pipe relay stdin write failed") from exc

    def _read_exact(self, relay, size, deadline):
        chunks = []
        while size:
            chunk = self._io(relay, size, False, deadline)
            if not chunk:
                raise TransportError("pipe relay EOF; outcome unknown")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    def _read_frame(self, relay, deadline):
        header = self._read_exact(relay, 4, deadline)
        return self._read_exact(relay, frame_size(header), deadline)

    def _connection(self, deadline):
        relay = getattr(self._local, "relay", None)
        if relay is not None and not relay.closed:
            return relay
        self._sweep()
        ident, token = threading.current_thread().ident, object()
        with self._lock:
            if self._closed:
                raise TransportError("pipe relay transport closed")
            relay = _RelayProcess(self.command)
            stale = self._processes.pop(ident, None)
            self._local.relay = relay
            self._local.finalizer = _RelayFinalizer(self, ident, token)
            self._processes[ident] = (relay, token)
        if stale is not None:
            stale[0].close()
        connect_deadline = time.monotonic() + self.connect_timeout_seconds
        if deadline is not None:
            connect_deadline = min(deadline, connect_deadline)
        hello = json.dumps(dict(pipe_path=self.path,
                                connect_timeout_seconds=self.connect_timeout_seconds)).encode("utf-8")
        self._write_frame(relay, hello, connect_deadline)
        if self._read_frame(relay, connect_deadline) != b"":
            raise TransportError("invalid pipe relay handshake")
        return relay

    def _drop(self):
        finalizer = getattr(self._local, "finalizer", None)
        if finalizer is not None:
            self._release(finalizer.ident, finalizer.token)
        self._local.relay = None

    def send_request(self, request, timeout_seconds):
        payload = json.dumps(request, ensure_ascii=False, default=str).encode("utf-8")
        frame_header(len(payload))
        deadline = time.monotonic() + float(timeout_seconds) if timeout_seconds else None
        try:
            for attempt in range(2):
                relay = self._connection(deadline)
                try:
                    self._write_frame(relay, payload, deadline)
                except _IncompleteWrite:
                    self._drop()
                    if attempt == 0:
                        continue
                    raise
                # 完整帧已交给中继：从此所有错误（含 EOF、坏 JSON、超时）
                # 都是未知结果，绝不能自动重发下单/撤单（#195/#245）。
                return json.loads(self._read_frame(relay, deadline).decode("utf-8"))
        except BaseException as exc:
            self._drop()
            if isinstance(exc, (TransportError, TransportTimeout)):
                raise
            if isinstance(exc, (OSError, ValueError)):
                raise TransportError("pipe relay failed; outcome may be unknown") from exc
            raise

    def stop(self):
        with self._lock:
            self._closed = True
            processes = list(self._processes.values())
            self._processes.clear()
        for relay, _ in processes:
            relay.close()
        super(PipeRelayTransport, self).stop()

    close = stop
