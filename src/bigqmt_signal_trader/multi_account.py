# coding: utf-8
"""Multi-account RPC service: one strategy instance serving multiple accounts.

When ``BIGQMT_ACCOUNT_TYPE_MAP`` (in the local config) maps multiple account IDs
to different account types (e.g. ``{"123456": "STOCK", "789012": "FUTURE"}``),
a single QMT terminal can serve both accounts from one strategy instance.

This module provides:

- :func:`build_multi_account_rpc_service` — the entry point called from
  ``_build_rpc_service`` in the strategy module. When the map has only one
  entry (or is empty), it falls back to the single-service builder unchanged.
- :class:`MultiAccountRpcServiceManager` — a thin wrapper that delegates
  start/stop/drain to all services and exposes the primary's attributes.
- :class:`SecondaryHandlersProxy` — injects the secondary account_id into
  every ``handle()`` call, so per-request account_type resolution (from
  ``account_type_map.py``, PR #135) routes correctly for the secondary
  channel.

Architecture
~~~~~~~~~~~~

One ``BigQmtRpcHandlers`` instance is shared across all RPC services.  The
primary service runs on the adjust thread (``background_threads=False``);
secondary services run on background threads (``background_threads=True``),
with trade-context requests (submit, cancel, query) deferred to the primary's
drain loop via ``pending`` queues.

Per-request account_type resolution (PR #135) means the gateway's
``_resolve_account_type(account_id)`` returns the correct type for each
request's account_id — no save/restore of ``self.account_type`` is needed,
and no monkey-patching of the handlers class is required.
"""

import logging

logger = logging.getLogger(__name__)


class SecondaryHandlersProxy:
    """Inject secondary account_id into params for every ``handle()`` call.

    Both primary and secondary RPC services share the same handlers instance,
    so ``handlers.account_id`` is always the primary's.  This proxy ensures the
    secondary's account_id is injected into ``params``, allowing per-request
    account_type resolution to route correctly for secondary-channel requests
    (including ping, which otherwise has no account_id in params).

    Attribute access is delegated to the proxied handlers, so this is
    transparent to callers.
    """

    def __init__(self, handlers, account_id):
        object.__setattr__(self, "_proxied", handlers)
        object.__setattr__(self, "_secondary_account_id", str(account_id or ""))

    def handle(self, method, params=None):
        params = dict(params or {})
        if "account_id" not in params and self._secondary_account_id:
            params["account_id"] = self._secondary_account_id
        return self._proxied.handle(method, params)

    def __getattr__(self, name):
        return getattr(self._proxied, name)

    def __setattr__(self, name, value):
        setattr(self._proxied, name, value)


class MultiAccountRpcServiceManager:
    """Manages primary + secondary RPC services for multi-account deployment.

    Delegates start/stop/drain to all services.  Exposes the primary service's
    attributes (``redis``, ``listen_redis``, ``account_id``, etc.) so that the
    strategy module can treat a single-service or multi-service deployment
    identically.
    """

    def __init__(self, services, handlers, exec_poller=None):
        self._services = services
        self.handlers = handlers
        self._primary = services[0]
        self.account_id = services[0].account_id
        self.redis = services[0].redis
        self.listen_redis = services[0].listen_redis
        # QMT calls order_callback / deal_callback for the bound (primary)
        # account only; the secondaries' events come from polling (#320).
        self.exec_poller = exec_poller

    def start(self):
        for s in self._services:
            try:
                s.start()
            except Exception as e:
                logger.error("multi_account start %s: %s", s.account_id[:3], e)

    def stop(self):
        for s in self._services:
            try:
                s.stop()
            except Exception:
                pass
            # Close secondary's transport listen_redis so zombie brpop/pubsub
            # threads exit immediately instead of surviving the join timeout
            # and competing for requests after reload.  The primary's
            # listen_redis is shared with the service-level attribute and
            # must NOT be closed here — it is managed by the primary's own
            # stop() lifecycle.
            if s is not self._primary:
                transport = getattr(s, "_transport", None)
                if transport is not None:
                    lr = getattr(transport, "listen_redis", None)
                    if lr is not None:
                        try:
                            lr.close()
                        except Exception:
                            pass
                # Also close the service-level listen_redis if it is a
                # separate connection (not shared with primary).
                slr = getattr(s, "listen_redis", None)
                if slr is not None and slr is not getattr(
                    self._primary, "listen_redis", None
                ):
                    try:
                        slr.close()
                    except Exception:
                        pass

    def drain_request_queue(self, max_items=20):
        return sum(
            s.drain_request_queue(max_items)
            for s in self._services
            if hasattr(s, "drain_request_queue")
        )

    def drain_pending(self, max_items=20, budget_seconds=None):
        processed = sum(
            s.drain_pending(max_items, budget_seconds=budget_seconds)
            for s in self._services
            if hasattr(s, "drain_pending")
        )
        poller = self.exec_poller
        if poller is not None:
            try:
                poller.poll()
            except Exception as e:
                logger.error("multi_account: secondary exec poll failed: %s", e)
        return processed

    def secondary_exec_poll_status(self):
        poller = self.exec_poller
        return poller.status() if poller is not None else None

    def __getattr__(self, name):
        return getattr(self._primary, name)


