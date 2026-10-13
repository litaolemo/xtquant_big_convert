# coding: utf-8
"""Exact native order/remark associations in the existing Redis identity store.

Only a source-dated native row teaches an association. Amounts, quantities,
prices and execution state remain QMT facts and are never reconstructed here.
Redis loss/expiry or an association not yet observed leaves the remark unknown.
"""

import copy
import datetime
import json
import logging
import math
import time
from urllib.parse import quote


LOGGER = logging.getLogger(__name__)
TIMEOUT_SECONDS = 0.25
_last_warning = -float("inf")

# One atomic operation: learn every native association (including
# conflicts) before recovering ANY blank in the batch. A conflict stays poisoned
# until expiry; neither a later blank nor a later native value clears it.
_OBSERVE_AND_READ = """
for i, key in ipairs(KEYS) do
    local observed = ARGV[i + 1]
    local old = redis.call('GET', key)
    if observed ~= '' then
        if not old then
            redis.call('SET', key, observed, 'EX', ARGV[1])
        elseif old ~= observed and old ~= '!conflict' then
            redis.call('SET', key, '!conflict', 'EX', ARGV[1])
        end
    end
end
for i, key in ipairs(KEYS) do
    local old = redis.call('GET', key)
    if old and old ~= '!conflict' then
        local ok, identity = pcall(cjson.decode, old)
        local expected = cjson.decode(ARGV[1 + #KEYS + i])
        if not ok or type(identity) ~= 'table' or #identity ~= 3
                or identity[1] ~= expected[1] or identity[2] ~= expected[2] then
            redis.call('SET', key, '!conflict', 'EX', ARGV[1])
        end
    end
end
local result = {}
for i, key in ipairs(KEYS) do
    result[i] = redis.call('GET', key) or false
end
return result
"""


def _warn(message, exc=None):
    global _last_warning
    now = time.monotonic()
    if now - _last_warning >= 60:
        _last_warning = now
        LOGGER.warning("%s%s", message, " (%s)" % type(exc).__name__ if exc else "")


def bounded_identity_client(client):
    """One short-timeout pool at init; no retry and no shared-pool mutation."""
    if client is None:
        return None
    try:
        import redis

        pool = client.connection_pool
        kwargs = dict(pool.connection_kwargs)
        for key in ("socket_timeout", "socket_connect_timeout"):
            value = kwargs.get(key)
            kwargs[key] = min(float(value), TIMEOUT_SECONDS) if value else TIMEOUT_SECONDS
        kwargs["retry_on_timeout"] = False
        try:
            from redis.backoff import NoBackoff
            from redis.retry import Retry
        except ImportError:
            # QMT Python 3.6 embeds redis 3.5.3; it has no Retry API and only
            # retries socket timeouts when retry_on_timeout is explicitly set.
            kwargs.pop("retry", None)
            kwargs.pop("retry_on_error", None)
            if issubclass(pool.connection_class, redis.UnixDomainSocketConnection):
                # redis 3.5 uses socket_timeout for Unix socket connect as well.
                kwargs.pop("socket_connect_timeout", None)
        else:
            kwargs["retry"] = Retry(NoBackoff(), 0)
            kwargs["retry_on_error"] = []
        return redis.Redis(connection_pool=redis.ConnectionPool(
            connection_class=pool.connection_class, max_connections=4, **kwargs))
    except Exception as exc:
        _warn("Order remark recovery disabled: bounded Redis client unavailable", exc)
        return None


def _get(row, field, default=None):
    return row.get(field, default) if isinstance(row, dict) else getattr(row, field, default)


def _descriptor(account_id, account_type, row):
    account_id = str(account_id or "").strip()
    account_type = str(account_type or "").strip().upper()
    row_account = str(_get(row, "account_id", "") or "").strip()
    sysid = str(_get(row, "order_sys_id", "") or "").strip()
    stock = str(_get(row, "stock_code", "") or "").strip().upper()
    action = str(_get(row, "action", "") or "").strip().upper()
    if (not account_id or not account_type or (row_account and row_account != account_id)
            or not sysid or sysid in ("0", "-1") or "." not in stock
            or action not in ("BUY", "SELL")):
        return None
    code, exchange = stock.rsplit(".", 1)
    if not code or not exchange:
        return None
    # Never use event receipt time, machine date or the client's timestamp
    # fallback. These fields are normalized directly from QMT source fields.
    stamp = _get(row, "order_time") or _get(row, "traded_time")
    if isinstance(stamp, bool):
        return None
    try:
        stamp = float(stamp)
        if not math.isfinite(stamp) or stamp <= 0:
            return None
        date = (datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
                + datetime.timedelta(hours=8)).strftime("%Y%m%d")
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    key = "bigqmt:order_remark:" + ":".join(
        quote(v, safe="") for v in (account_id, account_type, date, exchange, sysid))
    remarks = {str(_get(row, field, "") or "")
               for field in ("user_order_id", "remark")}
    remarks.discard("")
    if len(remarks) > 1:
        observed = "!conflict"
    elif remarks:
        observed = json.dumps([stock, action, next(iter(remarks))],
                              ensure_ascii=True, separators=(",", ":"))
    else:
        observed = ""
    return key, stock, action, observed


def recover_order_remarks(client, account_id, account_type, rows, ttl_seconds):
    """Return rows with only unknown remarks recovered from exact associations.

    Nonempty native values always win. Native rows are not modified, so a
    recovered value cannot become an observation on a reused gateway object.
    Any Redis failure keeps the whole batch native, including its blanks.
    """
    rows = list(rows or [])
    if client is None or not rows:
        return rows
    wanted = []
    for index, row in enumerate(rows):
        descriptor = _descriptor(account_id, account_type, row)
        if descriptor is not None:
            wanted.append((index, descriptor))
    if not wanted:
        return rows
    try:
        values = client.eval(_OBSERVE_AND_READ, len(wanted),
                             *([d[0] for _, d in wanted]
                               + [str(int(ttl_seconds))]
                               + [d[3] for _, d in wanted]
                               + [json.dumps([d[1], d[2]]) for _, d in wanted]))
        if len(values) != len(wanted):
            raise ValueError("incomplete order remark lookup")
        recovered = []
        for (index, (_, stock, action, observed)), value in zip(wanted, values):
            if value == b"!conflict" or value == "!conflict":
                _warn("Conflicting native order remark association; recovery refused")
                continue
            if observed or not value:
                continue
            identity = json.loads(value)
            if (not isinstance(identity, list) or len(identity) != 3
                    or identity[:2] != [stock, action]
                    or not isinstance(identity[2], str) or not identity[2]):
                continue
            row = copy.copy(rows[index])
            if isinstance(row, dict):
                row["user_order_id"] = row["remark"] = identity[2]
            else:
                row.user_order_id = identity[2]
                if hasattr(row, "remark"):
                    row.remark = identity[2]
            recovered.append((index, row))
        for index, row in recovered:
            rows[index] = row
        return rows
    except Exception as exc:
        _warn("Order remark recovery unavailable; native remarks retained", exc)
        return rows
