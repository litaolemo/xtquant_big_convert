"""ZeroMQ transport for the BigQMT RPC bridge.

Designed for same-host low latency. Topology:

* **Server** binds a ``ROUTER`` socket. Each inbound message arrives as
  ``[identity, payload]``; the server remembers ``identity`` keyed by
  ``request_id`` and replies with ``[identity, payload]`` so ZMQ routes the
  response back to the originating client automatically.
* **Client** connects a ``DEALER`` socket (with a unique random identity), sends
  ``[payload]``, then ``poll``/``recv`` for the response. DEALER gives each
  client an asymmetric async path that pairs naturally with ROUTER.

Wire framing is a single JSON payload per message. The original b64 stock-code
obfuscation (``encode_rpc_request_payload``) is applied too, so payloads stay
opaque even though ZMQ does not need it — keeps the wire uniform with Redis.

Two threads on the server: the ROUTER recv loop, and a per-client is implicit
(ZMQ handles multiplexing). One thread on the client for recv is avoided by
using DEALER + ``poll`` (synchronous request/response fits the RPC model).
"""

import json
import queue
import threading
import time
import uuid

from ..adapters.redis_common import decode_text
from ..redis_rpc import (
    decode_rpc_request_payload,
    encode_rpc_request_payload,
)
from .base import RpcTransport, TransportError, TransportTimeout


# ZMQ does not support ipc:// on Windows (it trips a signaler abort), so the
# default endpoint is tcp loopback. The port is derived from the account_id so
# distinct accounts don't collide on the same port; override via config when
# needed. Base 15560 keeps it clear of common dev ports.
DEFAULT_ZMQ_HOST = "127.0.0.1"
DEFAULT_ZMQ_BASE_PORT = 15560
DEFAULT_ZMQ_PORT_RANGE = 100  # derived port = base + (account_id_int mod range)


