#coding:gbk
"""QMT bridge entry (no-redis version).

Same file-loader pattern as BIGQMT_REDIS_DRYRUN, but the RPC transport is ZMQ
only -- no redis imports anywhere. This version loads the no-redis zmq transport
(bigqmt_no_redis/zmq_transport.py) which inlines all encoding helpers and drops
redis-based service discovery, so it loads cleanly in QMT sandboxes that reject
`import redis` or any redis-named module.

Use this when your QMT environment cannot import the redis package (e.g. broker
whitelist blocks it) or when you want zero redis dependency.

Config: set "transport": "zmq" in bigqmt_signal_trader_local_config.py (the
no-redis runtime forces zmq regardless). Redis config fields are ignored --
this entry also forces redis_enabled=False, so nothing here ever dials redis.
That costs the strategy_name backfill on queries (issue #133), async download
jobs and the whole-quote snapshot cache; the last two are off by default on
Big QMT anyway. Order and trade callbacks are unaffected -- they take the zmq
push channel.
"""
import builtins as _builtins
import importlib as _importlib
import os
import sys
import types


_LOCAL_ROOTS = (
    "bigqmt_signal_trader",
    "bigqmt_signal_trader_strategy",
    "bigqmt_signal_trader_redis_rpc_runtime",
    "bigqmt_signal_trader_local_config",
    "bigqmt_no_redis",
)
# Multi-account (#261): copy this entry per account and add ONE line near the
# top of the copy --
#
#     BIGQMT_LOCAL_CONFIG_MODULE = "bigqmt_signal_trader_local_config_credit"
#
# -- and the named file is loaded UNDER the canonical name below, which the
# runtime imports literally and the strategy importlib.reloads. Never assign
# that global in this file: an entry that execs this source into its own
# globals would have its value overwritten.
_CANONICAL_LOCAL_CONFIG = "bigqmt_signal_trader_local_config"
# name -> source path, for a module loaded from a file not named after it.
_LOCAL_MODULE_SOURCE_OVERRIDES = {}
_ORIGINAL_IMPORT = _builtins.__import__
_ORIGINAL_IMPORT_MODULE = _importlib.import_module
_ORIGINAL_RELOAD = _importlib.reload


def _known_qmt_python_dir():
    # Find the QMT python dir from sys.path instead of a hardcoded path, so
    # the bridge loads regardless of broker install location or launch mode.
    for p in sys.path:
        if p and r"\python" in p and os.path.isdir(p):
            return p
    return ""


try:
    _SOURCE_ROOT = os.path.dirname(os.path.abspath(__file__))
except Exception:
    _SOURCE_ROOT = _known_qmt_python_dir()
if not _SOURCE_ROOT:
    _SOURCE_ROOT = _known_qmt_python_dir()


def _is_local_module(name):
    return any(name == root or name.startswith(root + ".") for root in _LOCAL_ROOTS)


def _resolve_name(name, module_globals, level):
    if not level:
        return name
    package = (module_globals or {}).get("__package__") or (module_globals or {}).get("__name__", "")
    if not package:
        raise ImportError("relative import without package")
    for unused in range(level - 1):
        if "." not in package:
            raise ImportError("relative import beyond top-level package")
        package = package.rsplit(".", 1)[0]
    return package + ("." + name if name else "")


def _local_source_dirs():
    dirs = []
    if _SOURCE_ROOT:
        dirs.append(_SOURCE_ROOT)
    for p in sys.path:
        if p and os.path.isdir(p) and p not in dirs:
            dirs.append(p)
    return dirs


def _find_local_source(name):
    override = _LOCAL_MODULE_SOURCE_OVERRIDES.get(name)
    if override:
        return override, False
    relative = name.replace(".", os.sep)
    for d in _local_source_dirs():
        package_init = os.path.join(d, relative, "__init__.py")
        if os.path.isfile(package_init):
            return package_init, True
        module_file = os.path.join(d, relative + ".py")
        if os.path.isfile(module_file):
            return module_file, False
    raise ModuleNotFoundError("local source not found: %s" % name, name=name)


def _set_parent_attribute(name, module):
    if "." not in name:
        return
    parent_name, child_name = name.rsplit(".", 1)
    parent = _load_local_module(parent_name)
    setattr(parent, child_name, module)


