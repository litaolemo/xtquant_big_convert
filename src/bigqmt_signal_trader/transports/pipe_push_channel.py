# coding: utf-8
"""Server->client event push over a dedicated named pipe (#408).

The RPC pipe (pipe_transport) is request/response with single-threaded I/O
per connection -- a WriteFile concurrent with a pending ReadFile blocks
forever on a synchronous handle. Interleaving server-initiated frames there
would break that serialization, so push gets its own pipe with exactly one
direction -- the server only writes, clients only read -- and no handle
ever sees concurrent read+write.

Same publish/subscriber interface as ZmqQuotePushChannel, so exec events
(order/trade/order_error/cancel_error) and whole-quote frames ride it
unchanged. The publisher keeps one handle per connected client and fan-outs
each frame under that handle's write lock; a write error means the client
went away and the handle is dropped. The subscriber loops a blocking
ReadFile with reconnect, filters topics by prefix (zmq SUBSCRIBE
semantics), and owns the same per-subscriber stop Event as the zmq channel
(#403): a join timeout can never leak a zombie that a later start revives.
"""

import ctypes
import json
import threading
import time

from .pipe_transport import (
    _BUFFER_BYTES,
    _CONNECT_POLL_SECONDS,
    _kernel32,
    _ServerPipeConnection,
    pipe_path,
    DEFAULT_PIPE_NAME,
    ERROR_BROKEN_PIPE,
    ERROR_FILE_NOT_FOUND,
    ERROR_INVALID_HANDLE,
    ERROR_MORE_DATA,
    ERROR_OPERATION_ABORTED,
    ERROR_PIPE_BUSY,
    ERROR_PIPE_CONNECTED,
    GENERIC_READ,
    GENERIC_WRITE,
    INVALID_HANDLE_VALUE,
    OPEN_EXISTING,
    PIPE_READMODE_MESSAGE,
    PIPE_TYPE_MESSAGE,
    PIPE_REJECT_REMOTE_CLIENTS,
    PIPE_ACCESS_DUPLEX,
    PIPE_UNLIMITED_INSTANCES,
    PIPE_WAIT,
)


def pipe_events_path(name=DEFAULT_PIPE_NAME, account_id=""):
    """The event-push pipe path, derived from the RPC pipe name so both
    sides compute it from the same config block without discovery (#408)."""
    return pipe_path(str(name or DEFAULT_PIPE_NAME) + "_events", account_id)


