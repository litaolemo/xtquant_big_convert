# coding: utf-8
"""Shared-memory (mmap ring) transport -- same host, zero third-party deps.

Why this exists
---------------
The QMT runtime is broker-hosted: the broker can remove or block third-party
packages (``redis``, ``pyzmq``) at the import layer, and its bundled Python
cannot ``pip install``. A transport built only from the standard library is
the one wire that survives that. Shared memory is that wire: ``mmap`` and
``struct`` are both stdlib and neither is a socket.

Two regions per account
-----------------------
::

    {prefix}bigqmt_shm_{account}_req     client writes, server reads
    {prefix}bigqmt_shm_{account}_resp    server writes, client reads

Each region is a 64-byte header followed by ``slots`` fixed-size slots of
``48 + slot_size`` bytes. One direction per region means each is a strict
single-producer / single-consumer ring, so no mutex is needed: the producer
only ever writes a slot the consumer has already released, and a per-slot
token proves the payload is complete before it is read.

No background threads on the QMT side
-------------------------------------
Measured on the live terminal (2026-10-25): a ``threading.Thread`` started
from a QMT strategy ``init`` is almost never scheduled -- a PING written at
12:44:58 was not processed until the strategy was stopped at 12:57:10, five
loop iterations in fifty minutes. A blocking ``WaitForSingleObject`` receive
thread (the shape the original design doc assumed) therefore does not work.

This transport implements :meth:`drain_request_queue` instead, polled by the
strategy's ``run_time`` timer through ``adjust`` -- the same non-blocking
drain shape ``pipe_transport`` uses. Measured with that timer: 200/200 round
trips at p50 2.0 ms, p95 3.0 ms, 687 timer ticks/s at a 1 ms interval, at
2.2% of one core.

``background_threads=True`` still works (it starts a receive thread) for
supervisors that are not QMT; the QMT path is the drain.

Namespace
---------
``Global\\`` is the default and is a hard requirement, not a preference: the
QMT strategy host runs in an interactive session while the bridge runs in
session 0, and ``Local\\`` mappings are per-session. Verified cross-session
on the production machine.

Python 3.6.8 is the floor (the QMT-side interpreter): no ``time.time_ns()``,
no ``dataclasses``, no ``functools.cached_property``.
"""

import hashlib
import json
import mmap
import os
import struct
import sys
import threading
import time

from .base import RpcTransport, TransportError, TransportTimeout


MAGIC = b"BQSHM\0\0\0"
VERSION = 1

# Header (64 bytes, little endian):
#   magic 8s | version I | slot_size I | slots I | flags I
#   | write_seq Q | read_seq Q | peer_pid I | 4x | boot_id Q | 8x
HEADER = struct.Struct("<8sIIIIQQI4xQ8x")
HEADER_SIZE = HEADER.size

# Slot header (48 bytes): payload length, request id, token.
# The token is written LAST by the producer and checked FIRST by the consumer:
# a slot whose token does not match its index is still being written (or was
# never written) and must not be read.
SLOT_HEADER = struct.Struct("<I40sI")
SLOT_HEADER_SIZE = SLOT_HEADER.size

DEFAULT_SLOT_SIZE = 1 << 20      # 1 MiB: whole-market quote frames are large
DEFAULT_SLOTS = 8
DEFAULT_PREFIX = "Global\\"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
# Sleep between response polls. The QMT-side timer sets the floor (1 ms
# interval measured 2.0 ms median), so polling faster buys nothing.
DEFAULT_POLL_INTERVAL_SECONDS = 0.0005
ALLOCATION_GRANULARITY = 64 * 1024

_IS_WINDOWS = os.name == "nt"
_MASK32 = 0xFFFFFFFF


