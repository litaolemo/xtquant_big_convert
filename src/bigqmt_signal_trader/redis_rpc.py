"""Redis Pub/Sub RPC for the Big QMT runtime.

The subscriber thread only receives Redis messages and pushes them into an
in-memory queue. Real QMT calls are drained from the strategy callback thread
by ``drain_pending``.
"""

import datetime as _dt
import json
import queue
import threading
import time
import traceback
import uuid

from .adapters.redis_common import decode_text
from .code_utils import normalize_stock_code
from .models import AccountSnapshot, OrderRef, OrderRequest


READ_METHODS = {
    "ping",
    "get_ticks",
    "get_instrument",
    "get_positions",
    "get_asset",
    "query_orders",
    "query_trades",
    "query_stock_position",
    "sync_positions",
}

ORDER_METHODS = {
    "submit_order",
    "cancel_order",
}

METHOD_ALIASES = {
    "get_full_tick": "get_ticks",
    "get_instrument_detail": "get_instrument",
    "get_instrumentdetail": "get_instrument",
    "query_stock_asset": "get_asset",
    "query_stock_positions": "get_positions",
    "query_stock_orders": "query_orders",
    "query_stock_trades": "query_trades",
    "order_stock": "submit_order",
    "order_stock_async": "submit_order",
    "cancel_order_stock": "cancel_order",
    "cancel_order_stock_sysid": "cancel_order",
}

BUY_ORDER_TYPES = {"23", "STOCK_BUY", "BUY", "B"}
SELL_ORDER_TYPES = {"24", "STOCK_SELL", "SELL", "S"}
CANCELABLE_ORDER_STATUSES = {"50", "55"}