def _default_zmq_port(account_id):
    """Derive a stable port from account_id so each account gets its own socket."""
    text = str(account_id or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    try:
        offset = int(digits) % DEFAULT_ZMQ_PORT_RANGE if digits else 0
    except ValueError:
        offset = 0
    return DEFAULT_ZMQ_BASE_PORT + offset


def _default_zmq_address(account_id, host=None):
    host = host or DEFAULT_ZMQ_HOST
    return "tcp://%s:%d" % (host, _default_zmq_port(account_id))


def _loads(raw):
    if isinstance(raw, dict):
        return dict(raw)
    text = decode_text(raw)
    text = decode_rpc_request_payload(text)
    return json.loads(text)


class ZmqTransport(RpcTransport):
    """ZMQ ROUTER/DEALER transport.

    The same instance plays both roles depending on method called:
    ``send_request`` acts as a client (DEALER connect), ``start_receiving`` +
    ``send_response`` act as a server (ROUTER bind). A deployment normally uses
    one instance per role (the QMT process is the server; the external client
    is the client).
    """

    name = "zmq"

    def __init__(
        self,
        bind_address=None,
        connect_address=None,
        host=None,
        port=None,
        account_id="",
        print_prefix="[bigqmt_rpc]",
        io_threads=1,
        recv_timeout_seconds=1.0,
        server_hwm=10000,
        client_linger_ms=0,
        discovery_redis_client=None,
        discovery_key_template="bigqmt:zmq:addr:{account_id}",
        discovery_ttl_seconds=300,
        port_scan_range=50,
    ):
        super(ZmqTransport, self).__init__(account_id=account_id, print_prefix=print_prefix)
        # Address resolution order: explicit bind_address/connect_address win;
        # otherwise build tcp://host:port from host/port (port defaults to a
        # value derived from account_id so distinct accounts don't collide).
        resolved_host = host or DEFAULT_ZMQ_HOST
        if port is not None:
            resolved_port = int(port)
        else:
            resolved_port = _default_zmq_port(account_id)
        default_addr = "tcp://%s:%d" % (resolved_host, resolved_port)
        self.bind_address = bind_address or default_addr
        self.connect_address = connect_address
        self.bind_host = resolved_host
        self.base_port = resolved_port
        self.io_threads = int(io_threads)
        self.recv_timeout_seconds = float(recv_timeout_seconds)
        self.server_hwm = int(server_hwm)
        self.client_linger_ms = int(client_linger_ms)
        # Service discovery: when the derived port is taken, the server scans
        # upward for a free port and publishes the real address to Redis so the
        # client can find it. Without a discovery client this falls back to the
        # static derived address (and bind conflicts surface as errors).
        self.discovery_redis_client = discovery_redis_client
        self.discovery_key_template = discovery_key_template
        self.discovery_ttl_seconds = int(discovery_ttl_seconds)
        self.port_scan_range = int(port_scan_range)

        self._zmq = None  # imported lazily
        self._ctx = None
        # server state
        self._router = None
        self._router_thread = None
        self._actual_bind_address = None  # set after start_receiving()
        self._pending_identities = {}  # request_id -> client identity bytes
        self._identity_lock = threading.Lock()
        self._response_queue = queue.Queue()
        self._queued_response_count = 0
        self._sent_response_count = 0
        # client state
        self._dealer = None
        self._client_lock = threading.Lock()

    # -- construction helper ----------------------------------------------
    @classmethod
    def from_config(cls, config, account_id="", print_prefix="[bigqmt_rpc]"):
        config = dict(config or {})
        return cls(
            bind_address=config.get("bind_address"),
            connect_address=config.get("connect_address"),
            host=config.get("host"),
            port=config.get("port"),
            account_id=config.get("account_id", account_id),
            print_prefix=print_prefix,
            io_threads=int(config.get("io_threads", 1)),
            recv_timeout_seconds=float(config.get("recv_timeout_seconds", 1.0)),
            server_hwm=int(config.get("server_hwm", 10000)),
            client_linger_ms=int(config.get("client_linger_ms", 0)),
            discovery_redis_client=config.get("discovery_redis_client"),
            discovery_key_template=config.get(
                "discovery_key_template", "bigqmt:zmq:addr:{account_id}"
            ),
            discovery_ttl_seconds=int(config.get("discovery_ttl_seconds", 300)),
            port_scan_range=int(config.get("port_scan_range", 50)),
        )

    # -- shared zmq context -----------------------------------------------
    def _ensure_zmq(self):
        if self._zmq is None:
            try:
                import zmq  # noqa: F401
            except ImportError as exc:  # pragma: no cover - depends on env
                raise TransportError(
                    "pyzmq is required for the zmq transport: %s" % exc
                )
            self._zmq = zmq
        if self._ctx is None:
            self._ctx = self._zmq.Context.instance(self.io_threads)
        return self._zmq, self._ctx

    # -- server side ------------------------------------------------------
    def _bind_with_fallback(self):
        """Bind the ROUTER socket, scanning ports if the default is taken.

        Tries ``self.bind_address`` first. On ZMQ EADDRINUSE, walks upward
        from the base port for up to ``port_scan_range`` ports. The actually
        bound address is recorded on ``self._actual_bind_address`` and, when a
        discovery client is configured, published to Redis so clients can find
        it. Re-raises the original error if no port in the range is free.
        """
        zmq, ctx = self._ensure_zmq()
        attempts = [self.bind_address]
        # Build fallback ports from the base port upward. We always scan so a
        # collision (e.g. a stale server on the derived port) is recovered
        # automatically; an explicit bind_address just sets the scan origin.
        try:
            base = int(self.bind_address.rsplit(":", 1)[1])
            host_part = self.bind_address.rsplit(":", 1)[0]
            for offset in range(1, self.port_scan_range + 1):
                attempts.append("%s:%d" % (host_part, base + offset))
        except (ValueError, IndexError):
            pass

        last_error = None
        for addr in attempts:
            sock = ctx.socket(zmq.ROUTER)
            sock.setsockopt(zmq.RCVHWM, self.server_hwm)
            sock.setsockopt(zmq.SNDHWM, self.server_hwm)
            sock.setsockopt(zmq.RCVTIMEO, int(self.recv_timeout_seconds * 1000))
            try:
                sock.bind(addr)
                self._router = sock
                self._actual_bind_address = addr
                self._publish_discovery(addr)
                return
            except self._zmq.ZMQError as exc:
                last_error = exc
                try:
                    sock.close(linger=0)
                except Exception:
                    pass
                # Only EADDRINUSE is retriable; other errors (e.g. bad host) abort.
                if getattr(exc, "errno", None) != zmq.EADDRINUSE:
                    raise
        # Exhausted the scan range.
        raise last_error

    def _publish_discovery(self, address):
        if self.discovery_redis_client is None:
            return
        key = self.discovery_key_template.format(account_id=self.account_id)
        try:
            self.discovery_redis_client.setex(
                key, self.discovery_ttl_seconds, address
            )
        except Exception as exc:
            print("%s zmq discovery publish failed: %s" % (self.print_prefix, exc))

    def _clear_discovery(self):
        if self.discovery_redis_client is None:
            return
        key = self.discovery_key_template.format(account_id=self.account_id)
        try:
            self.discovery_redis_client.delete(key)
        except Exception:
            pass

    def start_receiving(self, on_request, background_threads=True):
        super(ZmqTransport, self).start_receiving(on_request)
        zmq, ctx = self._ensure_zmq()
        self._bind_with_fallback()
        bound = self._actual_bind_address or self.bind_address
        if not background_threads:
            print(
                "%s zmq bound=%s background_threads=False"
                % (self.print_prefix, bound)
            )
            return
        self._router_thread = threading.Thread(
            target=self._router_loop, name="bigqmt-zmq-rpc", daemon=True
        )
        self._router_thread.start()
        print(
            "%s zmq started bound=%s" % (self.print_prefix, self.bind_address)
        )

    def _router_loop(self):
        zmq = self._zmq
        try:
            while self._running:
                self._drain_response_queue()
                try:
                    frames = self._router.recv_multipart()
                except self._zmq.Again:
                    continue
                except Exception as exc:
                    if not self._running:
                        break
                    print("%s zmq recv failed: %s" % (self.print_prefix, exc))
                    time.sleep(0.5)
                    continue
                if len(frames) < 2:
                    continue
                identity, payload = frames[0], frames[-1]
                try:
                    request = _loads(payload)
                except Exception as exc:
                    print("%s zmq decode failed: %s" % (self.print_prefix, exc))
                    continue
                request_id = str(request.get("request_id") or uuid.uuid4().hex)
                with self._identity_lock:
                    self._pending_identities[request_id] = identity
                t0 = time.perf_counter()
                try:
                    self.deliver(request)
                except Exception as exc:
                    print("%s zmq deliver failed: %s" % (self.print_prefix, exc))
                handler_ms = (time.perf_counter() - t0) * 1000.0
                if handler_ms > 50.0:
                    # Distinguishes a slow handler (real work) from a GIL stall
                    # (which the gil_probe catches): if handler_ms is small but pings
                    # still spike, the stall is elsewhere in the process.
                    print("%s zmq slow handler method=%s %.0fms"
                          % (self.print_prefix, request.get("method"), handler_ms))
        finally:
            # Close the ROUTER socket on the thread that owns it. On Windows,
            # closing a ZMQ socket from a different thread trips a signaler
            # assertion (abort); closing it here is safe because this thread
            # created and exclusively used it.
            try:
                self._router.close(linger=0)
            except Exception:
                pass
            self._router = None

    def _drain_response_queue(self):
        while True:
            try:
                identity, payload = self._response_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._router.send_multipart([identity, payload])
                self._sent_response_count += 1
                if self._sent_response_count <= 5:
                    print("%s zmq queued response sent" % self.print_prefix)
            except Exception as exc:
                print("%s zmq send failed: %s" % (self.print_prefix, exc))

    def send_response(self, request, response):
        if self._router is None:
            raise TransportError("zmq server socket is not bound")
        request_id = str(
            response.get("request_id") or request.get("request_id") or ""
        )
        with self._identity_lock:
            identity = self._pending_identities.pop(request_id, None)
        if identity is None:
            # No matching peer — drop silently (client may have gone away).
            return
        payload = encode_rpc_request_payload(response).encode("utf-8")
        if threading.current_thread() is not self._router_thread:
            self._queued_response_count += 1
            if self._queued_response_count <= 5:
                print("%s zmq response queued for router thread" % self.print_prefix)
            self._response_queue.put((identity, payload))
            return
        try:
            self._router.send_multipart([identity, payload])
        except Exception as exc:
            print("%s zmq send failed: %s" % (self.print_prefix, exc))

    def drain_request_queue(self, max_items=20):
        """No-op: the ROUTER thread delivers requests itself. Defining this makes
        the RPC service's per-tick drain skip the Redis LPOP fallback, so the QMT
        strategy thread does NOT do a pointless cross-LAN Redis round-trip (which
        holds the GIL and can stall this transport thread) on every adjust tick."""
        return 0

    # -- client side ------------------------------------------------------
    def _resolve_connect_address(self):
        """Resolve the address to connect to.

        Order: explicit connect_address > discovery lookup > default derived.
        Discovery lets the client find a server that had to move off the
        default port because of a collision.
        """
        if self.connect_address:
            return self.connect_address
        discovered = self._lookup_discovery()
        if discovered:
            return discovered
        return _default_zmq_address(self.account_id)

    def _lookup_discovery(self):
        if self.discovery_redis_client is None:
            return None
        key = self.discovery_key_template.format(account_id=self.account_id)
        try:
            raw = self.discovery_redis_client.get(key)
        except Exception:
            return None
        if not raw:
            return None
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:
            return None
        return text or None

    def _ensure_dealer(self):
        zmq, ctx = self._ensure_zmq()
        if self._dealer is None:
            address = self._resolve_connect_address()
            sock = ctx.socket(zmq.DEALER)
            # Unique identity so ROUTER can route replies back to us.
            sock.setsockopt(zmq.IDENTITY, uuid.uuid4().hex.encode("utf-8")[:16])
            sock.setsockopt(zmq.LINGER, self.client_linger_ms)
            sock.connect(address)
            self._dealer = sock
            self.connect_address = address
        return self._dealer

    def send_request(self, request, timeout_seconds, **_kwargs):
        zmq = self._zmq or self._ensure_zmq()[0]
        with self._client_lock:
            dealer = self._ensure_dealer()
            request = dict(request)
            request.setdefault("request_id", uuid.uuid4().hex)
            request_id = request["request_id"]
            payload = encode_rpc_request_payload(request)
            try:
                dealer.send(payload.encode("utf-8"))
            except Exception as exc:
                raise TransportError("zmq send failed: %s" % exc)
            deadline = time.time() + float(timeout_seconds)
            poller = self._zmq.Poller()
            poller.register(dealer, self._zmq.POLLIN)
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                events = dict(poller.poll(timeout=int(remaining * 1000)))
                if dealer in events:
                    frames = dealer.recv_multipart()
                    raw = frames[-1]
                    response = _loads(raw)
                    if response.get("request_id") == request_id:
                        return response
            raise TransportTimeout("zmq rpc timeout: %s" % request.get("method"))

    # -- lifecycle --------------------------------------------------------
    def stop(self):
        super(ZmqTransport, self).stop()
        # Clear _running so the router loop exits; the loop closes its own
        # socket (closing cross-thread trips a Windows signaler abort).
        if self._router_thread is not None and self._router_thread.is_alive():
            self._router_thread.join(2.0)
        self._router_thread = None
        # If we were a server that published a discovery address, clear it so
        # clients don't keep hitting a dead endpoint.
        if self._actual_bind_address is not None:
            self._clear_discovery()
            self._actual_bind_address = None
        with self._client_lock:
            if self._dealer is not None:
                try:
                    self._dealer.close(linger=self.client_linger_ms)
                except Exception:
                    pass
                self._dealer = None
        # Do NOT terminate the shared context — other sockets/users may rely on it.