class PipeQuotePushChannel(object):
    """Named-pipe push channel: bind_address publishes, connect_address subscribes."""

    def __init__(self, bind_address=None, connect_address=None,
                 print_prefix="[bigqmt_quote_push]"):
        self.bind_address = bind_address
        self.connect_address = connect_address
        self.print_prefix = print_prefix
        self._running = False
        self._accept_thread = None
        self._sub_handles = {}
        self._sub_handles_lock = threading.Lock()
        self._publisher_stop = threading.Event()
        self._publisher_lifecycle_lock = threading.RLock()
        self._accepting = None
        self._sub_thread = None
        self._subscriber_stop = None
        self._stale_subscriber_threads = []
        self.subscriber_join_seconds = 1.0
        self._client_handle = None
        self._client_handle_lock = threading.Lock()

    def _dll(self):
        return _kernel32()

    def _last_error(self):
        return ctypes.get_last_error()

    # -- server side -------------------------------------------------------
    def start_publisher(self):
        if not self.bind_address:
            raise ValueError("bind_address is required to start a publisher")
        with self._publisher_lifecycle_lock:
            if self._accept_thread is not None and self._accept_thread.is_alive():
                raise RuntimeError("pipe publisher already running")
            with self._sub_handles_lock:
                self._running = True
                self._publisher_stop = threading.Event()
            thread = threading.Thread(
                target=self._accept_loop, name="bigqmt-quote-push-pipe-accept")
            thread.daemon = True
            self._accept_thread = thread
            thread.start()

    def _accept_loop(self):
        dll, _ = self._dll()
        stopped = self._publisher_stop
        while self._running and not stopped.is_set():
            # DUPLEX：订阅端 SetNamedPipeHandleState 需要 GENERIC_WRITE（#408）。
            handle = dll.CreateNamedPipeW(
                self.bind_address, PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                PIPE_UNLIMITED_INSTANCES, _BUFFER_BYTES, _BUFFER_BYTES, 0, None)
            if handle == INVALID_HANDLE_VALUE:
                if not self._running or stopped.is_set():
                    return
                time.sleep(_CONNECT_POLL_SECONDS)
                continue
            connection = _ServerPipeConnection(handle)
            with self._sub_handles_lock:
                if self._running and not stopped.is_set():
                    self._accepting = connection
                else:
                    connection.close(dll)
                    return
            try:
                connected = connection.call(dll.ConnectNamedPipe, None)
                if connected is None or (not connected and self._last_error() != ERROR_PIPE_CONNECTED):
                    continue
                with self._sub_handles_lock:
                    if not self._running or stopped.is_set() or connection.closing.is_set():
                        return
                    self._sub_handles[handle] = connection
                connection = None
            finally:
                with self._sub_handles_lock:
                    self._accepting = None
                if connection is not None:
                    connection.close(dll)

    def publish(self, topic, data):
        if not self._running or self._publisher_stop.is_set():
            return
        payload = json.dumps(
            {"topic": str(topic), "data": data},
            ensure_ascii=False, default=str).encode("utf-8")
        dll, wintypes = self._dll()
        with self._sub_handles_lock:
            connections = list(self._sub_handles.values())
        def write(handle):
            written = wintypes.DWORD()
            return dll.WriteFile(handle, payload, len(payload), ctypes.byref(written), None)
        for connection in connections:
            if not connection.call(write):
                self._drop_handle(connection)

    def _drop_handle(self, connection):
        dll, _ = self._dll()
        connection.closing.set()
        with self._sub_handles_lock:
            if self._sub_handles.get(connection.handle) is connection:
                del self._sub_handles[connection.handle]
        connection.close(dll)

    def publisher_subscriber_count(self):
        """Connected clients right now (diagnostic)."""
        with self._sub_handles_lock:
            return len(self._sub_handles)

    # -- client side -------------------------------------------------------
    def start_subscriber(self, topics, on_msg):
        if not self.connect_address:
            raise ValueError("connect_address is required to start a subscriber")
        # Self-contained, same lifecycle as the zmq channel (#403): never
        # rely on the caller to stop() first, and a join timeout means "still
        # in its callback", not "leaked".
        self._stop_subscriber()
        self._running = True
        stopped = self._subscriber_stop = threading.Event()
        self._sub_thread = threading.Thread(
            target=self._sub_loop,
            args=(set(str(t) for t in (topics or [])), on_msg, stopped),
            name="bigqmt-quote-push-pipe-sub")
        self._sub_thread.daemon = True
        self._sub_thread.start()

    def _connect_client(self, stopped):
        dll, wintypes = self._dll()
        while not stopped.is_set():
            handle = dll.CreateFileW(self.connect_address,
                                     GENERIC_READ | GENERIC_WRITE,
                                     0, None, OPEN_EXISTING, 0, None)
            if handle != INVALID_HANDLE_VALUE:
                # The handle's read mode defaults to BYTE, where a message
                # larger than one ReadFile buffer comes back ok=TRUE in
                # buffer-sized slices and the JSON reassembly below decodes
                # each slice as its own (broken) event -- small frames work,
                # big frames vanish silently. MESSAGE mode is what surfaces
                # ERROR_MORE_DATA (the RPC pipe client sets it too).
                mode = wintypes.DWORD(PIPE_READMODE_MESSAGE)
                dll.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None)
                return handle
            err = self._last_error()
            if err == ERROR_PIPE_BUSY:
                dll.WaitNamedPipeW(self.connect_address, 200)
            else:
                # ERROR_FILE_NOT_FOUND: the publisher is not up (yet). Retry
                # at the same cadence as the RPC pipe's connect.
                time.sleep(_CONNECT_POLL_SECONDS)
        return None

    def _read_one_message(self, dll, wintypes, handle, buf):
        """One complete message, or None on close/abort (caller decides)."""
        read = wintypes.DWORD()
        chunks = []
        while True:
            ok = dll.ReadFile(handle, buf, _BUFFER_BYTES,
                              ctypes.byref(read), None)
            if read.value:
                chunks.append(ctypes.string_at(buf, read.value))
            if ok:
                return b"".join(chunks)
            err = self._last_error()
            if err == ERROR_MORE_DATA:
                continue
            if err in (ERROR_OPERATION_ABORTED, ERROR_INVALID_HANDLE,
                       ERROR_BROKEN_PIPE):
                return None
            return None

    def _sub_loop(self, topics, on_msg, stopped):
        dll, wintypes = self._dll()
        buf = ctypes.create_string_buffer(_BUFFER_BYTES)
        try:
            while not stopped.is_set():
                handle = self._connect_client(stopped)
                if handle is None:
                    return
                with self._client_handle_lock:
                    self._client_handle = handle
                try:
                    while not stopped.is_set():
                        raw = self._read_one_message(dll, wintypes, handle, buf)
                        if raw is None:
                            # Server restart (reconnect) or stop() (the loop
                            # condition exits).
                            break
                        try:
                            event = json.loads(raw.decode("utf-8"))
                        except Exception:
                            continue
                        topic = str(event.get("topic") or "")
                        if topics and not any(topic.startswith(t) for t in topics):
                            continue
                        try:
                            on_msg(topic, event.get("data"))
                        except Exception as exc:
                            print("%s subscriber callback failed: %s"
                                  % (self.print_prefix, exc))
                finally:
                    with self._client_handle_lock:
                        if self._client_handle is handle:
                            self._client_handle = None
                    try:
                        dll.CloseHandle(handle)
                    except Exception:
                        pass
                if not stopped.is_set():
                    time.sleep(_CONNECT_POLL_SECONDS)
        except Exception:
            return

    def _stop_subscriber(self):
        event = self._subscriber_stop
        if event is not None:
            event.set()
        with self._client_handle_lock:
            handle = self._client_handle
        if handle is not None:
            try:
                dll, _ = self._dll()
                # Wake the blocking ReadFile so the loop sees the set Event;
                # CloseHandle on a handle with pending I/O would block until
                # the read completes (the RPC pipe's stop() learned this the
                # hard way).
                dll.CancelIoEx(handle, None)
            except Exception:
                pass
        thread = self._sub_thread
        if thread is not None and thread.is_alive():
            thread.join(self.subscriber_join_seconds)
            if thread.is_alive():
                self._stale_subscriber_threads.append(thread)
                print("%s subscriber did not stop within %.1fs; its stop "
                      "event is set and it will exit when its callback "
                      "returns (tracked, not abandoned)"
                      % (self.print_prefix, self.subscriber_join_seconds))
        self._sub_thread = None
        self._subscriber_stop = None

    def active_subscribers(self):
        """Live subscriber threads right now; healthy is 0 or 1 (#403)."""
        alive = []
        for thread in self._stale_subscriber_threads:
            if thread.is_alive():
                alive.append(thread)
        self._stale_subscriber_threads = alive
        current = self._sub_thread
        return len(alive) + (1 if current is not None and current.is_alive() else 0)

    # -- both --------------------------------------------------------------
    def _wake_accept(self):
        if not self.bind_address:
            return
        try:
            dll, _ = self._dll()
            handle = dll.CreateFileW(self.bind_address, GENERIC_READ | GENERIC_WRITE,
                                     0, None, OPEN_EXISTING, 0, None)
            if handle != INVALID_HANDLE_VALUE:
                dll.CloseHandle(handle)
        except Exception:
            pass

    def stop(self):
        with self._publisher_lifecycle_lock:
            with self._sub_handles_lock:
                self._running = False
                self._publisher_stop.set()
                connections = list(self._sub_handles.values())
                if self._accepting is not None:
                    connections.append(self._accepting)
                for connection in connections:
                    connection.closing.set()
            self._stop_subscriber()
            if connections:
                self._wake_accept()
            for connection in connections:
                self._drop_handle(connection)
            thread = self._accept_thread
            if thread is not None and thread is not threading.current_thread():
                # 一秒后线程仍活着不等于停止；新模型必须等旧 accept 真正退出。
                while thread.is_alive():
                    self._wake_accept()
                    thread.join(_CONNECT_POLL_SECONDS)
            self._accept_thread = None