def to_jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    if hasattr(value, "__dict__"):
        return {
            key: to_jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


class BigQmtRpcHandlers:
    """Whitelisted RPC method handlers backed by replaceable adapters."""

    def __init__(
        self,
        account_id,
        market_data,
        position_provider,
        order_gateway=None,
        position_sync_sink=None,
        allow_order_methods=False,
        allowed_methods=None,
    ):
        self.account_id = str(account_id or "")
        self.market_data = market_data
        self.position_provider = position_provider
        self.order_gateway = order_gateway
        self.position_sync_sink = position_sync_sink
        self.allow_order_methods = bool(allow_order_methods)
        if allowed_methods is None:
            allowed = set(READ_METHODS)
            if self.allow_order_methods:
                allowed.update(ORDER_METHODS)
            self.allowed_methods = allowed
        else:
            self.allowed_methods = {str(method) for method in allowed_methods}

    def _request_account_id(self, params):
        params = params or {}
        account = params.get("account")
        if isinstance(account, dict):
            account = account.get("account_id") or account.get("accountID") or account.get("id")
        account_id = str(params.get("account_id") or account or self.account_id or "")
        if not account_id:
            raise ValueError("account_id is required")
        return account_id

    def _canonical_method(self, method):
        return METHOD_ALIASES.get(method, method)

    def handle(self, method, params=None):
        requested_method = str(method or "").strip()
        method = self._canonical_method(requested_method)
        params = dict(params or {})
        if not requested_method:
            raise ValueError("method is required")
        if method not in self.allowed_methods:
            raise ValueError("rpc method is not allowed: %s" % requested_method)
        if method in ORDER_METHODS and not self.allow_order_methods:
            raise PermissionError("order rpc methods are disabled")
        handler = getattr(self, "_handle_%s" % method, None)
        if handler is None:
            raise ValueError("rpc method is not implemented: %s" % requested_method)
        return handler(params)

    def _handle_ping(self, params):
        return {
            "pong": True,
            "account_id": self.account_id,
            "server_time": _dt.datetime.now(),
        }

    def _handle_get_ticks(self, params):
        codes = params.get("codes")
        if isinstance(codes, str):
            codes = [codes]
        if not codes:
            code = params.get("code")
            codes = [code] if code else []
        if not codes:
            raise ValueError("codes or code is required")
        return self.market_data.get_ticks(codes)

    def _handle_get_instrument(self, params):
        code = params.get("code")
        if not code:
            raise ValueError("code is required")
        return self.market_data.get_instrument(code)

    def _handle_get_positions(self, params):
        return self.position_provider.get_positions(self._request_account_id(params))

    def _handle_query_stock_position(self, params):
        stock_code = str(params.get("stock_code") or params.get("code") or "").strip()
        if not stock_code:
            raise ValueError("stock_code is required")
        normalized_code = normalize_stock_code(stock_code)
        positions = self.position_provider.get_positions(self._request_account_id(params))
        return positions.get(normalized_code)

    def _handle_get_asset(self, params):
        return self.position_provider.get_asset(self._request_account_id(params))

    def _handle_query_orders(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        orders = self.order_gateway.query_orders(
            self._request_account_id(params),
            str(params.get("strategy_name") or "bigqmt_signal_trader"),
        )
        if _bool_value(params.get("cancelable_only"), False):
            return [
                order
                for order in orders
                if str(getattr(order, "status", "") or "") in CANCELABLE_ORDER_STATUSES
            ]
        return orders

    def _handle_query_trades(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        return self.order_gateway.query_trades(
            self._request_account_id(params),
            str(params.get("strategy_name") or "bigqmt_signal_trader"),
        )

    def _handle_sync_positions(self, params):
        account_id = self._request_account_id(params)
        snapshot = AccountSnapshot(
            account_id=account_id,
            asset=self.position_provider.get_asset(account_id),
            positions=self.position_provider.get_positions(account_id),
            reason=str(params.get("reason") or "rpc"),
            updated_at=_dt.datetime.now(),
        )
        if self.position_sync_sink is not None:
            self.position_sync_sink.publish(snapshot)
        return snapshot

    def _order_action_from_params(self, params):
        action = str(params.get("action") or "").upper()
        if action:
            return action
        order_type = str(params.get("order_type") or "").upper()
        if order_type in BUY_ORDER_TYPES:
            return "BUY"
        if order_type in SELL_ORDER_TYPES:
            return "SELL"
        raise ValueError("action or order_type is required")

    def _handle_submit_order(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        price = params.get("price")
        request = OrderRequest(
            signal_id=str(params.get("signal_id") or "rpc-%s" % uuid.uuid4().hex),
            account_id=self._request_account_id(params),
            action=self._order_action_from_params(params),
            stock_code=str(params.get("stock_code") or ""),
            volume=int(params.get("volume") or params.get("order_volume") or 0),
            price=float(price if price not in (None, "") else 0),
            price_type=params.get("price_type") or "LIMIT",
            strategy_name=str(params.get("strategy_name") or "bigqmt_rpc"),
            remark=str(params.get("remark") or params.get("order_remark") or "redis_rpc"),
        )
        if request.action not in ("BUY", "SELL"):
            raise ValueError("action must be BUY or SELL")
        if not request.stock_code:
            raise ValueError("stock_code is required")
        if request.volume <= 0:
            raise ValueError("volume must be positive")
        return self.order_gateway.submit(request)

    def _handle_cancel_order(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        order_sys_id = str(params.get("order_sys_id") or params.get("order_sysid") or params.get("order_id") or "")
        if not order_sys_id:
            raise ValueError("order_sys_id or order_id is required")
        return self.order_gateway.cancel(
            OrderRef(order_sys_id=order_sys_id, user_order_id=str(params.get("user_order_id") or ""))
        )


def _bool_value(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


class RedisPubSubRpcService:
    """Receive RPC requests from Redis Pub/Sub and write responses to Redis."""

    def __init__(
        self,
        redis_client,
        handlers,
        account_id="",
        request_channel_template="bigqmt:rpc:req:{account_id}",
        response_channel_template="bigqmt:rpc:resp:{account_id}:{request_id}",
        response_key_template="bigqmt:rpc:resp:{account_id}:{request_id}",
        response_ttl_seconds=60,
        max_queue_size=200,
        print_prefix="[bigqmt_rpc]",
    ):
        self.redis = redis_client
        self.handlers = handlers
        self.account_id = str(account_id or "")
        self.request_channel_template = request_channel_template
        self.response_channel_template = response_channel_template
        self.response_key_template = response_key_template
        self.response_ttl_seconds = int(response_ttl_seconds)
        self.print_prefix = print_prefix
        self.pending = queue.Queue(maxsize=int(max_queue_size))
        self._running = threading.Event()
        self._thread = None
        self._pubsub = None

    @property
    def request_channel(self):
        return self.request_channel_template.format(account_id=self.account_id)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._listen_loop, name="bigqmt-redis-rpc", daemon=True)
        self._thread.start()
        print("%s started channel=%s" % (self.print_prefix, self.request_channel))

    def stop(self):
        self._running.clear()
        pubsub = self._pubsub
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(1.0)
        self._thread = None

    def _listen_loop(self):
        while self._running.is_set():
            try:
                pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
                self._pubsub = pubsub
                pubsub.subscribe(self.request_channel)
                while self._running.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if not message or message.get("type") != "message":
                        continue
                    self.enqueue_payload(message.get("data"))
            except Exception:
                print("%s listener failed:\n%s" % (self.print_prefix, traceback.format_exc()))
                time.sleep(1.0)
            finally:
                try:
                    if self._pubsub is not None:
                        self._pubsub.close()
                except Exception:
                    pass
                self._pubsub = None

    def enqueue_payload(self, raw_payload):
        payload = self._loads(raw_payload)
        self.pending.put_nowait(payload)

    def _loads(self, raw_payload):
        if isinstance(raw_payload, dict):
            return dict(raw_payload)
        text = decode_text(raw_payload)
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("rpc payload must be a json object")
        return payload

    def drain_pending(self, max_items=20):
        processed = 0
        for _ in range(int(max_items)):
            try:
                request = self.pending.get_nowait()
            except queue.Empty:
                break
            self.process_request(request)
            processed += 1
        return processed

    def process_request(self, request):
        request = dict(request or {})
        request_id = str(request.get("request_id") or request.get("id") or uuid.uuid4().hex)
        account_id = str(request.get("account_id") or self.account_id or "")
        method = str(request.get("method") or "")
        response = {
            "schema_version": 1,
            "request_id": request_id,
            "account_id": account_id,
            "method": method,
            "ok": False,
            "data": None,
            "error": "",
            "handled_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            if self.account_id and account_id and account_id != self.account_id:
                raise PermissionError("account_id mismatch")
            response["data"] = to_jsonable(self.handlers.handle(method, request.get("params") or {}))
            response["ok"] = True
        except Exception as exc:
            response["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        self._publish_response(request, response)
        return response

    def _format_response_target(self, template, account_id, request_id):
        if not template:
            return ""
        return template.format(account_id=account_id, request_id=request_id)

    def _publish_response(self, request, response):
        request_id = response["request_id"]
        account_id = response["account_id"] or self.account_id
        payload = json.dumps(response, ensure_ascii=False)
        ttl_seconds = int(request.get("ttl_seconds") or self.response_ttl_seconds)
        response_key = request.get("reply_key") or self._format_response_target(
            self.response_key_template, account_id, request_id
        )
        response_channel = request.get("reply_channel") or self._format_response_target(
            self.response_channel_template, account_id, request_id
        )
        if response_key:
            if ttl_seconds > 0:
                self.redis.setex(response_key, ttl_seconds, payload)
            else:
                self.redis.set(response_key, payload)
        if response_channel:
            self.redis.publish(response_channel, payload)


def call_redis_rpc(
    redis_client,
    account_id,
    method,
    params=None,
    request_channel_template="bigqmt:rpc:req:{account_id}",
    response_channel_template="bigqmt:rpc:resp:{account_id}:{request_id}",
    response_key_template="bigqmt:rpc:resp:{account_id}:{request_id}",
    timeout_seconds=3.0,
    ttl_seconds=60,
):
    """Small external client helper for tests and admin scripts."""

    request_id = uuid.uuid4().hex
    request_channel = request_channel_template.format(account_id=account_id)
    response_channel = response_channel_template.format(account_id=account_id, request_id=request_id)
    response_key = response_key_template.format(account_id=account_id, request_id=request_id)
    request = {
        "schema_version": 1,
        "request_id": request_id,
        "account_id": account_id,
        "method": method,
        "params": params or {},
        "reply_channel": response_channel,
        "reply_key": response_key,
        "ttl_seconds": ttl_seconds,
    }
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    try:
        pubsub.subscribe(response_channel)
        redis_client.publish(request_channel, json.dumps(request, ensure_ascii=False))
        deadline = time.time() + float(timeout_seconds)
        while time.time() < deadline:
            message = pubsub.get_message(timeout=0.2)
            if not message or message.get("type") != "message":
                continue
            response = json.loads(decode_text(message.get("data")))
            if response.get("request_id") == request_id:
                return response
        raw_response = redis_client.get(response_key)
        if raw_response:
            return json.loads(decode_text(raw_response))
        raise TimeoutError("redis rpc timeout: %s" % method)
    finally:
        try:
            pubsub.close()
        except Exception:
            pass
