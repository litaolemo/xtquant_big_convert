# coding: utf-8
"""Per-request account_type resolution from BIGQMT_ACCOUNT_TYPE_MAP.

By default, BigQmtOrderGateway and BigQmtPositionProvider use a fixed
account_type set at init time (``self.account_type = "STOCK"``). In a
single-account deployment this is correct. In a multi-account deployment
(one QMT process serving STOCK + FUTURE), the gateway is shared and
self.account_type must match the *request's* account_id, not the gateway's
default.

BIGQMT_ACCOUNT_TYPE_MAP is a dict {account_id: account_type} in the local
config. When it is present, account_type_for() returns the mapped type;
otherwise it falls back to the gateway's own default (the *default_type*
argument), preserving full backward compatibility.

One account, several types (港股通)
----------------------------------
A stock account trades 港股通 through the SAME account id, but the terminal
keeps those positions / orders / deals under a different account type:
``get_trade_detail_data(acc, 'HUGANGTONG', 'POSITION')``, not ``'STOCK'``.
An id -> one type table cannot say that, so a value may be a LIST::

    BIGQMT_ACCOUNT_TYPE = ["STOCK", "HUGANGTONG", "SHENGANGTONG"]
    BIGQMT_ACCOUNT_TYPE_MAP = {"123456": ["STOCK", "HUGANGTONG"]}

The first entry is the default. A request that names one of the others --
the client sends ``StockAccount(id, "HUGANGTONG").account_type`` as the
``account_type`` param -- is answered as that type for the duration of the
request (a thread-local, see request_account_type(); the adjust thread and
the heavy-read worker each carry their own). A type the account is not
configured for is ignored, exactly as before: the deployment's config
decides what the account trades as (#92), and the server logs the refusal
once per (account, type).

Usage in gateway methods::

    # Before (fixed account_type):
    rows = query(account_id, self.account_type, "ORDER", strategy_name)

    # After (per-request resolution):
    rows = query(account_id, self._resolve_account_type(account_id), "ORDER", strategy_name)
"""

import contextlib
import importlib
import threading


_ACCOUNT_TYPE_MAP = None  # None = not loaded yet; {} = loaded but empty
_PRIMARY_ID = ""          # BIGQMT_ACCOUNT_ID, when the local config has it
_PRIMARY_TYPES = []       # BIGQMT_ACCOUNT_TYPE as a list (empty unless it was a list)
_REQUEST = threading.local()
_refused_logged = set()

# xtconstant codes -> names, for a client that sends the number (StockAccount
# stores the code it was constructed with, not the name).
_TYPE_BY_CODE = {
    1: "FUTURE", 2: "STOCK", 3: "CREDIT", 5: "FUTURE_OPTION", 6: "STOCK_OPTION",
    7: "HUGANGTONG", 11: "SHENGANGTONG", 10: "NEW3BOARD",
}


