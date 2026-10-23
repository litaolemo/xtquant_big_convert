# coding: utf-8
"""Windows named-pipe transport (same-host, zero third-party dependencies).

Why this exists
---------------
Some broker QMT builds enforce an import whitelist that rejects ``socket``
(directly and indirectly -- ``logging.handlers`` pulled it in once) and forbid
``pip install`` into the bundled Python. On those terminals neither the Redis
client nor ``pyzmq`` can be installed, so the bridge has no wire at all.

A named pipe needs neither: ``ctypes.WinDLL("kernel32")`` is standard library,
and named pipes are not sockets, so they sit outside the whitelist that blocks
networking. Same trick the cfquant project uses.

What it buys, measured
----------------------
Raw round trip on this machine, 108-byte payload, 500 iterations::

    named pipe    median 0.012ms
    zmq REQ/REP   median 0.109ms
    redis list    median 0.798ms

66x faster than Redis **at the wire**. But the wire is not where the time goes:
end-to-end RPC against the live bridge measures 3-12ms median, so replacing
Redis with a pipe saves ~0.79ms of that -- 25% on the fastest calls, 7% on the
slowest. Pick this transport for the *dependency* story, not the speed story.

Limits
------
Windows only, same host only. No cross-machine, no Linux client, no Docker.
Deployments that need those keep Redis or ZMQ.
"""

import ctypes
import json
import os
import queue
import threading
import time

from .base import RpcTransport, TransportError, TransportTimeout
from ..adapters.redis_common import decode_text


DEFAULT_PIPE_NAME = "bigqmt_rpc"
_PIPE_ROOT = "\\\\.\\pipe\\"

# kernel32 constants (winbase.h)
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_MESSAGE = 0x00000004
PIPE_READMODE_MESSAGE = 0x00000002
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_BUSY = 231
ERROR_MORE_DATA = 234
ERROR_OPERATION_ABORTED = 995
ERROR_INVALID_HANDLE = 6
ERROR_BROKEN_PIPE = 109
ERROR_FILE_NOT_FOUND = 2

_BUFFER_BYTES = 1 << 20      # 1MB: whole-market quote frames are large
_CONNECT_POLL_SECONDS = 0.05


def _kernel32():
    if os.name != "nt":
        raise TransportError(
            "named-pipe transport is Windows-only (os.name=%r). Use redis or "
            "zmq for cross-platform deployments." % os.name)
    from ctypes import wintypes

    # 部分券商 QMT 内置 Python 3.6 的 ctypes.wintypes 缺 HANDLE/BOOL
    # （#419 切 pipe 即 AttributeError）。Win32 HANDLE 是指针宽、BOOL 是
    # 32 位有符号整型，用 c_void_p / c_int32 兜底，类型全的 build 不受影响。
    handle_type = getattr(wintypes, "HANDLE", ctypes.c_void_p)
    bool_type = getattr(wintypes, "BOOL", ctypes.c_int32)

    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateNamedPipeW.restype = handle_type
    dll.CreateFileW.restype = handle_type
    # drain 模式要用它做非阻塞探测：有数据才 ReadFile，绝不在 adjust 线程上阻塞
    dll.PeekNamedPipe.restype = bool_type
    return dll, wintypes


def pipe_path(name=DEFAULT_PIPE_NAME, account_id=""):
    """Full pipe path. The account id is part of the name so two bridges --
    one live account, one simulated -- never share a wire (the same mistake
    that gave two deployments one log file, #144)."""
    suffix = ("_" + str(account_id)) if account_id else ""
    return _PIPE_ROOT + str(name or DEFAULT_PIPE_NAME) + suffix