def _region_size(slot_size, slots):
    raw = HEADER_SIZE + int(slots) * (SLOT_HEADER_SIZE + int(slot_size))
    # Windows rounds a mapping up to the allocation granularity; spell the
    # size out so both ends agree on the stride arithmetic.
    if raw % ALLOCATION_GRANULARITY:
        raw += ALLOCATION_GRANULARITY - (raw % ALLOCATION_GRANULARITY)
    return raw


def _boot_id():
    """Per-process start marker. ``time.time_ns()`` is 3.7+, so compose it."""
    return int(time.time() * 1e6) ^ (os.getpid() << 20)


def _platform_name(name):
    """POSIX shared-memory names cannot contain a backslash."""
    if _IS_WINDOWS:
        return name
    return name.replace("Global\\", "").replace("\\", "_")


def _request_id_text(value):
    """Reply routing id as text. Hex when the caller's id cannot be a 40-byte
    ASCII field, so an overlong or non-ASCII id is still stable and unique."""
    text = str(value or "")
    raw = text.encode("ascii", "ignore")
    if not raw or len(raw) > 40:
        return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:40]
    return text


def _encode_request_id(value):
    """40-byte ASCII slot id, NUL padded. Callers compare the *text* form."""
    raw = _request_id_text(value).encode("ascii")
    return raw + b"\0" * (40 - len(raw))


def _mapping_exists(name):
    """Did somebody already create this named mapping?

    ``mmap.mmap(-1, size, tagname=name)`` creates-or-opens, so it cannot tell
    a live server from a typo. The client must never create: two ends each
    creating their own region look exactly like "every request times out".
    """
    if not _IS_WINDOWS:
        try:
            from multiprocessing import shared_memory

            shm = shared_memory.SharedMemory(name=_platform_name(name), create=False)
        except (FileNotFoundError, OSError):
            return False
        try:
            shm.close()
        except Exception:
            pass
        return True
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenFileMappingW(0x0004, False, name)   # FILE_MAP_READ
    if not handle:
        return False
    k32.CloseHandle(handle)
    return True