def build_multi_account_rpc_service(context_info, app, config, build_single_fn):
    """Build one ``RedisPubSubRpcService`` per account in the type map.

    Falls back to the single-service builder (``build_single_fn``) when the
    map is empty or has only one entry — zero behavior change for
    single-account deployments.

    Parameters
    ----------
    context_info, app, config :
        Forwarded to ``build_single_fn`` for the primary service.
    build_single_fn : callable
        The original ``_build_rpc_service`` from the strategy module.

    Returns
    -------
    RedisPubSubRpcService or MultiAccountRpcServiceManager or None
    """
    from .account_type_map import get_account_type_map

    account_map = get_account_type_map()
    if not account_map:
        return build_single_fn(context_info, app, config)

    primary = build_single_fn(context_info, app, config)
    if primary is None:
        return None

    if len(account_map) <= 1:
        return primary

    # Build secondary service(s) — same handlers, different channel
    services = [primary]
    for aid in account_map:
        if str(aid) == str(primary.account_id):
            continue
        sec = _build_secondary(primary, str(aid), config)
        if sec:
            services.append(sec)

    logger.info(
        "multi_account: %d services, primary=%s***",
        len(services),
        primary.account_id[:3],
    )
    poller = _build_secondary_exec_poller(
        context_info, primary, [s.account_id for s in services[1:]], config)
    return MultiAccountRpcServiceManager(services, primary.handlers, exec_poller=poller)


class _PushSink(object):
    """publish(topic, data) over the quote push channel's publisher; no
    ``xadd``, so exec_events.publish_exec_event takes the push-channel path."""

    def __init__(self, publisher):
        self._publisher = publisher

    def publish(self, topic, data):
        return self._publisher(topic, data)


def _push_sink_from_handlers(handlers):
    manager = getattr(handlers, "quote_subscription_manager", None)
    publisher = getattr(manager, "_on_push_publisher", None)
    return _PushSink(publisher) if callable(publisher) else None


def _build_secondary_exec_poller(context_info, primary, secondary_accounts, config):
    """The #320 poller, or None when exec events are off / no redis sink /
    the gateway cannot read native rows / the interval is 0."""
    exec_config = dict(config.get("exec_events") or {})
    enabled = exec_config.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "off", "")
    if not enabled or not secondary_accounts:
        return None
    rpc_config = dict(config.get("rpc") or {})
    try:
        interval = float(rpc_config.get("secondary_exec_poll_seconds", 1.0))
    except (TypeError, ValueError):
        interval = 1.0
    if interval <= 0:
        return None
    gateway = getattr(primary.handlers, "order_gateway", None)
    query_rows = getattr(gateway, "query_native_rows", None)
    sink = getattr(primary, "redis", None)
    identity_redis = sink
    if sink is None:
        # zmq (#334): no redis. Exec events there go out on the quote push
        # channel (exec:* topics, #76); the manager holds that channel's
        # publish, so wrap it as a push sink.
        sink = _push_sink_from_handlers(primary.handlers)
    if not callable(query_rows) or sink is None:
        logger.warning("multi_account: secondary exec poll unavailable (gateway=%s sink=%s)",
                       type(gateway).__name__, sink is not None)
        return None
    from .secondary_exec_poll import SecondaryExecPoller, build_row_publisher

    publish = build_row_publisher(sink, context_info=context_info, identity_redis=identity_redis)
    poller = SecondaryExecPoller(
        secondary_accounts, query_rows, publish, interval_seconds=interval,
        log=lambda text: logger.warning("multi_account: %s", text))
    logger.info("multi_account: secondary exec poll every %.1fs for %s",
                interval, [a[:3] + "***" for a in poller.accounts])
    return poller


def _secondary_zmq_config(rpc_config, account_id):
    """The zmq block for a secondary: its OWN endpoint, on the primary's host.

    The configured block belongs to the primary -- ``bind_address`` / ``port``
    / ``account_id`` in it would put the secondary on the primary's socket
    (EADDRINUSE) or under the primary's name. Drop those so the port derives
    from the secondary's account_id, which is exactly how a zmq client
    configured with that account derives the address it connects to. Keep
    the host: 0.0.0.0 for a remote-reachable terminal, a specific interface
    for a locked-down one -- dropping it would fall back to loopback.
    """
    zmq_config = dict(rpc_config.get("zmq") or {})
    primary_bind = zmq_config.pop("bind_address", None)
    zmq_config.pop("connect_address", None)
    zmq_config.pop("port", None)
    zmq_config.pop("account_id", None)
    if primary_bind and not zmq_config.get("host"):
        text = str(primary_bind).split("://", 1)[-1]
        host = text.rsplit(":", 1)[0] if ":" in text else text
        if host:
            zmq_config["host"] = host
    return zmq_config


