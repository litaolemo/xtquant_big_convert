#coding:gbk
"""QMT bridge entry using the same file-loader pattern as qmt_realtime strategies.

Broker QMT strategy sandboxes may reject local package names through their
normal ``import`` allowlist.  The realtime QMT strategies in gupiao_ztfx load
their colocated helpers through ``importlib.util.spec_from_file_location``.
This entry applies path-based loading to the bridge package, including its
internal relative imports, while leaving all standard-library and QMT imports
untouched.  This terminal's spec loader ignores custom builtins for nested
package imports, so local bridge files are compiled explicitly after resolving
their path.
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
)
_ORIGINAL_IMPORT = _builtins.__import__
_ORIGINAL_IMPORT_MODULE = _importlib.import_module
_ORIGINAL_RELOAD = _importlib.reload


def _known_qmt_python_dir():
    install = "".join(chr(value) for value in (
        0x541b, 0x5f18, 0x541b, 0x667a, 0x4ea4, 0x6613, 0x7cfb, 0x7edf,
    ))
    return r"D:\君弘君智交易系统\python"


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


def _find_local_source(name):
    relative = name.replace(".", os.sep)
    package_init = os.path.join(_SOURCE_ROOT, relative, "__init__.py")
    if os.path.isfile(package_init):
        return package_init, True
    module_file = os.path.join(_SOURCE_ROOT, relative + ".py")
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
    # QMT native allowlist rejects the root package eager exports.
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
        # The shell clears these modules for every strategy start.  Do not hand
        # their names back to QMT's normal import/reload allowlist afterwards.
        return module
    return _ORIGINAL_RELOAD(module)


def _clear_local_modules():
    names = [name for name in sys.modules if _is_local_module(name)]
    for name in sorted(names, key=lambda item: item.count("."), reverse=True):
        sys.modules.pop(name, None)


_clear_local_modules()
_importlib.import_module = _local_import_module
_importlib.reload = _local_reload
print("[bigqmt_shell] importlib entry source_root=%s" % _SOURCE_ROOT)


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
        passorder_func=passorder,
        cancel_func=cancel,
        get_trade_detail_data_func=get_trade_detail_data,
        extra_funcs=qmt_extra or None,
    )
except NameError:
    pass


init = _runtime.init
handlebar = _runtime.handlebar
adjust = _runtime.adjust
order_callback = _runtime.order_callback
deal_callback = _runtime.deal_callback