def normalize_account_types(value):
    """``value`` as a list of upper-case type names, in order, no repeats.

    Accepts a name, a code, or a list/tuple of either; None and "" give [].
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        items = [value]
    out = []
    for item in items:
        text = str("" if item is None else item).strip()
        if not text:
            continue
        if text.isdigit():
            text = _TYPE_BY_CODE.get(int(text), "")
            if not text:
                continue
        text = text.upper()
        if text not in out:
            out.append(text)
    return out


def primary_account_type(value, default="STOCK"):
    """The default type out of a str-or-list config value."""
    types = normalize_account_types(value)
    return types[0] if types else str(default or "STOCK").strip().upper()


def _load_map():
    """Load BIGQMT_ACCOUNT_TYPE_MAP from local config, or {} if absent.

    Uses importlib.import_module (not reload) to avoid:
    1. Re-executing a config module that holds credentials.
    2. QMT sandbox monkeypatched importlib (issue noted in PR #135 review):
       the sandbox wraps importlib.reload with a custom loader that may fail
       on C++ callback threads with SystemError.
    Hot config changes should go through reload_deployment() → gateway init,
    which reconstructs the entire object graph.
    """
    global _ACCOUNT_TYPE_MAP, _PRIMARY_ID, _PRIMARY_TYPES
    try:
        cfg = importlib.import_module("bigqmt_signal_trader_local_config")
        raw = dict(getattr(cfg, "BIGQMT_ACCOUNT_TYPE_MAP", {}) or {})
        _ACCOUNT_TYPE_MAP = {}
        for key, value in raw.items():
            types = normalize_account_types(value)
            if types:
                _ACCOUNT_TYPE_MAP[str(key)] = types
        _PRIMARY_ID = str(getattr(cfg, "BIGQMT_ACCOUNT_ID", "") or "")
        primary = getattr(cfg, "BIGQMT_ACCOUNT_TYPE", None)
        # Only a LIST here adds types; a plain string is the runtime's
        # business (it resolves ACCOUNT_TYPE from three sources, #92).
        _PRIMARY_TYPES = (normalize_account_types(primary)
                          if isinstance(primary, (list, tuple)) else [])
    except Exception:
        _ACCOUNT_TYPE_MAP = {}
        _PRIMARY_ID = ""
        _PRIMARY_TYPES = []
    return _ACCOUNT_TYPE_MAP


def get_account_type_map():
    """The loaded map, loading it first if needed: {account_id: [types]}."""
    if _ACCOUNT_TYPE_MAP is None:
        _load_map()
    return _ACCOUNT_TYPE_MAP


def account_types_for(account_id, default_type="STOCK"):
    """Every type *account_id* may be addressed as; the first is the default.

    The map's entry when there is one; else the caller's default (a str or a
    list), extended by BIGQMT_ACCOUNT_TYPE's list for the primary account.
    """
    mapping = get_account_type_map()
    types = list(mapping.get(str(account_id or ""), [])) if account_id else []
    if types:
        return types
    types = normalize_account_types(default_type) or ["STOCK"]
    if _PRIMARY_TYPES and (not account_id or not _PRIMARY_ID
                           or str(account_id) == _PRIMARY_ID):
        for extra in _PRIMARY_TYPES:
            if extra not in types:
                types.append(extra)
    return types


def account_type_for(account_id, default_type="STOCK", requested=None):
    """account_type string for *account_id*, falling back to *default_type*.

    This is the core lookup: if BIGQMT_ACCOUNT_TYPE_MAP maps the account_id
    to a type, return it; otherwise return default_type unchanged.

    ``requested`` (or, when None, the type the current request named -- see
    request_account_type) wins when it is one of the account's configured
    types. Anything else is answered as the default, and said so once.
    """
    types = account_types_for(account_id, default_type)
    if requested is None:
        requested = get_request_account_type()
    wanted = normalize_account_types(requested)
    if wanted:
        if wanted[0] in types:
            return wanted[0]
        _note_refused(account_id, wanted[0], types)
    if not account_id:
        return types[0] if isinstance(default_type, (list, tuple)) else default_type
    mapping = get_account_type_map()
    if str(account_id) not in mapping and not _PRIMARY_TYPES:
        # No map, no list: exactly the old behaviour, default returned as-is.
        return default_type
    return types[0]


def _note_refused(account_id, wanted, types):
    key = (str(account_id or ""), wanted)
    if key in _refused_logged:
        return
    _refused_logged.add(key)
    try:
        from .logging_setup import get_logger
        get_logger("rpc").warning(
            "account_type %s requested for account %s***, which is configured "
            "as %s; answering as %s. Add it to BIGQMT_ACCOUNT_TYPE (a list) or "
            "BIGQMT_ACCOUNT_TYPE_MAP in the QMT-side local config and restart.",
            wanted, str(account_id or "")[:3], "/".join(types), types[0])
    except Exception:
        pass


# -- the type this request named --------------------------------------------

def get_request_account_type():
    """The account_type the request on THIS thread named, or None."""
    return getattr(_REQUEST, "account_type", None)


@contextlib.contextmanager
def request_account_type(value):
    """Scope ``value`` as the current request's account_type on this thread.

    Nested scopes restore the outer value. None / "" mean "unset" and are
    scoped too (a settlement pass for a STOCK order must not inherit the
    HUGANGTONG a previous request on the same thread named).
    """
    previous = getattr(_REQUEST, "account_type", None)
    _REQUEST.account_type = value if value not in (None, "") else None
    try:
        yield
    finally:
        _REQUEST.account_type = previous


def reload():
    """Force-reload the map (e.g. after config change)."""
    global _ACCOUNT_TYPE_MAP
    _ACCOUNT_TYPE_MAP = None
    _refused_logged.clear()
    return get_account_type_map()
