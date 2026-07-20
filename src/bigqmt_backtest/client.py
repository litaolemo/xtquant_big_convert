"""External-strategy client SDK for the ZMQ backtest bridge."""

import json
import uuid


class BacktestRemoteError(RuntimeError):
    pass


class BacktestZmqClient(object):
    def __init__(
        self,
        endpoint,
        run_id,
        client_id="external-strategy",
        timeout_seconds=10.0,
    ):
        self.endpoint = str(endpoint)
        self.run_id = str(run_id)
        self.client_id = str(client_id)
        self.timeout_seconds = float(timeout_seconds)
        self._context = None
        self._socket = None

    def _connect(self):
        if self._socket is not None:
            return self._socket
        import zmq

        self._context = zmq.Context.instance()
        self._socket = self._context.socket(zmq.REQ)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.connect(self.endpoint)
        return self._socket

    def _reset_socket(self):
        if self._socket is not None:
            self._socket.close(linger=0)
        self._socket = None

    def request(self, method, params=None, request_id=None):
        import zmq

        request_id = str(request_id or uuid.uuid4().hex)
        envelope = {
            "schema_version": 1,
            "request_id": request_id,
            "run_id": self.run_id,
            "client_id": self.client_id,
            "method": str(method),
            "params": dict(params or {}),
        }
        socket = self._connect()
        socket.send(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        events = dict(poller.poll(int(self.timeout_seconds * 1000)))
        if socket not in events:
            self._reset_socket()
            raise TimeoutError("backtest ZMQ request timed out: %s" % method)
        response = json.loads(socket.recv().decode("utf-8"))
        if str(response.get("request_id") or "") != request_id:
            raise BacktestRemoteError("response request_id mismatch")
        if not response.get("ok"):
            raise BacktestRemoteError(str(response.get("error") or "remote request failed"))
        return response.get("data")

    def ping(self):
        return self.request("ping")

    def describe(self):
        data = self.request("describe")
        if not self.run_id and data.get("run_id"):
            self.run_id = str(data["run_id"])
        return data

    def start(self):
        return self.request("start")

    def next_bar(self):
        return self.request("next_bar")

    def state(self):
        return self.request("state")

    def submit_order(
        self,
        symbol,
        side,
        quantity,
        order_type="MARKET",
        limit_price=None,
        client_order_id="",
        time_in_force="NEXT_BAR",
    ):
        params = {
            "symbol": symbol,
            "side": side,
            "quantity": int(quantity),
            "order_type": order_type,
            "client_order_id": client_order_id,
            "time_in_force": time_in_force,
        }
        if limit_price is not None:
            params["limit_price"] = limit_price
        return self.request("submit_order", params)

    def cancel_order(self, order_id):
        return self.request("cancel_order", {"order_id": order_id})

    def history(self, symbol, count=100, fields=None):
        params = {"symbol": symbol, "count": int(count)}
        if fields is not None:
            params["fields"] = list(fields)
        return self.request("history", params)

    def orders(self):
        return self.request("orders")

    def fills(self):
        return self.request("fills")

    def finish(self):
        return self.request("finish")

    def close(self):
        self._reset_socket()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
