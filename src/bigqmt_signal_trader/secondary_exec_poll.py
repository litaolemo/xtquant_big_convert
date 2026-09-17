# coding: utf-8
"""Order / trade events for accounts QMT never calls back about (#320, #322).

A big-QMT model is bound to one account, and ``order_callback`` /
``deal_callback`` fire for that account only. In single-instance
multi-account mode (``BIGQMT_ACCOUNT_TYPE_MAP``, 方式一) the secondary
accounts place orders through the same process -- ``passorder`` accepts any
logged-in account -- but their 委托 / 成交 never reach the callbacks, so
``on_stock_order`` / ``on_stock_trade`` on a secondary-account client stayed
silent while ``on_order_stock_async_response`` (an RPC reply, not a push)
worked. There is no MiniQMT-style ``subscribe(account)`` to ask for them.

What the terminal DOES answer for any account is ``get_trade_detail_data``.
So this polls it from the adjust loop -- the one thread it answers on -- and
turns what changed into the same events the callbacks would have produced,
through the same normalizers, onto the same per-account channels.

    orders   one event per (order_sys_id) whose (status, traded) changed
    trades   one event per trade id, once
    baseline the first poll after start publishes nothing: everything
             already in the lists happened before we were watching, and a
             restart must not replay the day as fresh callbacks

Delay is one poll interval (1s by default), and an order that passes
through several states within one interval yields only the last of them.
That is the price of not having the callback; a client that needs every
intermediate state runs the account in its own instance (方式二).
"""

import time


class SecondaryExecPoller(object):
    def __init__(self, accounts, query_rows, publish, interval_seconds=1.0,
                 time_func=None, log=None):
        """
        accounts        secondary account ids to watch
        query_rows      fn(account_id, kind) -> native rows, kind "ORDER"/"DEAL"
        publish         fn(kind, account_id, row) -> None, kind "order"/"trade"
        """
        self.accounts = [str(a) for a in (accounts or []) if str(a or "")]
        self.query_rows = query_rows
        self.publish = publish
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._now = time_func or time.monotonic
        self._log = log or (lambda text: None)
        self._last_poll = None
        # account -> {"orders": {sysid: (status, traded)}, "trades": set(), "baselined": bool}
        self._state = {}
        self._failures = 0
        self.published = 0

    # -- keys -----------------------------------------------------------------
    @staticmethod
    def _field(row, names, default=""):
        for name in names:
            if hasattr(row, name):
                value = getattr(row, name)
                if value is not None:
                    return value
            if isinstance(row, dict) and name in row and row[name] is not None:
                return row[name]
        return default

    @classmethod
    def order_key(cls, row):
        return str(cls._field(row, ("m_strOrderSysID", "order_sys_id")) or "").strip()

    @classmethod
    def order_state(cls, row):
        status = str(cls._field(row, ("m_nOrderStatus", "status")) or "")
        traded = str(cls._field(row, ("m_nVolumeTraded", "traded_volume")) or "0")
        return (status, traded)

    @classmethod
    def trade_key(cls, row):
        trade_id = str(cls._field(row, ("m_strTradeID", "trade_id")) or "").strip()
        if trade_id:
            return trade_id
        # No trade id on this terminal: the fill is identified by what it is.
        return "|".join(str(cls._field(row, names) or "") for names in (
            ("m_strOrderSysID", "order_sys_id"), ("m_strTradeTime", "trade_time"),
            ("m_nVolume", "volume"), ("m_dPrice", "price")))

    # -- polling ----------------------------------------------------------------
    def due(self, now=None):
        now = self._now() if now is None else now
        return self._last_poll is None or now - self._last_poll >= self.interval_seconds

    def poll(self, now=None):
        """Publish what changed since the last poll. Returns events published."""
        now = self._now() if now is None else now
        if not self.accounts or not self.due(now):
            return 0
        self._last_poll = now
        published = 0
        for account in self.accounts:
            state = self._state.get(account)
            if state is None:
                state = self._state[account] = {"orders": {}, "trades": set(), "baselined": False}
            try:
                orders = list(self.query_rows(account, "ORDER") or [])
                trades = list(self.query_rows(account, "DEAL") or [])
            except Exception as exc:
                self._failures += 1
                if self._failures <= 3 or self._failures % 100 == 0:
                    self._log("secondary exec poll failed for %s***: %s" % (account[:3], exc))
                continue
            first = not state["baselined"]
            for row in orders:
                key = self.order_key(row)
                if not key:
                    continue          # pre-sysid row: the next poll sees it with its id
                current = self.order_state(row)
                if state["orders"].get(key) == current:
                    continue
                state["orders"][key] = current
                if not first:
                    published += self._emit("order", account, row)
            for row in trades:
                key = self.trade_key(row)
                if not key or key in state["trades"]:
                    continue
                state["trades"].add(key)
                if not first:
                    published += self._emit("trade", account, row)
            state["baselined"] = True
        self.published += published
        return published

    def _emit(self, kind, account, row):
        try:
            self.publish(kind, account, row)
            return 1
        except Exception as exc:
            self._failures += 1
            if self._failures <= 3 or self._failures % 100 == 0:
                self._log("secondary exec publish %s failed for %s***: %s" % (kind, account[:3], exc))
            return 0

    def status(self):
        return {
            "accounts": [a[:3] + "***" for a in self.accounts],
            "interval_seconds": self.interval_seconds,
            "published": self.published,
            "failures": self._failures,
            "tracked_orders": sum(len(s["orders"]) for s in self._state.values()),
            "tracked_trades": sum(len(s["trades"]) for s in self._state.values()),
        }


def build_row_publisher(sink, context_info=None, identity_redis=None, log=None):
    """A ``publish(kind, account_id, row)`` for the poller: normalize the native
    row the way the callbacks do, enrich, and publish on the account's channel.

    Mirrors the strategy's ``_publish_exec_event`` minus the pre-sysid hold
    (rows without an id are never emitted) and the raw-field debug dump.
    """
    from . import exec_events

    def _instrument_name(code):
        getter = getattr(context_info, "get_stock_name", None)
        if not callable(getter) or not code:
            return ""
        try:
            return str(getter(code) or "")
        except Exception:
            return ""

    def publish(kind, account_id, row):
        if kind == "trade":
            event = exec_events.normalize_trade_event(row, account_id)
        else:
            event = exec_events.normalize_order_event(row, account_id)
        if identity_redis is not None:
            try:
                event = exec_events.enrich_order_identity(identity_redis, account_id, event)
            except Exception:
                pass
        if not event.get("instrument_name"):
            event["instrument_name"] = _instrument_name(event.get("stock_code"))
        event["source"] = "poll"
        exec_events.publish_exec_event(sink, account_id, event)
        if kind == "order":
            try:
                status = int(event.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            if status == 57:      # 废单 -> on_order_error, as the callback path does
                err_event = exec_events.normalize_order_error_event(row, account_id)
                err_event["source"] = "poll"
                exec_events.publish_exec_event(sink, account_id, err_event)

    return publish