class _ClientHandleFinalizer(object):
    """一条客户端线程的句柄收尸人（#431）。

    threading.local 的值随线程退出被销毁，这个对象的 __del__ 就在那时
    触发 —— CPython 只给了这一个「线程退出」钩子。没有它之前，每条线程
    的管道句柄都活到进程退出：客户端每死一条线程，服务端就白留一条连接
    （background 模式下还搭一个工作线程），churn 下去一路积到
    PIPE_UNLIMITED_INSTANCES 上限，之后谁 CreateFileW 都是 PIPE_BUSY。

    释放只认 (ident, token) 全等的注册项：ident 和内核句柄值都会被系统
    回收复用，只靠其中任何一个比对都可能错关一条活连接的句柄。
    """

    __slots__ = ("_transport", "_ident", "_token")

    def __init__(self, transport, ident, token):
        self._transport = transport
        self._ident = ident
        self._token = token

    def __del__(self):
        try:
            self._transport._release_thread_handle(self._ident, self._token)
        except Exception:
            # 解释器退出边缘模块可能已被拆半，句柄由 OS 收，别在这里出事。
            pass


class NamedPipeTransport(RpcTransport):
    """Request/response over a Windows named pipe in message mode.

    Message mode (not byte mode) is deliberate: each WriteFile is one message
    and each ReadFile returns exactly one, so there is no framing protocol to
    get wrong. A message larger than the read buffer surfaces as
    ERROR_MORE_DATA rather than a silently truncated payload.
    """

    name = "pipe"

    def __init__(self, account_id="", print_prefix="[bigqmt_rpc]",
                 pipe_name=DEFAULT_PIPE_NAME, connect_timeout_seconds=5.0,
                 **kwargs):
        super(NamedPipeTransport, self).__init__(
            account_id=account_id, print_prefix=print_prefix)
        self.pipe_name = str(pipe_name or DEFAULT_PIPE_NAME)
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.path = pipe_path(self.pipe_name, account_id)
        self._k32 = None
        self._wintypes = None
        self._listener = None
        self._server_handles = []
        # drain 模式下由 adjust 线程轮询的已连接句柄
        self._background_threads = True
        self._drain_handles = []
        self._server_lock = threading.RLock()
        # Per-thread client handle. A single shared handle would serialise every
        # caller behind one round trip -- exactly the bug #186 fixed for the ZMQ
        # DEALER, and the reason that fix is not worth repeating here.
        self._client_local = threading.local()
        # 注册表 ident -> (handle, token)：线程退出时由 finalizer 精确释放，
        # finalizer 没跑成的由 _sweep_dead_thread_handles 兜底（#431）。
        self._client_threads = {}
        # 读缓冲也按线程持有 —— 见 _read_buffer 里的实测数字。
        self._io_local = threading.local()

    # -- ctypes plumbing ---------------------------------------------------
    def _dll(self):
        if self._k32 is None:
            self._k32, self._wintypes = _kernel32()
        return self._k32

    def _last_error(self):
        return ctypes.get_last_error()

    def _write(self, handle, payload):
        dll = self._dll()
        written = self._wintypes.DWORD()
        ok = dll.WriteFile(handle, payload, len(payload),
                           ctypes.byref(written), None)
        if not ok:
            raise TransportError("WriteFile failed (err=%s)" % self._last_error())
        return written.value

    def _read_buffer(self):
        """按线程复用读缓冲。

        原来每次 _read 都 create_string_buffer(1MB) —— 分配并清零 1MB，一次
        往返两侧各一次就是 2MB。实测：裸管道往返 0.012ms、两侧 JSON 合计
        0.010ms，预期总共 0.022ms，而传输层实测中位 0.765ms —— 多出来的
        0.74ms 全在这里。缓冲区按线程持有一份即可，读多少用多少。
        """
        buf = getattr(self._io_local, "buf", None)
        if buf is None:
            buf = ctypes.create_string_buffer(_BUFFER_BYTES)
            self._io_local.buf = buf
        return buf

    def _read(self, handle):
        """Read one message; b"" only for a CancelIoEx wake, raise for a real close.

        Two outcomes MUST be told apart now that writes are driven by waking
        the reader: CancelIoEx (a writer woke us so we can write) returns
        ERROR_OPERATION_ABORTED -- that is a wake, not a close; a peer going
        away (ERROR_BROKEN_PIPE / ERROR_INVALID_HANDLE) IS a close and the
        connection is over.
        """
        dll = self._dll()
        buf = self._read_buffer()
        read = self._wintypes.DWORD()
        chunks = []
        while True:
            ok = dll.ReadFile(handle, buf, _BUFFER_BYTES, ctypes.byref(read), None)
            if read.value:
                chunks.append(ctypes.string_at(buf, read.value))
            if ok:
                break
            err = self._last_error()
            if err == ERROR_MORE_DATA:
                continue
            if err == ERROR_OPERATION_ABORTED:
                return b""          # a writer woke us to write; not a close
            if err in (ERROR_INVALID_HANDLE, ERROR_BROKEN_PIPE):
                raise TransportError("pipe closed (err=%s)" % err)
            if not self._running:
                return b""
            if not chunks or not chunks[0]:
                return b""
            raise TransportError("ReadFile failed (err=%s)" % err)
        return b"".join(chunks)

    # -- client side -------------------------------------------------------
    def _client_handle(self):
        handle = getattr(self._client_local, "handle", None)
        if handle is not None:
            return handle
        self._sweep_dead_thread_handles()
        dll = self._dll()
        deadline = time.time() + self.connect_timeout_seconds
        while True:
            handle = dll.CreateFileW(self.path, GENERIC_READ | GENERIC_WRITE,
                                     0, None, OPEN_EXISTING, 0, None)
            if handle != INVALID_HANDLE_VALUE:
                break
            err = self._last_error()
            # FILE_NOT_FOUND is the startup race: start_receiving has spawned
            # the accept thread but it has not created the first instance yet.
            # Retrying it is exactly as legitimate as retrying PIPE_BUSY.
            if err not in (ERROR_PIPE_BUSY, ERROR_FILE_NOT_FOUND) \
                    or time.time() >= deadline:
                raise TransportError(
                    "cannot connect to %s (err=%s). Is the QMT-side strategy "
                    "running with transport=pipe?" % (self.path, err))
            time.sleep(_CONNECT_POLL_SECONDS)
        mode = self._wintypes.DWORD(PIPE_READMODE_MESSAGE)
        dll.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None)
        ident = threading.current_thread().ident
        token = object()
        self._client_local.handle = handle
        # 赋新 finalizer 会释放掉旧 finalizer 的最后一个引用，它的 __del__
        # 就地触发 —— 所以注册必须放在赋值之后：旧 finalizer 按 token 找
        # 自己的条目时，旧条目要么已被 _drop_client_handle 摘掉、要么根本
        # 没存在过，它必然空转，绝碰不到新句柄。
        self._client_local.finalizer = _ClientHandleFinalizer(self, ident, token)
        with self._server_lock:
            stale = self._client_threads.get(ident)
            self._client_threads[ident] = (handle, token)
        if stale is not None:
            # ident 被系统复用、旧线程的 finalizer 又没跑成（非 CPython 的
            # thread-local 回收时点不保证）。旧线程已死，ident 在我们手上，
            # 这条注册只可能是死线程的残留，替它关。
            try:
                dll.CloseHandle(stale[0])
            except Exception:
                pass
        return handle

    def _release_thread_handle(self, ident, token):
        """线程退出时关它自己的句柄 —— finalizer 的唯一入口。

        必须 (ident, token) 全等才动手：注册表里已是别的 token，说明这个
        ident 被新线程领走了，旧句柄由新线程注册时的 stale 分支（或
        _sweep）代关。内核句柄值会被回收复用，错关一次就是杀了一个无辜的
        活句柄 —— 那种 bug 只会在高并发下偶发，最难查。
        """
        with self._server_lock:
            entry = self._client_threads.get(ident)
            if entry is None or entry[1] is not token:
                return
            del self._client_threads[ident]
        try:
            self._dll().CloseHandle(entry[0])
        except Exception:
            pass

    def _sweep_dead_thread_handles(self):
        """finalizer 没跑成的条目在这里兜底，每次新建连接前过一次。

        ident 同一时刻只属于一条活线程，所以不在 threading.enumerate()
        里的 ident 一定是死线程的残留。finalizer 在 CPython 上是随线程
        退出即时触发的，这里只为回收时点不保证的运行时（或解释器退出
        边缘）兜底。
        """
        live = set(t.ident for t in threading.enumerate())
        dead = []
        with self._server_lock:
            for ident, entry in list(self._client_threads.items()):
                if ident in live:
                    continue
                del self._client_threads[ident]
                dead.append(entry[0])
        if not dead:
            return
        dll = self._dll()
        for handle in dead:
            try:
                dll.CloseHandle(handle)
            except Exception:
                pass

    def _cancel_read(self, handle):
        """Abort whatever ReadFile is pending on the handle -- the timeout
        mechanism for the client (#236 defect 3).

        The first design put a pump thread between the caller and the pipe so
        the caller could wait on a queue instead. That dead-locked on the very
        first request: a synchronous (non-overlapped) handle serializes I/O,
        so a WriteFile issued while the pump sat in a blocking ReadFile on the
        SAME handle never completes. The alternatives were overlapped I/O or a
        second pipe per direction; a watchdog that cancels the blocking read
        at the deadline keeps single-threaded I/O and gets the same real
        timeout with none of that surgery. CancelIoEx is already the proven
        shutdown mechanism on the server side (the stop() deadlock fix).
        """
        try:
            self._dll().CancelIoEx(handle, None)
        except Exception:
            pass

    def send_request(self, request, timeout_seconds):
        payload = json.dumps(request, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self._write(self._client_handle(), payload)
        except TransportError:
            # A write-side failure is provable: nothing left this process, so
            # ONE reconnect-and-resend is safe (never a duplicate). A read-side
            # failure is the unknown-outcome case and is NOT retried here --
            # that is the #195 class of bug. The caller decides on those.
            self._drop_client_handle()
            self._write(self._client_handle(), payload)
        return json.loads(decode_text(
            self._read_with_watchdog(self._client_handle(), timeout_seconds)))

    def _read_with_watchdog(self, handle, timeout_seconds):
        watchdog = None
        if timeout_seconds:
            watchdog = threading.Timer(
                float(timeout_seconds), self._cancel_read, args=(handle,))
            watchdog.daemon = True
            watchdog.start()
        try:
            raw = self._read(handle)
        except TransportError:
            self._drop_client_handle()
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
        if not raw:
            # Empty read is the watchdog's ERROR_OPERATION_ABORTED, a closed
            # handle, or a broken pipe -- all mean the answer is not coming.
            self._drop_client_handle()
            raise TransportTimeout("named pipe rpc timeout")
        return raw

    def _drop_client_handle(self):
        handle = getattr(self._client_local, "handle", None)
        if handle is None:
            return
        # 本线程还活着，ident 此刻只属于它，按 ident 摘即可。thread-local
        # 里的旧 finalizer 留下无妨：它触发时按 token 找不到条目，空转。
        with self._server_lock:
            self._client_threads.pop(threading.current_thread().ident, None)
        try:
            self._dll().CloseHandle(handle)
        except Exception:
            pass
        self._client_local.handle = None

    # -- server side -------------------------------------------------------
    def start_receiving(self, on_request, **kwargs):
        """两种模式，和 zmq / redis 一致（#183）。

        background_threads=True  每连接一个工作线程收发（原有行为）
        background_threads=False adjust 线程在 drain_request_queue 里非阻塞轮询

        实测 background_threads=True 时请求要 ~200ms 才到达 handler，而
        handler 本身 0.0ms —— 和 #183 记录的 zmq 404ms 是同一个形状。drain
        模式把读、处理、写全放回 adjust 线程，去掉跨线程交接。

        两种模式下 accept 都留一个线程：它只阻塞在 ConnectNamedPipe 上，不碰
        请求数据，代价是一个常驻线程而不是每连接一个。
        """
        super(NamedPipeTransport, self).start_receiving(on_request)
        background = kwargs.get("background_threads")
        self._background_threads = True if background is None else bool(background)
        self._listener = threading.Thread(
            target=self._accept_loop, name="bigqmt-pipe-accept")
        self._listener.daemon = True
        self._listener.start()

    def _accept_loop(self):
        dll = self._dll()
        while self._running:
            handle = dll.CreateNamedPipeW(
                self.path, PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
                PIPE_UNLIMITED_INSTANCES, _BUFFER_BYTES, _BUFFER_BYTES, 0, None)
            if handle == INVALID_HANDLE_VALUE:
                if not self._running:
                    return
                time.sleep(_CONNECT_POLL_SECONDS)
                continue
            with self._server_lock:
                self._server_handles.append(handle)
            connected = dll.ConnectNamedPipe(handle, None)
            if not connected and self._last_error() != ERROR_PIPE_CONNECTED:
                self._close_server_handle(handle)
                continue
            if not self._running:
                self._close_server_handle(handle)
                return
            if not self._background_threads:
                # drain 模式：不起工作线程，交给 adjust 线程轮询这个句柄。
                self._drain_handles.append(handle)
                continue
            worker = threading.Thread(
                target=self._serve_connection, args=(handle,),
                name="bigqmt-pipe-conn")
            worker.daemon = True
            worker.start()

    def _serve_connection(self, handle):
        """One connection, single-threaded I/O: reads AND writes happen here.

        This is the deferred-answer defect, finally explained: a deferred
        answer is produced on the adjust thread while this worker sits in a
        blocking ReadFile -- and a synchronous handle does not tolerate a
        WriteFile concurrent with a pending ReadFile (measured: the write
        blocks forever, or fails err=232 ERROR_NO_DATA). So nobody may write
        except this thread. send_response on any thread only QUEUES into the
        outbox and CancelIoEx's the pending read; this loop then writes the
        payload itself. Write-before-read ordering plus the writer's
        queue-before-cancel ordering means a cancelled-for payload is always
        visible when the aborted read returns.
        """
        outbox = queue.Queue()
        try:
            while self._running:
                while not outbox.empty():
                    self._write(handle, outbox.get())
                try:
                    raw = self._read(handle)
                except TransportError:
                    return
                if not raw:
                    # Woken by a writer (payload comes out at the top of the
                    # loop) or by stop(). Not a close -- a real close raises.
                    continue
                try:
                    request = json.loads(decode_text(raw))
                except Exception:
                    return
                # Remember which outbox to answer on. send_response reads it
                # back, so inline and deferred answers both reach this peer.
                request["_pipe_handle"] = handle
                request["_pipe_outbox"] = outbox
                self.deliver(request)
        finally:
            # Flush whatever is queued before closing: the reload reply is a
            # deferred answer, and a stop() that swallows it is exactly the
            # reload self-teardown defect.
            try:
                while not outbox.empty():
                    self._write(handle, outbox.get())
            except Exception:
                pass
            self._close_server_handle(handle)

    def _peek_available(self, handle):
        """这个句柄上有没有待读数据。PeekNamedPipe 不阻塞，可用于同步句柄。"""
        dll = self._dll()
        avail = self._wintypes.DWORD(0)
        ok = dll.PeekNamedPipe(handle, None, 0, None,
                               ctypes.byref(avail), None)
        if not ok:
            raise TransportError("PeekNamedPipe failed (err=%s)" % self._last_error())
        return avail.value

    def drain_request_queue(self, max_items=20):
        """drain 模式：adjust 线程自己把请求读出来、处理掉、把响应写回去。

        读、handler、写全在同一条线程上，所以没有跨线程交接，也不需要
        outbox + CancelIoEx 那套唤醒机制 —— 那套是 background_threads=True
        时才需要的。

        非阻塞：先 PeekNamedPipe 看有没有数据，有才 ReadFile。没有就跳过，
        绝不能在 adjust 线程上阻塞等下一个请求。
        """
        if self._background_threads:
            return 0                 # 工作线程在收，这里不插手
        processed = 0
        for handle in list(self._drain_handles):
            while processed < int(max_items):
                try:
                    if not self._peek_available(handle):
                        break
                    raw = self._read(handle)
                except TransportError:
                    self._forget_drain_handle(handle)
                    break
                if not raw:
                    break
                try:
                    request = json.loads(decode_text(raw))
                except Exception:
                    self._forget_drain_handle(handle)
                    break
                request["_pipe_handle"] = handle
                response = None
                try:
                    response = self.deliver(request)
                except Exception as exc:
                    response = {"schema_version": 1,
                                "request_id": request.get("request_id"),
                                "method": request.get("method"),
                                "ok": False,
                                "error": "%s: %s" % (exc.__class__.__name__, exc)}
                if response is not None:
                    try:
                        self._write(handle, json.dumps(
                            response, ensure_ascii=False, default=str).encode("utf-8"))
                    except Exception:
                        self._forget_drain_handle(handle)
                        break
                processed += 1
        return processed

    def _forget_drain_handle(self, handle):
        try:
            self._drain_handles.remove(handle)
        except ValueError:
            pass
        self._close_server_handle(handle)


    def send_response(self, request, response):
        """Queue the answer onto the connection's outbox and wake its worker.

        Never writes directly: only the connection worker may write (a
        synchronous handle does not tolerate a WriteFile concurrent with a
        pending ReadFile -- the deferred-answer defect). The worker queues
        BEFORE cancelling, so the payload is always visible by the time the
        aborted read returns.
        """
        handle = (request or {}).get("_pipe_handle")
        if handle is None:
            raise TransportError("no pipe handle on the request to reply to")
        payload = json.dumps(response, ensure_ascii=False, default=str).encode("utf-8")
        outbox = (request or {}).get("_pipe_outbox")
        if outbox is None:
            # drain 模式：没有工作线程，adjust 线程是唯一的读者也是唯一的写者，
            # 没有并发读写的问题，直接写。（漏了这一支时服务端自己发响应会抛
            # 「no pipe handle」，客户端表现为超时，实盘上就是这么卡住的。）
            self._write(handle, payload)
            return
        outbox.put(payload)
        self._cancel_read(handle)

    def _close_server_handle(self, handle):
        # 先把句柄从表里摘掉，摘到的那个线程才负责真正关闭。stop() 和工作线程
        # 的 finally 会同时走到这里，重复 CloseHandle 会关掉一个已被回收复用的
        # 句柄 —— 那种 bug 只会在高并发下偶发，最难查。
        with self._server_lock:
            if handle not in self._server_handles:
                return
            self._server_handles.remove(handle)
        dll = self._dll()
        # **必须先取消挂起的 I/O。** DisconnectNamedPipe 会等同一句柄上挂起的
        # 同步 ReadFile 完成，而工作线程正阻塞在那个 ReadFile 上等下一个请求 ——
        # 谁也等不到谁，主线程和 20 个工作线程一起卡死（实测线程栈确认）。
        # CancelIoEx 让那次读带 ERROR_OPERATION_ABORTED 返回，循环随即退出。
        try:
            dll.CancelIoEx(handle, None)
        except Exception:
            pass
        try:
            dll.DisconnectNamedPipe(handle)
        except Exception:
            pass
        try:
            dll.CloseHandle(handle)
        except Exception:
            pass

    def stop(self):
        self._running = False
        with self._server_lock:
            server = list(self._server_handles)
            clients = [entry[0] for entry in self._client_threads.values()]
            # 先清空注册表：之后各线程 finalizer 触发时找不到条目，自然不会
            # 对同一批句柄关第二次。
            self._client_threads = {}
        # Unblock the accept loop: it is parked in ConnectNamedPipe, which only
        # returns once somebody connects. Connect to our own pipe once.
        if server:
            try:
                dll = self._dll()
                handle = dll.CreateFileW(self.path, GENERIC_READ | GENERIC_WRITE,
                                         0, None, OPEN_EXISTING, 0, None)
                if handle != INVALID_HANDLE_VALUE:
                    dll.CloseHandle(handle)
            except Exception:
                pass
        for handle in server:
            self._close_server_handle(handle)
        dll = self._dll()
        for handle in clients:
            try:
                # CancelIoEx BEFORE CloseHandle even though there is no pump:
                # a send_request on another thread can have a blocking
                # ReadFile pending on this handle, and CloseHandle on a
                # synchronous handle with pending I/O blocks until it
                # completes -- i.e. stop() hangs for as long as the server
                # stays silent (this test's exact failure).
                dll.CancelIoEx(handle, None)
            except Exception:
                pass
            try:
                dll.CloseHandle(handle)
            except Exception:
                pass
        self._client_local = threading.local()
        self._io_local = threading.local()
        super(NamedPipeTransport, self).stop()