def _load_local_module(name):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    source_path, is_package = _find_local_source(name)
    if "." in name:
        _load_local_module(name.rsplit(".", 1)[0])
    module = types.ModuleType(name)
    module.__file__ = source_path
    module.__package__ = name if is_package else name.rpartition(".")[0]
    if is_package:
        module.__path__ = [os.path.dirname(source_path)]
    module_builtins = dict(_builtins.__dict__)
    module_builtins["__import__"] = _local_import
    module.__dict__["__builtins__"] = module_builtins
    module.__dict__["__bigqmt_load_local_module"] = _load_local_module
    sys.modules[name] = module
    if name == "bigqmt_signal_trader":
        return module
    try:
        with open(source_path, "rb") as source_file:
            source = source_file.read()
        exec(compile(source, source_path, "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    _set_parent_attribute(name, module)
    return module


def _local_import(name, module_globals=None, module_locals=None, fromlist=(), level=0):
    absolute_name = _resolve_name(name, module_globals, level)
    if not _is_local_module(absolute_name):
        return _ORIGINAL_IMPORT(name, module_globals, module_locals, fromlist, level)
    module = _load_local_module(absolute_name)
    for child in fromlist or ():
        if child != "*":
            try:
                _load_local_module(absolute_name + "." + child)
            except ModuleNotFoundError:
                pass
    if fromlist:
        return module
    return _load_local_module(absolute_name.split(".", 1)[0])


def _local_import_module(name, package=None):
    if _is_local_module(name):
        return _load_local_module(name)
    return _ORIGINAL_IMPORT_MODULE(name, package)


def _local_reload(module):
    if _is_local_module(getattr(module, "__name__", "")):
        return _load_local_module(module.__name__)
    return _ORIGINAL_RELOAD(module)


def _clear_local_modules():
    for name in list(sys.modules):
        if _is_local_module(name):
            sys.modules.pop(name, None)


def _stop_previous_rpc_service():
    """Release the previous QMT strategy's socket before clearing its module."""
    previous = sys.modules.get("bigqmt_signal_trader_strategy")
    reset = getattr(previous, "reset_app", None)
    if not callable(reset):
        return
    try:
        reset()
        print("[bigqmt_shell] previous rpc service stopped")
    except Exception as exc:
        print("[bigqmt_shell] previous rpc service stop failed: %s" % exc)


def _resolve_local_config_module():
    """Point the canonical config name at whichever module this entry names.

    Returns the module name that will actually be read. Must run after
    _clear_local_modules() (so the next load reads the chosen file) and before
    anything imports the config -- the runtime imports it at module load.
    """
    chosen = str(globals().get("BIGQMT_LOCAL_CONFIG_MODULE") or "").strip()
    if chosen.endswith(".py"):
        # A module name is what is wanted, but "...config_credit.py" is the
        # obvious slip and its intent is not ambiguous.
        chosen = chosen[:-3]
    _LOCAL_MODULE_SOURCE_OVERRIDES.pop(_CANONICAL_LOCAL_CONFIG, None)
    if not chosen or chosen == _CANONICAL_LOCAL_CONFIG:
        return _CANONICAL_LOCAL_CONFIG
    if os.sep in chosen or "/" in chosen or "." in chosen:
        raise RuntimeError(
            "BIGQMT_LOCAL_CONFIG_MODULE must be a module name, not a path: %r. "
            "Put the config file in the QMT python directory beside this entry "
            "and name the module, e.g. BIGQMT_LOCAL_CONFIG_MODULE = "
            "\"bigqmt_signal_trader_local_config_credit\"." % chosen)
    try:
        source_path, unused_is_package = _find_local_source(chosen)
    except ImportError:
        # Falling back to the default config would start this instance on
        # ANOTHER account -- the one failure mode worse than not starting.
        raise RuntimeError(
            "BIGQMT_LOCAL_CONFIG_MODULE = %r, but %s.py was not found. Looked "
            "in: %s. Create that file (copy "
            "bigqmt_signal_trader_local_config.example.py, set its "
            "BIGQMT_ACCOUNT_ID / BIGQMT_ACCOUNT_TYPE) into the QMT python "
            "directory. Not falling back to %s.py: that is the other account's "
            "config, and trading the wrong account is worse than not starting."
            % (chosen, chosen,
               ", ".join(_local_source_dirs()) or "(no directories)",
               _CANONICAL_LOCAL_CONFIG))
    _LOCAL_MODULE_SOURCE_OVERRIDES[_CANONICAL_LOCAL_CONFIG] = source_path
    return chosen


_stop_previous_rpc_service()
_clear_local_modules()
_importlib.import_module = _local_import_module
_importlib.reload = _local_reload
print("[bigqmt_shell] importlib entry source_root=%s" % _SOURCE_ROOT)
_LOCAL_CONFIG_MODULE = _resolve_local_config_module()
if _LOCAL_CONFIG_MODULE != _CANONICAL_LOCAL_CONFIG:
    print("[bigqmt_shell] local config module=%s file=%s" % (
        _LOCAL_CONFIG_MODULE,
        _LOCAL_MODULE_SOURCE_OVERRIDES.get(_CANONICAL_LOCAL_CONFIG, "")))


def _fallback_account_id():
    for name in ("BIGQMT_ACCOUNT_ID", "account", "account_id", "accountID"):
        value = globals().get(name)
        if value:
            return str(value)
    return ""


try:
    _local_import("bigqmt_signal_trader.adapters.market_bigqmt", globals(), fromlist=("*",))
    _local_import("bigqmt_signal_trader.adapters.order_bigqmt", globals(), fromlist=("*",))
    _local_import("bigqmt_signal_trader.adapters.position_bigqmt", globals(), fromlist=("*",))
    _strategy = _local_import("bigqmt_signal_trader_strategy", globals(), fromlist=("*",))
    _strategy.reset_app()
except Exception as bridge_preload_error:
    print("[bigqmt_shell] bridge preload failed: %s" % bridge_preload_error)

_runtime = _local_import("bigqmt_signal_trader_redis_rpc_runtime", globals(), fromlist=("*",))


def _load_local_config():
    # Always the canonical name: _resolve_local_config_module has already
    # pointed it at whichever file this entry names.
    return _local_import(_CANONICAL_LOCAL_CONFIG, globals(), fromlist=("*",))


try:
    _config = _load_local_config()
    BIGQMT_REDIS_CONFIG = getattr(_config, "BIGQMT_REDIS_CONFIG", {})
    # Force zmq transport (this is the no-redis version).
    BIGQMT_REDIS_CONFIG = dict(BIGQMT_REDIS_CONFIG or {})
    BIGQMT_REDIS_CONFIG["transport"] = "zmq"
    BIGQMT_REDIS_CONFIG["rpc_background_threads"] = True
    # And say so, rather than leaving the runtime to fill in 127.0.0.1:6379 from
    # its defaults. Without this the redis block is emitted anyway, exec events
    # prefer a client that can never connect, and every order/trade callback
    # times out while the zmq push channel sits idle (issues #145 / #147). This
    # file exists because the machine cannot import redis at all, so there is
    # nothing to weigh up here.
    BIGQMT_REDIS_CONFIG["redis_enabled"] = False
    print("[bigqmt_shell] no-redis mode: module=%s transport=zmq "
          "background_threads=True redis_enabled=False" % _LOCAL_CONFIG_MODULE)
    _runtime.configure_runtime_redis(BIGQMT_REDIS_CONFIG)
except Exception as redis_config_error:
    print("[bigqmt_shell] local redis config load failed: %s" % redis_config_error)

try:
    _config = _load_local_config()
    BIGQMT_ACCOUNT_ID = getattr(_config, "BIGQMT_ACCOUNT_ID", "")
    print("[bigqmt_shell] local account config loaded=%s" % bool(BIGQMT_ACCOUNT_ID))
    _runtime.configure_runtime_account(BIGQMT_ACCOUNT_ID)
except Exception as account_config_error:
    print("[bigqmt_shell] local account config load failed: %s" % account_config_error)
    account_id = _fallback_account_id()
    if account_id:
        _runtime.configure_runtime_account(account_id)

try:
    # QMT only injects its globals into the namespace of the file it mounts --
    # THIS one -- so the capture has to happen here, with this file's globals().
    # Read the strategy module's single source instead of hand-copying the
    # names: a copy here is how query_credit_account went missing on the redis
    # entry, and the bridge then reported it as "this terminal does not have
    # that function" when the terminal had it all along (#202).
    qmt_extra = _runtime.capture_qmt_injected_funcs(globals())
    print("[bigqmt_shell] down_history_data bound=%s" % ("down_history_data" in qmt_extra))
    _runtime.bind_runtime_api(
        passorder_func=globals().get("passorder"),
        cancel_func=globals().get("cancel"),
        get_trade_detail_data_func=globals().get("get_trade_detail_data"),
        extra_funcs=qmt_extra or None,
    )
except NameError:
    pass


init = _runtime.init
handlebar = _runtime.handlebar
adjust = _runtime.adjust
order_callback = _runtime.order_callback
deal_callback = _runtime.deal_callback
# Credit-account counter query callback. QMT only calls back into the
# namespace of the file it mounted, so this has to be re-exported here
# the same way order_callback / deal_callback are (#202).
credit_account_callback = _runtime.credit_account_callback