def _build_secondary_non_redis(primary, account_id, config, transport_name):
    """A secondary on the primary's transport kind, for zmq (#334).

    Only zmq derives a per-account endpoint the client already knows how to
    find; pipe / mysql / shm have no per-account addressing, so a secondary
    on them is refused with a log line rather than built on a dead redis
    client (the pre-#334 failure: ``'NoneType' object has no attribute
    'pubsub'`` from the secondary's listener threads).
    """
    from .redis_rpc import RedisPubSubRpcService

    rpc_config = dict((config.get("rpc") or {}))
    if transport_name != "zmq":
        logger.warning("multi_account: secondary %s*** not built -- transport %r has no "
                       "per-account endpoint (redis or zmq only)", account_id[:3], transport_name)
        return None
    try:
        from .transports.factory import build_transport

        factory_config = dict(rpc_config)
        factory_config["account_id"] = account_id
        factory_config["zmq"] = _secondary_zmq_config(rpc_config, account_id)
        transport = build_transport(
            transport_name, factory_config, account_id=account_id,
            print_prefix="[bigqmt_rpc_2nd]")
    except Exception as e:
        logger.error("multi_account: secondary %s*** zmq transport build failed: %s",
                     account_id[:3], e)
        return None
    logger.info("multi_account: secondary %s*** on zmq bound=%s",
                account_id[:3], getattr(transport, "bind_address", "?"))
    return RedisPubSubRpcService(
        redis_client=None,
        response_redis_client=None,
        handlers=SecondaryHandlersProxy(primary.handlers, account_id),
        account_id=account_id,
        max_queue_size=int(rpc_config.get("max_queue_size", 200)),
        process_in_listener=True,
        listener_methods=rpc_config.get("listener_methods") or ("*",),
        background_threads=True,
        transport=transport,
    )


def _build_secondary(primary, account_id, config):
    """Build a secondary ``RedisPubSubRpcService`` sharing the primary's handlers.

    The secondary gets its own Redis connection (so ``stop()`` on the secondary
    doesn't kill the primary's listener) and a ``SecondaryHandlersProxy`` that
    injects the secondary's account_id into every request. On a zmq
    deployment it gets its own zmq endpoint instead (#334).
    """
    rpc_config = dict((config.get("rpc") or {}))
    transport_name = str(rpc_config.get("transport") or "redis").lower()
    if transport_name not in ("redis", "", "default"):
        return _build_secondary_non_redis(primary, account_id, config, transport_name)
    try:
        from .redis_rpc import RedisPubSubRpcService
        from .transports.redis_transport import RedisTransport
        from .adapters import redis_common
    except ImportError:
        logger.warning("multi_account: secondary build failed (import error)")
        return None

    # Build an independent Redis client for the secondary service.
    # Shares the same host/port/db/password as primary, but is a separate
    # connection so closing it during stop() won't affect the primary.
    try:
        redis_config = dict(config.get("redis") or {})
        redis_config.update(dict(rpc_config.get("redis") or {}))
        if redis_config.get("socket_timeout") in (None, ""):
            redis_config["socket_timeout"] = 10
        secondary_listen_redis = redis_common.build_redis_client(redis_config)
    except Exception:
        # Fallback: share primary's connection (suboptimal but functional)
        secondary_listen_redis = primary.listen_redis

    transport = RedisTransport(
        redis_client=secondary_listen_redis,
        account_id=account_id,
        response_redis_client=primary.redis,
        request_channel_template=rpc_config.get(
            "request_channel_template", "bigqmt:rpc:req:{account_id}"),
        request_queue_template=rpc_config.get(
            "request_queue_template", "bigqmt:rpc:queue:{account_id}"),
        response_channel_template=rpc_config.get(
            "response_channel_template",
            "bigqmt:rpc:resp:{account_id}:{request_id}"),
        response_list_template=rpc_config.get(
            "response_list_template",
            "bigqmt:rpc:respq:{account_id}:{request_id}"),
        response_key_template=rpc_config.get(
            "response_key_template",
            "bigqmt:rpc:resp:{account_id}:{request_id}"),
        response_ttl_seconds=int(rpc_config.get("response_ttl_seconds", 60)),
        print_prefix="[bigqmt_rpc_2nd]",
    )

    return RedisPubSubRpcService(
        redis_client=secondary_listen_redis,
        response_redis_client=primary.redis,
        handlers=SecondaryHandlersProxy(primary.handlers, account_id),
        account_id=account_id,
        request_channel_template=rpc_config.get(
            "request_channel_template", "bigqmt:rpc:req:{account_id}"),
        response_channel_template=rpc_config.get(
            "response_channel_template",
            "bigqmt:rpc:resp:{account_id}:{request_id}"),
        response_key_template=rpc_config.get(
            "response_key_template",
            "bigqmt:rpc:resp:{account_id}:{request_id}"),
        response_ttl_seconds=int(rpc_config.get("response_ttl_seconds", 60)),
        max_queue_size=int(rpc_config.get("max_queue_size", 200)),
        process_in_listener=True,
        listener_methods=rpc_config.get("listener_methods") or ("*",),
        background_threads=True,
        transport=transport,
    )