def _read_existing_header(name):
    """Read a live Windows mapping without creating or resizing it.

    ``mmap(-1, new_size, tagname=...)`` raises WinError 5 when an existing
    mapping has a different size. Reading the fixed header first lets a layout
    change fail with the actual old/new values instead of that OS error.
    """
    if not _IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.MapViewOfFile.restype = wintypes.LPVOID
    k32.MapViewOfFile.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_size_t,
    ]
    k32.UnmapViewOfFile.argtypes = [wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenFileMappingW(0x0004, False, name)   # FILE_MAP_READ
    if not handle:
        return None
    try:
        address = k32.MapViewOfFile(handle, 0x0004, 0, 0, HEADER_SIZE)
        if not address:
            raise TransportError("cannot inspect shared-memory region %r" % name)
        try:
            return HEADER.unpack(ctypes.string_at(address, HEADER_SIZE))
        finally:
            k32.UnmapViewOfFile(address)
    finally:
        k32.CloseHandle(handle)


class _Mapping(object):
    """One named region. Windows uses ``mmap``; POSIX uses shared_memory."""

    def __init__(self, name, size, create):
        self.name = name
        self.size = int(size)
        self._owner = bool(create)
        self._shm = None
        self.buf = None
        if _IS_WINDOWS:
            if not create and not _mapping_exists(name):
                raise TransportError(
                    "shared-memory region %r does not exist (is the QMT-side "
                    "strategy running with transport=shm?)" % name)
            self.buf = mmap.mmap(-1, self.size, tagname=name)
        else:
            from multiprocessing import shared_memory

            try:
                self._shm = shared_memory.SharedMemory(
                    name=_platform_name(name), create=create, size=self.size)
            except FileExistsError:
                # A region left by a previous server instance. The caller
                # inspects the header before deciding whether to reuse it.
                self._shm = shared_memory.SharedMemory(
                    name=_platform_name(name), create=False)
            except FileNotFoundError:
                raise TransportError(
                    "shared-memory region %r does not exist (is the QMT-side "
                    "strategy running with transport=shm?)" % name)
            self.buf = self._shm.buf

    def close(self):
        try:
            if _IS_WINDOWS:
                if self.buf is not None:
                    self.buf.close()
            else:
                if self._shm is not None:
                    self._shm.close()
                    if self._owner:
                        try:
                            self._shm.unlink()
                        except (FileNotFoundError, OSError):
                            pass
        except Exception:
            pass
        finally:
            self.buf = None


class _Ring(object):
    """The protocol on top of one region. Producer/consumer role is fixed."""

    def __init__(self, mapping, slot_size, slots):
        self.mapping = mapping
        self.buf = mapping.buf
        self.slot_size = int(slot_size)
        self.slots = int(slots)
        self.stride = SLOT_HEADER_SIZE + self.slot_size

    # -- header ------------------------------------------------------------
    def read_header(self):
        if self.buf is None:
            raise TransportError("shared-memory region was closed")
        return HEADER.unpack(bytes(self.buf[0:HEADER_SIZE]))

    def write_header(self, slot_size, slots, flags, write_seq, read_seq,
                     peer_pid, boot_id):
        # One slice assignment: a peer never sees a half-written header,
        # because the whole 64 bytes go in a single store.
        self.buf[0:HEADER_SIZE] = HEADER.pack(
            MAGIC, VERSION, int(slot_size), int(slots), int(flags),
            int(write_seq), int(read_seq), int(peer_pid), int(boot_id))

    def seqs(self):
        values = self.read_header()
        return values[5], values[6]

    def set_write_seq(self, value):
        self.buf[24:32] = struct.pack("<Q", int(value))

    def set_read_seq(self, value):
        self.buf[32:40] = struct.pack("<Q", int(value))

    # -- slots -------------------------------------------------------------
    def _offset(self, index):
        return HEADER_SIZE + (int(index) % self.slots) * self.stride

    def write_slot(self, index, request_id, payload):
        base = self._offset(index)
        token = int(index) & _MASK32
        # Invalidate first, then the header+payload, token last.
        self.buf[base + 44:base + 48] = struct.pack("<I", 0)
        SLOT_HEADER.pack_into(self.buf, base, len(payload), request_id, 0)
        self.buf[base + SLOT_HEADER_SIZE:base + SLOT_HEADER_SIZE + len(payload)] = payload
        self.buf[base + 44:base + 48] = struct.pack("<I", token)

    def read_slot(self, index):
        """Return ``(request_id, payload)`` or ``None`` when not complete."""
        if self.buf is None:
            return None
        base = self._offset(index)
        token = struct.unpack("<I", bytes(self.buf[base + 44:base + 48]))[0]
        if token != (int(index) & _MASK32):
            return None
        length, request_id, unused_token = SLOT_HEADER.unpack(
            bytes(self.buf[base:base + SLOT_HEADER_SIZE]))
        if length <= 0 or length > self.slot_size:
            return None
        start = base + SLOT_HEADER_SIZE
        payload = bytes(self.buf[start:start + length])
        request_id = request_id.split(b"\0", 1)[0].decode("ascii", "replace")
        return request_id, payload


def _encode_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class SharedMemoryTransport(RpcTransport):
    """Request/response over a pair of named memory regions.

    Client side: :meth:`send_request`. Server side: :meth:`start_receiving`
    plus :meth:`drain_request_queue` on the strategy's timer, or a receive
    thread when ``background_threads=True``.
    """

    name = "shm"

    def __init__(self, account_id="", print_prefix="[bigqmt_rpc]",
                 slot_size=None, slots=None, name_prefix=None,
                 connect_timeout_seconds=None, poll_interval_seconds=None,
                 **kwargs):
        super(SharedMemoryTransport, self).__init__(
            account_id=account_id, print_prefix=print_prefix)
        self.slot_size = int(slot_size or DEFAULT_SLOT_SIZE)
        self.slots = max(2, int(slots or DEFAULT_SLOTS))
        self.name_prefix = DEFAULT_PREFIX if name_prefix is None else str(name_prefix)
        self.connect_timeout_seconds = float(
            connect_timeout_seconds or DEFAULT_CONNECT_TIMEOUT_SECONDS)
        self.poll_interval_seconds = max(
            0.0, float(poll_interval_seconds if poll_interval_seconds is not None
                       else DEFAULT_POLL_INTERVAL_SECONDS))
        self.size = _region_size(self.slot_size, self.slots)

        suffix = str(self.account_id or "default")
        self.req_name = "%sbigqmt_shm_%s_req" % (self.name_prefix, suffix)
        self.resp_name = "%sbigqmt_shm_%s_resp" % (self.name_prefix, suffix)

        self._server = False
        self._req = None          # request ring (client writes)
        self._resp = None         # response ring (server writes)
        self._send_lock = threading.Lock()
        self._receive_thread = None
        self._background_threads = False
        self._drained_anything = False
        self._abandoned = set()
        self._processed_count = 0
        self._published_count = 0
        self._boot = _boot_id()

    # -- lifecycle ---------------------------------------------------------
    def _open_server_side(self):
        """Create (or reset) both regions. Only the server does this."""
        if self._req is not None and self._resp is not None:
            return
        for name in (self.req_name, self.resp_name):
            previous = _read_existing_header(name)
            if (previous is not None and previous[0] == MAGIC
                    and (int(previous[2]) != self.slot_size
                         or int(previous[3]) != self.slots)):
                raise TransportError(
                    "shared-memory region %r already exists with slot_size=%d "
                    "slots=%d, this build wants slot_size=%d slots=%d -- stop every "
                    "holder and change shm.name_prefix to migrate"
                    % (name, int(previous[2]), int(previous[3]),
                       self.slot_size, self.slots))
        req_map = _Mapping(self.req_name, self.size, create=True)
        try:
            resp_map = _Mapping(self.resp_name, self.size, create=True)
        except Exception:
            req_map.close()
            raise
        self._req = _Ring(req_map, self.slot_size, self.slots)
        self._resp = _Ring(resp_map, self.slot_size, self.slots)
        # A region left over from a previous instance is normally reusable:
        # Windows keeps the mapping alive only while a handle is open, and a
        # strategy restart re-creates it. But if the new config asks for a
        # different slot layout, the physical region is still the old size --
        # writing the new stride into it would corrupt every slot after the
        # first. Refuse loudly instead of silently mis-indexing.
        previous = self._req.read_header()
        if previous[0] == MAGIC and (int(previous[2]) != self.slot_size
                                     or int(previous[3]) != self.slots):
            for ring in (self._req, self._resp):
                ring.mapping.close()
            self._req = None
            self._resp = None
            raise TransportError(
                "shared-memory region %r already exists with slot_size=%d "
                "slots=%d, this build wants slot_size=%d slots=%d -- stop every "
                "holder and change shm.name_prefix to migrate"
                % (self.req_name, int(previous[2]), int(previous[3]),
                   self.slot_size, self.slots))
        # The server owns both rings, and resetting the counters
        # unconditionally is what makes a strategy restart safe: a region left
        # over from the previous instance may still be mapped by a client, and
        # its stale counters must not be read as unread requests from that run.
        for ring in (self._req, self._resp):
            ring.write_header(self.slot_size, self.slots, 0, 0, 0,
                              os.getpid(), self._boot)
        self._server = True
        print("%s shm regions ready req=%s resp=%s slot_size=%d slots=%d size=%d"
              % (self.print_prefix, self.req_name, self.resp_name,
                 self.slot_size, self.slots, self.size))

    def _open_client_side(self):
        if self._req is not None and self._resp is not None:
            return
        deadline = time.time() + self.connect_timeout_seconds
        while True:
            try:
                req_map = _Mapping(self.req_name, self.size, create=False)
            except TransportError as exc:
                if time.time() >= deadline:
                    raise TransportError(
                        "%s (waited %.1fs for %r)" %
                        (exc, self.connect_timeout_seconds, self.req_name))
                time.sleep(0.02)
                continue
            try:
                resp_map = _Mapping(self.resp_name, self.size, create=False)
            except TransportError as exc:
                req_map.close()
                if time.time() >= deadline:
                    raise
                time.sleep(0.02)
                continue
            req = _Ring(req_map, self.slot_size, self.slots)
            resp = _Ring(resp_map, self.slot_size, self.slots)
            header = req.read_header()
            if header[0] != MAGIC or header[1] != VERSION:
                req_map.close()
                resp_map.close()
                message = TransportError(
                    "region %r has a foreign layout (magic=%r version=%r); "
                    "the peer is running an incompatible build"
                    % (self.req_name, header[0], header[1]))
                if time.time() >= deadline:
                    raise message
                time.sleep(0.02)
                continue
            server_slot_size = int(header[2])
            server_slots = int(header[3])
            if (server_slot_size != self.slot_size
                    or server_slots != self.slots):
                req_map.close()
                resp_map.close()
                raise TransportError(
                    "slot layout mismatch: client slot_size=%d slots=%d, "
                    "server slot_size=%d slots=%d -- set the same "
                    "shm.slot_size/shm.slots on both ends"
                    % (self.slot_size, self.slots, server_slot_size, server_slots))
            self._req = req
            self._resp = resp
            print("%s shm attached req=%s resp=%s slot_size=%d slots=%d"
                  % (self.print_prefix, self.req_name, self.resp_name,
                     self.slot_size, self.slots))
            return

    def close(self):
        for ring in (self._req, self._resp):
            if ring is not None:
                ring.mapping.close()
        self._req = None
        self._resp = None

    def stop(self):
        super(SharedMemoryTransport, self).stop()
        thread = self._receive_thread
        self._receive_thread = None
        if thread is not None and thread.is_alive():
            thread.join(1.0)
        self.close()

    # -- server side -------------------------------------------------------
    def start_receiving(self, on_request, **kwargs):
        super(SharedMemoryTransport, self).start_receiving(on_request)
        background = kwargs.get("background_threads")
        self._background_threads = True if background is None else bool(background)
        self._open_server_side()
        if not self._background_threads:
            # The QMT path: the strategy's run_time timer calls
            # drain_request_queue from adjust. Starting a thread here would be
            # strictly worse -- measured, QMT does not schedule it.
            print("%s shm drain mode: no receive thread, polled from adjust"
                  % self.print_prefix)
            return
        thread = threading.Thread(target=self._receive_loop, name="bigqmt-shm-rpc")
        thread.daemon = True
        self._receive_thread = thread
        thread.start()

    def _receive_loop(self):
        while self._running:
            try:
                processed = self.drain_request_queue(max_items=20)
            except Exception as exc:
                print("%s shm receive loop error: %s" % (self.print_prefix, exc))
                time.sleep(0.05)
                continue
            if not processed:
                time.sleep(0.002)

    def drain_request_queue(self, max_items=20):
        """Non-blocking: read what is queued, run the handler, write the reply.

        Called on the strategy's adjust thread. Read, handler and reply all
        happen on that one thread, so there is no cross-thread handoff to pay
        a GIL tick for.
        """
        if self._req is None:
            return 0
        processed = 0
        while processed < int(max_items):
            write_seq, read_seq = self._req.seqs()
            if read_seq >= write_seq:
                break
            item = self._req.read_slot(read_seq)
            if item is None:
                # The producer has not finished the payload; it bumps
                # write_seq only after the token is in, so retry next tick.
                break
            request_id, payload = item
            self._req.set_read_seq(read_seq + 1)
            try:
                request = json.loads(_encode_text(payload))
            except Exception as exc:
                print("%s shm dropped undecodable request (%s)"
                      % (self.print_prefix, exc))
                continue
            if not isinstance(request, dict):
                print("%s shm dropped non-object request" % self.print_prefix)
                continue
            request.setdefault("request_id", request_id)
            # Reply routing: send_response reads this back instead of any
            # redis-style reply key.
            request["_shm_request_id"] = request_id
            try:
                self.deliver(request)
            except Exception as exc:
                print("%s shm deliver failed: %s" % (self.print_prefix, exc))
            processed += 1
        self._processed_count += processed
        return processed

    def send_response(self, request, response):
        if self._resp is None:
            raise TransportError("shm response ring is not open (server side only)")
        request_id = _encode_request_id(
            (request or {}).get("_shm_request_id")
            or (request or {}).get("request_id"))
        payload = json.dumps(response, ensure_ascii=False, default=str).encode("utf-8")
        if len(payload) > self.slot_size:
            raise TransportError(
                "shm response for method=%s is %d bytes, slot_size is %d -- "
                "raise shm.slot_size" %
                ((response or {}).get("method"), len(payload), self.slot_size))
        write_seq, read_seq = self._resp.seqs()
        if write_seq - read_seq >= self.slots:
            # All slots unread means the client is gone, not slow.
            raise TransportError(
                "shm response ring full (%d slots); client is not reading"
                % self.slots)
        self._resp.write_slot(write_seq, request_id, payload)
        self._resp.set_write_seq(write_seq + 1)
        self._published_count += 1

    # -- client side -------------------------------------------------------
    def send_request(self, request, timeout_seconds):
        payload = json.dumps(request, ensure_ascii=False, default=str).encode("utf-8")
        if len(payload) > self.slot_size:
            raise TransportError(
                "shm request for method=%s is %d bytes, slot_size is %d -- "
                "raise shm.slot_size" %
                ((request or {}).get("method"), len(payload), self.slot_size))
        request_id = _encode_request_id((request or {}).get("request_id"))
        request_text = _request_id_text((request or {}).get("request_id"))
        timeout = float(timeout_seconds or 0.0)
        # One in-flight request at a time: a single response ring with no
        # correlation window cannot pair two of them.
        with self._send_lock:
            if self._req is None:
                self._open_client_side()
            write_seq, read_seq = self._req.seqs()
            if write_seq - read_seq >= self.slots:
                raise TransportError(
                    "shm request ring full (%d slots); the server is not "
                    "draining (is the strategy's adjust timer running?)"
                    % self.slots)
            self._req.write_slot(write_seq, request_id, payload)
            self._req.set_write_seq(write_seq + 1)
            deadline = time.time() + timeout if timeout > 0 else None
            return self._await_response(request_text, deadline)

    def _await_response(self, request_text, deadline):
        poll = self.poll_interval_seconds
        while True:
            if self._resp is None:
                raise TransportError("shm response ring closed while waiting")
            write_seq, read_seq = self._resp.seqs()
            while read_seq < write_seq:
                item = self._resp.read_slot(read_seq)
                if item is None:
                    break
                got_id, payload = item
                self._resp.set_read_seq(read_seq + 1)
                read_seq += 1
                if got_id in self._abandoned:
                    self._abandoned.discard(got_id)
                    continue
                if got_id != request_text:
                    # A reply to a request that already timed out (#195 class).
                    continue
                return json.loads(_encode_text(payload))
            if deadline is not None and time.time() >= deadline:
                self._abandoned.add(request_text)
                if len(self._abandoned) > 256:
                    self._abandoned.clear()
                raise TransportTimeout(
                    "shared-memory rpc timeout (request_id=%s)" % request_text)
            time.sleep(poll)

    def __repr__(self):
        return "<SharedMemoryTransport account_id=%r req=%r>" % (
            self.account_id, self.req_name)
