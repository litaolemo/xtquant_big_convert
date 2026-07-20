"""Versioned request/response protocol for the backtest-only ZMQ bridge."""

import datetime as dt
import json


SCHEMA_VERSION = 1


class BacktestBridgeProtocol(object):
    def __init__(self, engine, request_cache_size=10000):
        self.engine = engine
        self.request_cache_size = int(request_cache_size)
        self.client_id = None
        self._responses = {}
        self._request_fingerprints = {}
        self._response_order = []

    def _response(self, request, ok, data=None, error=""):
        execution_backend = str(getattr(self.engine, "execution_backend", "LOCAL_SIM"))
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": str(request.get("request_id") or ""),
            "run_id": self.engine.config.run_id,
            "client_id": str(request.get("client_id") or ""),
            "method": str(request.get("method") or ""),
            "ok": bool(ok),
            "data": data,
            "error": str(error or ""),
            "execution_mode": "QMT_BACKTEST" if execution_backend == "QMT_NATIVE" else "BACKTEST",
            "execution_backend": execution_backend,
            "live_ready": False,
            "handled_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def _fingerprint(request):
        return json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _remember(self, request_id, request, response):
        self._responses[request_id] = response
        self._request_fingerprints[request_id] = self._fingerprint(request)
        self._response_order.append(request_id)
        while len(self._response_order) > self.request_cache_size:
            oldest = self._response_order.pop(0)
            self._responses.pop(oldest, None)
            self._request_fingerprints.pop(oldest, None)

    def _validate(self, request):
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        if int(request.get("schema_version") or 0) != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if not str(request.get("request_id") or ""):
            raise ValueError("request_id is required")
        method = str(request.get("method") or "").lower()
        requested_run_id = str(request.get("run_id") or "")
        discovery = method in ("ping", "describe") and not requested_run_id
        if not discovery and requested_run_id != self.engine.config.run_id:
            raise ValueError("run_id mismatch")
        if not str(request.get("client_id") or ""):
            raise ValueError("client_id is required")
        if not str(request.get("method") or ""):
            raise ValueError("method is required")

    def _claim_or_check_client(self, request):
        client_id = str(request["client_id"])
        method = str(request["method"]).lower()
        if self.client_id is None and method == "start":
            self.client_id = client_id
        if method not in ("ping", "describe") and self.client_id != client_id:
            raise PermissionError("run is owned by another client_id")

    def _dispatch(self, request):
        method = str(request["method"]).lower()
        params = dict(request.get("params") or {})
        if method == "ping":
            return {
                "status": "ok",
                "started": self.engine.started,
                "finished": self.engine.finished,
            }
        if method == "describe":
            execution_backend = str(getattr(self.engine, "execution_backend", "LOCAL_SIM"))
            return {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.engine.config.run_id,
                "engine_version": str(getattr(self.engine, "engine_version", "1.0.0")),
                "execution_backend": execution_backend,
                "engine_owner": "QMT" if execution_backend == "QMT_NATIVE" else "LOCAL",
                "matching_owner": "QMT" if execution_backend == "QMT_NATIVE" else "LOCAL",
                "methods": [
                    "ping", "describe", "start", "next_bar", "submit_order", "cancel_order",
                    "state", "history", "orders", "fills", "finish",
                ],
                "fill_timing": str(getattr(self.engine, "fill_timing", "next_symbol_bar")),
                "live_ready": False,
            }
        if method == "start":
            return self.engine.start()
        if method == "next_bar":
            return self.engine.next_bar()
        if method == "submit_order":
            return self.engine.submit_order(params)
        if method == "cancel_order":
            return self.engine.cancel_order(params.get("order_id"))
        if method == "state":
            return self.engine.state()
        if method == "history":
            return self.engine.history(
                params.get("symbol"),
                count=params.get("count", 100),
                fields=params.get("fields"),
            )
        if method == "orders":
            return self.engine.orders()
        if method == "fills":
            return self.engine.fills()
        if method == "finish":
            return self.engine.finish()
        raise ValueError("unsupported method: %s" % method)

    def handle(self, request):
        request_id = str((request or {}).get("request_id") or "")
        if request_id and request_id in self._responses:
            if self._request_fingerprints.get(request_id) != self._fingerprint(request):
                return self._response(request, False, None, "request_id reused with different payload")
            return self._responses[request_id]
        try:
            self._validate(request)
            self._claim_or_check_client(request)
            response = self._response(request, True, self._dispatch(request))
        except Exception as exc:
            response = self._response(request or {}, False, None, "%s: %s" % (exc.__class__.__name__, exc))
        if request_id:
            self._remember(request_id, request, response)
        return response
