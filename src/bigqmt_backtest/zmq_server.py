"""Minimal REQ/REP ZMQ server for one isolated backtest run."""

import json
import threading


class ZmqBacktestServer(object):
    def __init__(self, protocol, endpoint="tcp://127.0.0.1:16661", exit_on_finish=False, poll_ms=100):
        self.protocol = protocol
        self.endpoint = str(endpoint)
        self.exit_on_finish = bool(exit_on_finish)
        self.poll_ms = int(poll_ms)
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self.actual_endpoint = None

    def wait_until_ready(self, timeout_seconds=None):
        return self._ready_event.wait(timeout_seconds)

    def stop(self):
        self._stop_event.set()

    def serve_forever(self):
        import zmq

        context = zmq.Context.instance()
        socket = context.socket(zmq.REP)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.RCVHWM, 1000)
        socket.setsockopt(zmq.SNDHWM, 1000)
        try:
            if self.endpoint.endswith(":0"):
                base = self.endpoint.rsplit(":", 1)[0]
                port = socket.bind_to_random_port(base)
                self.actual_endpoint = "%s:%d" % (base, port)
            else:
                socket.bind(self.endpoint)
                self.actual_endpoint = self.endpoint
            self._ready_event.set()
            poller = zmq.Poller()
            poller.register(socket, zmq.POLLIN)
            while not self._stop_event.is_set():
                events = dict(poller.poll(self.poll_ms))
                if socket not in events:
                    continue
                try:
                    request = json.loads(socket.recv().decode("utf-8"))
                    response = self.protocol.handle(request)
                except Exception as exc:
                    response = {
                        "schema_version": 1,
                        "request_id": "",
                        "run_id": self.protocol.engine.config.run_id,
                        "client_id": "",
                        "method": "",
                        "ok": False,
                        "data": None,
                        "error": "%s: %s" % (exc.__class__.__name__, exc),
                    }
                socket.send(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                if self.exit_on_finish and self.protocol.engine.finished:
                    break
        finally:
            self._ready_event.set()
            socket.close(linger=0)
