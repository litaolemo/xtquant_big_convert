# -*- coding: utf-8 -*-
"""Build BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py.

Reads every custom module used by BIGQMT_REDIS_DRYRUN.py
(bigqmt_signal_trader package + bigqmt_signal_trader_strategy +
bigqmt_signal_trader_redis_rpc_runtime) and embeds their sources
base64-encoded into a single self-contained strategy file that
never imports other custom modules from disk.
"""
import base64
import os

# This script lives in tools/ in a source checkout; a byte-identical copy ships
# inside the wheel as package data (bigqmt_signal_trader/_singlefile/) so
# bigqmt-init's single-file options work from a plain pip install too.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.environ.get(
    "BIGQMT_BUILD_OUT", os.path.join(ROOT, "src", "BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py"))


def resolve_source_dirs(root=ROOT):
    """(top_dir, package_dir): top_dir holds the top-level strategy modules
    (bigqmt_signal_trader_strategy.py, bigqmt_signal_trader_redis_rpc_runtime.py)
    and is the root for the embedded relative paths; package_dir is the
    bigqmt_signal_trader package.

    A repository checkout (root/src/...) wins. Otherwise fall back to an
    installed package: this is the pip-install layout, where the top-level
    modules sit beside the package directory.
    """
    repo_src = os.path.join(root, "src")
    if os.path.isdir(os.path.join(repo_src, "bigqmt_signal_trader")):
        return repo_src, os.path.join(repo_src, "bigqmt_signal_trader")
    try:
        import bigqmt_signal_trader as _pkg
        package_dir = os.path.dirname(os.path.abspath(_pkg.__file__))
    except Exception:
        package_dir = ""
    top_dir = os.path.dirname(package_dir) if package_dir else ""
    if (top_dir
            and os.path.isfile(os.path.join(top_dir, "bigqmt_signal_trader_strategy.py"))
            and os.path.isfile(os.path.join(top_dir, "bigqmt_signal_trader_redis_rpc_runtime.py"))):
        return top_dir, package_dir
    raise SystemExit(
        "cannot locate bigqmt_signal_trader sources: neither a repository "
        "checkout (src/bigqmt_signal_trader under the builder root) nor an "
        "installed package with its top-level strategy modules was found")


# Setup-time tooling, not runtime code. Keeping it out is not just about size:
# it imports subprocess and getpass, and this project has already been bitten by
# QMT's import whitelist (socket, and logging.handlers -> socket indirectly).
# An unimported module's imports never execute, so this is belt-and-braces --
# but the belt is cheap.
EXCLUDED_MODULES = ("init_config.py",)


def _assert_unused(pkg_dir, excluded):
    """Refuse to exclude a module something else imports.

    A previous attempt at trimming this build excluded a module that turned out
    to be imported at the top level of another one, and the build died on load
    rather than at build time.
    """
    stems = [name[:-3] for name in excluded]
    for dirpath, _dirnames, filenames in os.walk(pkg_dir):
        for fn in sorted(filenames):
            if not fn.endswith(".py") or fn in excluded:
                continue
            with open(os.path.join(dirpath, fn), "rb") as f:
                text = f.read().decode("utf-8", "replace")
            for stem in stems:
                if ("import %s" % stem) in text or ("from .%s" % stem) in text:
                    raise SystemExit(
                        "refusing to exclude %s.py: %s imports it" % (stem, fn))


def collect_package(pkg_dir, root, excluded=EXCLUDED_MODULES):
    _assert_unused(pkg_dir, excluded)
    sources = {}
    for dirpath, _dirnames, filenames in os.walk(pkg_dir):
        for fn in sorted(filenames):
            if not fn.endswith(".py") or fn in excluded:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            with open(full, "rb") as f:
                sources[rel] = f.read()
    return sources


def embed_block(sources, extra):
    lines = ["_EMBEDDED_SOURCES = {"]
    all_items = sorted(sources.items())
    all_items += sorted(extra.items())
    for rel, data in all_items:
        b64 = base64.b64encode(data).decode("ascii")
        chunks = [b64[i:i + 100] for i in range(0, len(b64), 100)]
        chunk_lines = "\n".join('        "%s"' % c for c in chunks)
        lines.append('    "%s": (' % rel)
        lines.append(chunk_lines)
        lines.append('    ),')
    lines.append("}")
    return "\n".join(lines)


