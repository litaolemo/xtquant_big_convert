"""Redis Pub/Sub RPC for the Big QMT runtime.

By default the service can process selected requests directly in the Redis
listener thread. The in-memory queue and ``drain_pending`` are kept as a
runtime fallback for environments where a QMT API must run from a strategy
callback thread.
"""

import base64
import datetime as _dt
import json
import math
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
    "get_instrument_type",
    "get_market_data",
    "get_market_data_ex",
    "get_local_data",
    "get_stock_list_in_sector",
    "get_sector_list",
    "get_sector_info",
    "get_markets",
    "get_market_last_trade_date",
    "get_divid_factors",
    "download_history_data",
    "download_history_data2",
    "get_trading_dates",
    "get_holidays",
    "download_holiday_data",
    "get_ipo_info",
    "get_etf_info",
    "download_etf_info",
    "get_option_list",
    "get_his_option_list",
    "get_his_option_list_batch",
    "get_financial_data",
    "download_financial_data",
    "download_financial_data2",
    "call_formula",
    "subscribe_formula",
    "unsubscribe_formula",
    "get_formula_result",
    "gen_factor_index",
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

LISTENER_DEFERRED_METHODS = {
    "sync_positions",
}

METHOD_ALIASES = {
    "get_full_tick": "get_ticks",
    "get_instrument_detail": "get_instrument",
    "get_instrumentdetail": "get_instrument",
    "getDividFactors": "get_divid_factors",
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
SAFE_B64_PREFIX = "b64s:"
SAFE_B64_DIGIT_ENCODE = str.maketrans("0123456789", "!#$%&()*~?")
SAFE_B64_DIGIT_DECODE = str.maketrans("!#$%&()*~?", "0123456789")
MARKET_DATA_METHODS = {
    "get_instrument_type",
    "get_market_data",
    "get_market_data_ex",
    "get_local_data",
    "get_stock_list_in_sector",
    "get_sector_list",
    "get_sector_info",
    "get_markets",
    "get_market_last_trade_date",
    "get_divid_factors",
    "download_history_data",
    "download_history_data2",
    "get_trading_dates",
    "get_holidays",
    "download_holiday_data",
    "get_ipo_info",
    "get_etf_info",
    "download_etf_info",
    "get_option_list",
    "get_his_option_list",
    "get_his_option_list_batch",
    "get_financial_data",
    "download_financial_data",
    "download_financial_data2",
    "call_formula",
    "subscribe_formula",
    "unsubscribe_formula",
    "get_formula_result",
    "gen_factor_index",
}


def _maybe_scalar(value):
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            return value
    return value


def to_jsonable(value):
    value = _maybe_scalar(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "isoformat") and value.__class__.__module__.startswith("pandas"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if hasattr(value, "to_dict") and hasattr(value, "columns") and hasattr(value, "index"):
        try:
            frame = value.reset_index()
            return {
                "__bigqmt_type__": "DataFrame",
                "columns": [str(col) for col in frame.columns],
                "records": to_jsonable(frame.to_dict("records")),
            }
        except Exception:
            return str(value)
    if hasattr(value, "to_dict") and hasattr(value, "index") and not isinstance(value, dict):
        try:
            return {
                "__bigqmt_type__": "Series",
                "data": to_jsonable(value.to_dict()),
            }
        except Exception:
            return str(value)
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return to_jsonable(value.tolist())
        except Exception:
            pass
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
        if handler is None and method in MARKET_DATA_METHODS:
            return self._handle_market_data_method(method, params)
        elif handler is None:
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

    def _handle_market_data_method(self, method, params):
        handler = getattr(self.market_data, method, None)
        if handler is None:
            raise NotImplementedError("market data method is not available: %s" % method)
        return handler(**dict(params or {}))

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


def encode_rpc_request_payload(request):
    """Encode request JSON so patched QMT Redis clients do not inspect stock-code text."""

    raw = json.dumps(request, ensure_ascii=False).encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii").translate(SAFE_B64_DIGIT_ENCODE)
    return SAFE_B64_PREFIX + encoded


def decode_rpc_request_payload(text):
    text = str(text)
    if not text.startswith(SAFE_B64_PREFIX):
        return text
    encoded = text[len(SAFE_B64_PREFIX):].translate(SAFE_B64_DIGIT_DECODE)
    return base64.b64decode(encoded.encode("ascii")).decode("utf-8")


class RedisPubSubRpcService:
    """Receive RPC requests from Redis and write responses back to Redis."""

    def __init__(
        self,
        redis_client,
        handlers,
        account_id="",
        response_redis_client=None,
        request_channel_template="bigqmt:rpc:req:{account_id}",
        request_queue_template="bigqmt:rpc:queue:{account_id}",
        response_channel_template="bigqmt:rpc:resp:{account_id}:{request_id}",
        response_list_template="bigqmt:rpc:respq:{account_id}:{request_id}",
        response_key_template="bigqmt:rpc:resp:{account_id}:{request_id}",
        response_ttl_seconds=60,
        max_queue_size=200,
        process_in_listener=False,
        listener_methods=None,
        background_threads=True,
        queue_poll_interval_seconds=0.02,
        debug_log_limit=0,
        print_prefix="[bigqmt_rpc]",
    ):
        self.listen_redis = redis_client
        self.redis = response_redis_client or redis_client
        self.handlers = handlers
        self.account_id = str(account_id or "")
        self.request_channel_template = request_channel_template
        self.request_queue_template = request_queue_template
        self.response_channel_template = response_channel_template
        self.response_list_template = response_list_template
        self.response_key_template = response_key_template
        self.response_ttl_seconds = int(response_ttl_seconds)
        self.process_in_listener = bool(process_in_listener)
        self.background_threads = bool(background_threads)
        if listener_methods is None:
            listener_methods = ("ping",)
        self.listener_methods = self._expand_listener_methods(listener_methods)
        self.queue_poll_interval_seconds = max(0.001, float(queue_poll_interval_seconds))
        self.debug_log_limit = int(debug_log_limit)
        self._received_count = 0
        self._processed_count = 0
        self._published_count = 0
        self.print_prefix = print_prefix
        self.pending = queue.Queue(maxsize=int(max_queue_size))
        self._running = threading.Event()
        self._thread = None
        self._queue_thread = None
        self._pubsub = None

    @property
    def request_channel(self):
        return self.request_channel_template.format(account_id=self.account_id)

    @property
    def request_queue(self):
        return self.request_queue_template.format(account_id=self.account_id)

    def start(self):
        if not self.background_threads:
            print("%s started queue=%s background_threads=False" % (self.print_prefix, self.request_queue))
            return
        if (
            self._running.is_set()
            and self._thread is not None
            and self._thread.is_alive()
            and self._queue_thread is not None
            and self._queue_thread.is_alive()
        ):
            return
        self._running.set()
        self._thread = threading.Thread(target=self._listen_loop, name="bigqmt-redis-rpc", daemon=True)
        self._queue_thread = threading.Thread(target=self._queue_loop, name="bigqmt-redis-rpc-queue", daemon=True)
        self._thread.start()
        self._queue_thread.start()
        print("%s started channel=%s queue=%s" % (self.print_prefix, self.request_channel, self.request_queue))

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
        queue_thread = self._queue_thread
        if queue_thread is not None and queue_thread.is_alive():
            queue_thread.join(1.0)
        self._thread = None
        self._queue_thread = None

    def _listen_loop(self):
        while self._running.is_set():
            try:
                pubsub = self.listen_redis.pubsub(ignore_subscribe_messages=True)
                self._pubsub = pubsub
                pubsub.subscribe(self.request_channel)
                if self.debug_log_limit > 0:
                    print("%s subscribed channel=%s" % (self.print_prefix, self.request_channel))
                while self._running.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if not self._running.is_set():
                        break
                    if not message or message.get("type") != "message":
                        continue
                    self._handle_received_payload(message.get("data"), "pubsub")
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

    def _queue_loop(self):
        while self._running.is_set():
            try:
                if self.debug_log_limit > 0:
                    print("%s queue polling key=%s" % (self.print_prefix, self.request_queue))
                while self._running.is_set():
                    item = self.listen_redis.lpop(self.request_queue)
                    if not self._running.is_set():
                        break
                    if not item:
                        time.sleep(self.queue_poll_interval_seconds)
                        continue
                    self._handle_received_payload(item, "queue")
            except Exception:
                print("%s queue listener failed:\n%s" % (self.print_prefix, traceback.format_exc()))
                time.sleep(1.0)

    def _handle_received_payload(self, raw_payload, source):
        self._received_count += 1
        if self._received_count <= self.debug_log_limit:
            try:
                preview = self._loads(raw_payload)
                method = str(preview.get("method") or "")
                print(
                    "%s received source=%s method=%s inline=%s"
                    % (self.print_prefix, source, method, self._should_process_in_listener(preview))
                )
                self.enqueue_payload(preview)
                return
            except Exception:
                print("%s receive preview failed:\n%s" % (self.print_prefix, traceback.format_exc()))
        self.enqueue_payload(raw_payload)

    def enqueue_payload(self, raw_payload):
        payload = self._loads(raw_payload)
        if self._should_process_in_listener(payload):
            self.process_request(payload)
            return
        self.pending.put_nowait(payload)

    def _should_process_in_listener(self, payload):
        if not self.process_in_listener:
            return False
        method = str((payload or {}).get("method") or "")
        if method in self.listener_methods:
            return True
        canonical = getattr(self.handlers, "_canonical_method", lambda value: value)(method)
        return canonical in self.listener_methods

    def _expand_listener_methods(self, listener_methods):
        methods = set()
        for method in listener_methods or ():
            method = str(method)
            if method in ("*", "all", "read", "readonly"):
                methods.update(READ_METHODS - LISTENER_DEFERRED_METHODS)
            else:
                methods.add(method)
                canonical = getattr(self.handlers, "_canonical_method", lambda value: value)(method)
                methods.add(canonical)
        return methods

    def _loads(self, raw_payload):
        if isinstance(raw_payload, dict):
            return dict(raw_payload)
        text = decode_text(raw_payload)
        text = decode_rpc_request_payload(text)
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

    def drain_request_queue(self, max_items=20):
        processed = 0
        for _ in range(int(max_items)):
            item = self.listen_redis.lpop(self.request_queue)
            if not item:
                break
            self.process_request(self._loads(item))
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
        self._processed_count += 1
        if self._processed_count <= self.debug_log_limit:
            print("%s responded method=%s ok=%s" % (self.print_prefix, method, response["ok"]))
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
        response_list = request.get("reply_list")
        if response_key:
            self._write_response_key(response_key, ttl_seconds, payload)
        if response_list:
            self._push_response_list(response_list, ttl_seconds, payload)
        if response_channel:
            self._publish_response_channel(response_channel, payload)

    def _response_clients(self):
        clients = [self.redis]
        if self.listen_redis is not self.redis:
            clients.append(self.listen_redis)
        return clients

    def _write_response_key(self, response_key, ttl_seconds, payload):
        first_error = None
        wrote = 0
        for client in self._response_clients():
            try:
                if ttl_seconds > 0:
                    client.setex(response_key, ttl_seconds, payload)
                else:
                    client.set(response_key, payload)
                wrote += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if wrote <= 0 and first_error is not None:
            raise first_error
        return wrote

    def _push_response_list(self, response_list, ttl_seconds, payload):
        first_error = None
        pushed = 0
        for client in self._response_clients():
            try:
                client.rpush(response_list, payload)
                if ttl_seconds > 0:
                    client.expire(response_list, ttl_seconds)
                pushed += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if pushed <= 0 and first_error is not None:
            raise first_error
        return pushed

    def _publish_response_channel(self, response_channel, payload):
        first_error = None
        receivers = 0
        published = 0
        for client in self._response_clients():
            try:
                receivers += int(client.publish(response_channel, payload) or 0)
                published += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if published <= 0 and first_error is not None:
            raise first_error
        self._published_count += 1
        if self._published_count <= self.debug_log_limit:
            print("%s published response receivers=%s" % (self.print_prefix, receivers))
        return receivers


def call_redis_rpc(
    redis_client,
    account_id,
    method,
    params=None,
    request_channel_template="bigqmt:rpc:req:{account_id}",
    request_queue_template="bigqmt:rpc:queue:{account_id}",
    response_channel_template="bigqmt:rpc:resp:{account_id}:{request_id}",
    response_list_template="bigqmt:rpc:respq:{account_id}:{request_id}",
    response_key_template="bigqmt:rpc:resp:{account_id}:{request_id}",
    timeout_seconds=3.0,
    ttl_seconds=60,
    transport="queue",
):
    """Small external client helper for tests and admin scripts."""

    request_id = uuid.uuid4().hex
    request_channel = request_channel_template.format(account_id=account_id)
    request_queue = request_queue_template.format(account_id=account_id)
    response_channel = response_channel_template.format(account_id=account_id, request_id=request_id)
    response_list = response_list_template.format(account_id=account_id, request_id=request_id)
    response_key = response_key_template.format(account_id=account_id, request_id=request_id)
    request = {
        "schema_version": 1,
        "request_id": request_id,
        "account_id": account_id,
        "method": method,
        "params": params or {},
        "reply_channel": response_channel,
        "reply_list": response_list,
        "reply_key": response_key,
        "ttl_seconds": ttl_seconds,
    }
    payload = encode_rpc_request_payload(request)
    if str(transport or "queue").lower() in ("queue", "list", "blpop"):
        redis_client.rpush(request_queue, payload)
        redis_client.expire(request_queue, max(60, int(ttl_seconds)))
        wait_timeout = max(1, int(float(timeout_seconds) + 0.999))
        item = redis_client.blpop(response_list, timeout=wait_timeout)
        if item:
            raw_response = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
            try:
                redis_client.delete(response_list)
            except Exception:
                pass
            return json.loads(decode_text(raw_response))
        raw_response = redis_client.get(response_key)
        if raw_response:
            return json.loads(decode_text(raw_response))
        raise TimeoutError("redis rpc timeout: %s" % method)

    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    try:
        pubsub.subscribe(response_channel)
        redis_client.publish(request_channel, payload)
        deadline = time.time() + float(timeout_seconds)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            message = pubsub.get_message(timeout=remaining)
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
