#coding:gbk
"""Isolated QMT backtest entry for external ZMQ strategies.

This file is ASCII-only. It loads only the bigqmt_backtest package and never
loads or mutates the live bridge package.
"""

import builtins as _builtins
import os
import sys
import types


BACKTEST_ZMQ_CONFIG = {
    "bind_endpoint": "tcp://127.0.0.1:16662",
    "run_id": "",
    "account_id": "",
    "account_type": "STOCK",
    "strategy_name": "ZMQ_BACKTEST",
    "combo_type": 1101,
    "quick_trade": 2,
    "market_price_type": 5,
    "limit_price_type": 11,
    "bar_wait_timeout_seconds": 60,
    "require_qmt_backtest": True,
}


_LOCAL_ROOT = "bigqmt_backtest"
_ORIGINAL_IMPORT = _builtins.__import__


def _known_qmt_python_dir():
    install = "".join(chr(value) for value in (
        0x541b, 0x5f18, 0x541b, 0x667a, 0x4ea4, 0x6613, 0x7cfb, 0x7edf,
    ))
    return "D:\\" + install + "\\python"


try:
    _SOURCE_ROOT = os.path.dirname(os.path.abspath(__file__))
except Exception:
    _SOURCE_ROOT = _known_qmt_python_dir()
if not _SOURCE_ROOT:
    _SOURCE_ROOT = _known_qmt_python_dir()


def _is_local(name):
    return name == _LOCAL_ROOT or name.startswith(_LOCAL_ROOT + ".")


def _resolve_name(name, module_globals, level):
    if not level:
        return name
    package = (module_globals or {}).get("__package__") or ""
    if not package:
        raise ImportError("relative import without package")
    for unused in range(level - 1):
        package = package.rsplit(".", 1)[0]
    return package + (("." + name) if name else "")


def _find_source(name):
    relative = name.replace(".", os.sep)
    package_init = os.path.join(_SOURCE_ROOT, relative, "__init__.py")
    if os.path.isfile(package_init):
        return package_init, True
    module_file = os.path.join(_SOURCE_ROOT, relative + ".py")
    if os.path.isfile(module_file):
        return module_file, False
    raise ModuleNotFoundError("local source not found: %s" % name, name=name)


def _load_local_module(name):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    source_path, is_package = _find_source(name)
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
    sys.modules[name] = module
    if name == _LOCAL_ROOT:
        return module
    try:
        with open(source_path, "rb") as source_file:
            source = source_file.read()
        exec(compile(source, source_path, "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    if "." in name:
        parent_name, child_name = name.rsplit(".", 1)
        setattr(_load_local_module(parent_name), child_name, module)
    return module


def _local_import(name, module_globals=None, module_locals=None, fromlist=(), level=0):
    absolute_name = _resolve_name(name, module_globals, level)
    if not _is_local(absolute_name):
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


for _name in sorted(
    [name for name in list(sys.modules) if _is_local(name)],
    key=lambda item: item.count("."),
    reverse=True,
):
    sys.modules.pop(_name, None)


_runtime = _load_local_module("bigqmt_backtest.qmt_runtime")
_runtime.configure(**BACKTEST_ZMQ_CONFIG)
_runtime.bind_qmt_api(
    passorder_func=globals().get("passorder") or getattr(_builtins, "passorder", None),
    cancel_func=globals().get("cancel") or getattr(_builtins, "cancel", None),
    get_trade_detail_data_func=(
        globals().get("get_trade_detail_data")
        or getattr(_builtins, "get_trade_detail_data", None)
    ),
)

init = _runtime.init
handlebar = _runtime.handlebar
order_callback = _runtime.order_callback
deal_callback = _runtime.deal_callback
stop = _runtime.stop
after_backtest = _runtime.after_backtest