def main():
    top_dir, package_dir = resolve_source_dirs()
    sources = collect_package(package_dir, top_dir)
    extra = {}
    for name in ("bigqmt_signal_trader_strategy.py",
                 "bigqmt_signal_trader_redis_rpc_runtime.py"):
        full = os.path.join(top_dir, name)
        with open(full, "rb") as f:
            extra[name] = f.read()

    embedded = embed_block(sources, extra)

    template = TEMPLATE.replace("__EMBEDDED_SOURCES_BLOCK__", embedded)

    total = sum(len(v) for v in sources.values()) + sum(len(v) for v in extra.values())
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(template)
    print("WROTE %s" % OUT_PATH)
    print("embedded files: %d package + %d top-level" % (len(sources), len(extra)))
    print("embedded raw bytes: %d" % total)


TEMPLATE = '''#coding:gbk
"""Single-file self-contained Big QMT Redis dry-run strategy.

All custom modules referenced by the original BIGQMT_REDIS_DRYRUN entry
are embedded into this file and loaded in-memory at runtime:

    * bigqmt_signal_trader package (all submodules)
    * bigqmt_signal_trader_strategy
    * bigqmt_signal_trader_redis_rpc_runtime
    * bigqmt_signal_trader_local_config  (generated from the config block below)

This file never imports any other custom module from disk. It only uses the
standard library plus third-party libraries (redis, zmq, pandas, ...). The
embedded loader below resolves relative imports against the in-memory sources,
so you can copy this one file to a QMT python directory and run it as a
strategy without shipping the package alongside.

Edit BIGQMT_ACCOUNT_ID / BIGQMT_REDIS_CONFIG below before running.
"""
import base64 as _base64
import builtins as _builtins
import importlib as _importlib
import os
import sys
import types


# =========================== config block ===========================
BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"

BIGQMT_ACCOUNT_TYPE = "STOCK"

BIGQMT_REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "",
    # Keep order RPC disabled unless you explicitly want remote order/cancel.
    "rpc_allow_order_methods": False,
    # Requests drain through QMT's run_time("adjust", ...) callback, on the main
    # strategy thread. get_trade_detail_data returns EMPTY off that thread, so
    # order/query methods must not be moved to a background thread.
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",
    "full_tick_cache_enabled": False,
    "full_tick_demand_ttl_seconds": 10,
    "full_tick_cache_ttl_seconds": 10,
    "full_tick_refresh_interval_seconds": 0.5,
    "full_tick_market_refresh_interval_seconds": 3,
    "full_tick_refresh_max_wall_seconds": 0.3,
    "full_tick_max_requests": 8,
    "download_jobs_enabled": False,
    "download_job_chunk_size": 10,
    "download_job_max_wall_seconds": 0.5,
    "download_job_ttl_seconds": 3600,
    "exec_events_enabled": True,
    "exec_events_debug_raw_fields": False,
}
# ===================================================================


# ========================= embedded sources ========================
__EMBEDDED_SOURCES_BLOCK__

# bigqmt_signal_trader_local_config is generated from the config block.
_EMBEDDED_SOURCES["bigqmt_signal_trader_local_config.py"] = _base64.b64encode(
    (
        (
            "# coding: utf-8\\n"
            "BIGQMT_ACCOUNT_ID = %r\\n"
            "BIGQMT_ACCOUNT_TYPE = %r\\n"
            "BIGQMT_REDIS_CONFIG = %r\\n"
        ) % (BIGQMT_ACCOUNT_ID, BIGQMT_ACCOUNT_TYPE, BIGQMT_REDIS_CONFIG)
    ).encode("utf-8")
).decode("ascii")
# ===================================================================


_LOCAL_ROOTS = (
    "bigqmt_signal_trader",
    "bigqmt_signal_trader_strategy",
    "bigqmt_signal_trader_redis_rpc_runtime",
    "bigqmt_signal_trader_local_config",
)
_ORIGINAL_IMPORT = _builtins.__import__
_ORIGINAL_IMPORT_MODULE = _importlib.import_module
_ORIGINAL_RELOAD = _importlib.reload

# QMT runs the strategy via exec, where __file__ may be absent; fall back safely.
_STRATEGY_FILE_DIR = os.path.dirname(os.path.abspath(globals().get("__file__", os.getcwd())))


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


def _find_local_source(name):
    relative = name.replace(".", "/")
    package_init = relative + "/__init__.py"
    if package_init in _EMBEDDED_SOURCES:
        return package_init, True
    module_file = relative + ".py"
    if module_file in _EMBEDDED_SOURCES:
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
    # Point __file__ under the strategy file dir so adapters that walk up the
    # tree (e.g. market_bigqmt locating the native xtdata SDK) still work when
    # this file lives inside a QMT python directory.
    module.__file__ = os.path.join(_STRATEGY_FILE_DIR, source_path.replace("/", os.sep))
    module.__package__ = name if is_package else name.rpartition(".")[0]
    if is_package:
        module.__path__ = [os.path.dirname(module.__file__)]
    module_builtins = dict(_builtins.__dict__)
    module_builtins["__import__"] = _local_import
    module.__dict__["__builtins__"] = module_builtins
    module.__dict__["__bigqmt_load_local_module"] = _load_local_module
    sys.modules[name] = module
    # QMT native allowlist rejects the root package eager exports.
    if name == "bigqmt_signal_trader":
        return module
    try:
        source = _base64.b64decode(_EMBEDDED_SOURCES[source_path])
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
    previous = sys.modules.get("bigqmt_signal_trader_strategy")
    reset = getattr(previous, "reset_app", None)
    if not callable(reset):
        return
    try:
        reset()
        print("[bigqmt_shell] previous rpc service stopped")
    except Exception as exc:
        print("[bigqmt_shell] previous rpc service stop failed: %s" % exc)


_stop_previous_rpc_service()
_clear_local_modules()
_importlib.import_module = _local_import_module
_importlib.reload = _local_reload
print("[bigqmt_shell] all-in-one strategy embedded sources=%d" % len(_EMBEDDED_SOURCES))


def _fallback_account_id():
    for name in ("BIGQMT_ACCOUNT_ID", "account", "account_id", "accountID"):
        value = globals().get(name)
        if value:
            return str(value)
    return ""


try:
    _local_import("bigqmt_signal_trader.adapters.redis_common", globals(), fromlist=("*",))
    _local_import("bigqmt_signal_trader.redis_rpc", globals(), fromlist=("*",))
    _strategy = _local_import("bigqmt_signal_trader_strategy", globals(), fromlist=("*",))
    _strategy.reset_app()
except Exception as bridge_preload_error:
    print("[bigqmt_shell] bridge preload failed: %s" % bridge_preload_error)

_runtime = _local_import("bigqmt_signal_trader_redis_rpc_runtime", globals(), fromlist=("*",))


def _load_local_config():
    return _local_import("bigqmt_signal_trader_local_config", globals(), fromlist=("*",))


try:
    _config = _load_local_config()
    BIGQMT_REDIS_CONFIG = getattr(_config, "BIGQMT_REDIS_CONFIG", {})
    print("[bigqmt_shell] local redis config loaded keys=%s" % sorted((BIGQMT_REDIS_CONFIG or {}).keys()))
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
    qmt_extra = {}
    for function_name in (
        "get_history_trade_detail_data", "get_value_by_order_id", "get_last_order_id",
        "get_ipo_data", "get_new_purchase_limit", "get_assure_contract",
        "get_enable_short_contract", "get_unclosed_compacts", "get_closed_compacts",
        "get_debt_contract", "get_option_subject_position", "get_comb_option",
        "get_hkt_exchange_rate",
    ):
        if function_name in globals():
            qmt_extra[function_name] = globals()[function_name]
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
# namespace of the file it mounted, so the single-file entry has to
# re-export it too or query_credit_account can never answer (#202).
credit_account_callback = _runtime.credit_account_callback
'''


if __name__ == "__main__":
    main()
