# -*- coding: utf-8 -*-
"""Build BIGQMT_DRYRUN_NO_REDIS_FLAT_ALL_IN_ONE.py.

与 build_no_redis_single_file_plain.py 收集的模块完全一致，但内嵌源码以
【真实 Python 代码】形式写入文件（不是字符串、不是 base64）:

    每个模块的源码被缩进后放进一个 def _mod_N(): 函数体，
    运行时取该函数的 __code__ 在一个全新的模块 dict 中 exec:

        exec(func.__code__, module.__dict__)

这样生成的文件里可以直接搜索 / 阅读 / 修改内嵌源码 —— 类和函数就是
普通代码，IDE 可以高亮、跳转、缩进，不存在三引号转义问题。

实现要点（与纯字符串加载的差异）:
- 函数体 code 在 exec 时, LOAD_GLOBAL 查 module.__dict__（exec 的 globals）
- 【闭包陷阱】模块级名字如果被嵌套函数/类方法闭包引用，函数体编译器会
  把它变成 cell 变量（存于 _mod_N 函数帧），而模块代码里用 global 更新
  的是 module.__dict__ —— 两者不再同步（典型：redis_rpc_runtime 的
  RPC_TRANSPORT 被 configure_runtime_redis 更新后，_apply_config 闭包
  读到的仍是旧值 "redis"）。因此构建时用 AST 收集每个模块的顶层绑定名，
  在函数体开头注入 "global <names>"，让所有模块级名字都走 module dict，
  与真实模块语义完全一致（这也是 flat 版与字符串版行为一致的前提）。
- 缩进时用 tokenize 保护多行字符串内部行，避免改动字符串内容
- 模块级名字经 global 声明后直接写入模块 dict，无需 __export_module__
  回写；__export_module__ 仅保留给 local_config 的手工函数体使用
- 相对导入 (from .models import X) 走 _local_import 钩子, 依赖
  module.__dict__["__package__"], 与字符串版完全一致
- local_config 模块同样以真实代码函数体生成，值引用 _SHELL_CONFIG
"""
import ast
import io
import os
import tokenize

import build_single_file as bsf

# This script lives in tools/ in a source checkout; a byte-identical copy ships
# inside the wheel as package data so bigqmt-init works from a pip install too.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.environ.get(
    "BIGQMT_BUILD_OUT", os.path.join(ROOT, "src", "BIGQMT_DRYRUN_NO_REDIS_FLAT_ALL_IN_ONE.py"))

INDENT = "    "


def no_redis_override_path(root=ROOT):
    """Path to the self-contained zmq transport that replaces the redis one.

    Repository checkout first (bigqmt_no_redis/zmq_transport.py); a pip
    install ships the same file as package data, materialized to a temp dir.
    """
    repo_path = os.path.join(root, "bigqmt_no_redis", "zmq_transport.py")
    if os.path.isfile(repo_path):
        return repo_path
    try:
        import pkgutil
        import tempfile
        data = pkgutil.get_data(
            "bigqmt_signal_trader", "_singlefile/zmq_transport.py.txt")
    except Exception:
        data = None
    if data:
        tmpdir = tempfile.mkdtemp(prefix="bigqmt-flat-")
        path = os.path.join(tmpdir, "zmq_transport.py")
        with open(path, "wb") as handle:
            handle.write(data)
        return path
    raise SystemExit(
        "cannot locate bigqmt_no_redis/zmq_transport.py: neither the "
        "repository checkout nor the packaged copy inside "
        "bigqmt_signal_trader._singlefile was found")


def _decode(data):
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def indent_protected(text):
    """把模块源码缩进 INDENT 放进函数体，但保持多行字符串内部行原样，
    避免缩进改变字符串字面量的内容。"""
    lines = text.splitlines(keepends=True)
    keep_flat = set()  # 0-based 行号：多行字符串 token 的"非首行"都要保持原样
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except Exception:
        toks = []
    for tok in toks:
        if tok.type == tokenize.STRING and tok.start[0] != tok.end[0]:
            # 多行字符串：从第二行到末行都视为字符串内部（首行是代码+引号）
            s, e = tok.start[0] - 1, tok.end[0] - 1
            for ln in range(s + 1, e + 1):
                keep_flat.add(ln)
    out = []
    for i, line in enumerate(lines):
        if not line.strip():
            out.append(line)
        elif i in keep_flat:
            out.append(line)
        else:
            out.append(INDENT + line)
    return "".join(out)


def module_level_names(source):
    """AST 收集模块顶层绑定的名字。

    这些名字在 def _mod_N() 函数体里必须 global 化，否则被内层函数/类
    方法闭包引用时会被编译器变成 cell 变量，模块代码里 global 更新
    module.__dict__ 不会同步到 cell —— 造成模块级状态错位。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()

    def collect_target(node):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                collect_target(elt)
        elif isinstance(node, ast.Starred):
            collect_target(node.value)

    def walk_body(stmts):
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    collect_target(target)
            elif isinstance(node, ast.AnnAssign):
                collect_target(node.target)
            elif isinstance(node, ast.AugAssign):
                collect_target(node.target)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                collect_target(node.target)
                walk_body(node.body)
                walk_body(node.orelse)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        collect_target(item.optional_vars)
                walk_body(node.body)
            elif isinstance(node, ast.If):
                walk_body(node.body)
                walk_body(node.orelse)
            elif isinstance(node, ast.While):
                walk_body(node.body)
                walk_body(node.orelse)
            elif isinstance(node, ast.Try):
                walk_body(node.body)
                walk_body(node.orelse)
                for handler in node.handlers:
                    if handler.name:
                        names.add(handler.name)
                    walk_body(handler.body)
                walk_body(node.finalbody)
            elif isinstance(node, ast.Global):
                names.update(node.names)
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    collect_target(target)

    walk_body(tree.body)
    # 排除 shell 注入的名字（它们不在模块源码里，且不属于模块级绑定）
    for excluded in ("__export_module__", "_SHELL_CONFIG", "__builtins__"):
        names.discard(excluded)
    return names


def global_decl_lines(names, per_line=8):
    """把模块级名字分组生成 "global a, b, ..." 行（global 不支持换行）。"""
    out = []
    for i in range(0, len(names), per_line):
        chunk = ", ".join(sorted(names)[i:i + per_line])
        out.append(INDENT + "global " + chunk)
    return out


def module_funcs_block(sources, extra):
    """生成: 若干 def _mod_N(): 函数体 + _MODULE_FUNCS 映射表。"""
    funcs = []  # [ (index, rel, source_text) ]
    all_items = sorted(sources.items()) + sorted(extra.items())
    for idx, (rel, data) in enumerate(all_items):
        text = _decode(data).rstrip("\n")
        funcs.append((idx, rel, text))
    lines = []
    for idx, rel, text in funcs:
        lines.append("def _mod_%d():" % idx)
        try:
            has_stmt = bool(ast.parse(text).body)
        except SyntaxError:
            has_stmt = False
        if text.strip() and has_stmt:
            global_names = module_level_names(text)
            if global_names:
                lines.extend(global_decl_lines(global_names))
            lines.append(indent_protected(text))
        else:
            lines.append(INDENT + "pass")
        lines.append("")
    lines.append("")
    lines.append("_MODULE_FUNCS = {")
    for idx, rel, text in funcs:
        lines.append('    "%s": _mod_%d,' % (rel, idx))
    lines.append("}")
    return "\n".join(lines)


def main():
    top_dir, package_dir = bsf.resolve_source_dirs()
    sources = bsf.collect_package(package_dir, top_dir)

    with open(no_redis_override_path(), "rb") as f:
        override = f.read()
    sources["bigqmt_signal_trader/transports/zmq_transport.py"] = override

    extra = {}
    for name in ("bigqmt_signal_trader_strategy.py",
                 "bigqmt_signal_trader_redis_rpc_runtime.py"):
        full = os.path.join(top_dir, name)
        with open(full, "rb") as f:
            extra[name] = f.read()
    extra["bigqmt_no_redis/__init__.py"] = b"# coding: utf-8\n# no-redis local package marker (self-contained zmq transport lives here).\n"
    extra["bigqmt_no_redis/zmq_transport.py"] = override

    funcs_block = module_funcs_block(sources, extra)
    template = FLAT_TEMPLATE.replace("__MODULE_FUNCS_BLOCK__", funcs_block)

    total = sum(len(v) for v in sources.values()) + sum(len(v) for v in extra.values())
    with open(OUT_PATH, "w", encoding="gbk", newline="\n") as f:
        f.write(template)
    print("WROTE %s" % OUT_PATH)
    print("embedded files: %d package + %d top-level" % (len(sources), len(extra)))
    print("embedded raw bytes: %d (flat real code, not string/base64)" % total)


FLAT_TEMPLATE = '''#coding:gbk
"""Single-file self-contained Big QMT dry-run strategy (no-redis version).

FLAT build: the embedded sources are written inline as REAL Python code
(NOT strings, NOT base64). Each module source is indented inside a
``def _mod_N():`` body; at runtime its ``__code__`` is executed in a fresh
module namespace (``exec(func.__code__, module.__dict__)``).

So you can search / read / edit any embedded module as normal code, e.g.
bigqmt_signal_trader_redis_rpc_runtime, with IDE syntax highlighting and
indentation support, and no triple-quote escaping issues.

All custom modules referenced by bigqmt_no_redis/DRYRUN_no_redis.py are
embedded into this file and loaded in-memory at runtime:

    * bigqmt_signal_trader package (all submodules, with
      transports/zmq_transport overridden by the self-contained no-redis
      version that inlines the encoding helpers)
    * bigqmt_signal_trader_strategy
    * bigqmt_signal_trader_redis_rpc_runtime
    * bigqmt_signal_trader_local_config  (generated from the config block below)

The RPC transport is forced to ZMQ; the third-party redis package is never
imported and the zmq transport path never touches a redis-named module, so
this file loads cleanly in QMT sandboxes whose whitelist rejects redis.

This file never imports any other custom module from disk. It only uses the
standard library plus third-party libraries (zmq, pandas, ...). The embedded
loader below resolves relative imports against the in-memory modules, so you
can copy this one file to a QMT python directory and run it as a strategy
without shipping the package alongside.

Edit BIGQMT_ACCOUNT_ID / BIGQMT_REDIS_CONFIG below before running. The redis
connection fields in the config are ignored in no-redis mode.
"""
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
    # This build cannot import redis at all, so the redis block must not be
    # emitted -- otherwise every consumer dials a host that is not there
    # (issues #145 / #147).
    "redis_enabled": False,
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


# ========================= flat embedded modules ====================
# NOTE: real code. Each module source lives in a def _mod_N(): body below
# and is executed via its __code__ in a dedicated module namespace.
# Build-time AST analysis injects "global <name>" declarations at the top
# of every _mod_N() body so that module-level names resolve through the
# module dict exactly like a real module (no stale closure-cell snapshots).
_SHELL_CONFIG = {
    "BIGQMT_ACCOUNT_ID": BIGQMT_ACCOUNT_ID,
    "BIGQMT_ACCOUNT_TYPE": BIGQMT_ACCOUNT_TYPE,
    "BIGQMT_REDIS_CONFIG": BIGQMT_REDIS_CONFIG,
}

__MODULE_FUNCS_BLOCK__

def _mod_local_config():
    # Read straight from the shell config block and write into the module dict
    # via global. Do NOT rely on __export_module__/closure cells/module lookup:
    # QMT's per-process reload can leave those paths stale, silently yielding an
    # empty account and disabling the RPC service.
    global BIGQMT_ACCOUNT_ID, BIGQMT_ACCOUNT_TYPE, BIGQMT_REDIS_CONFIG
    BIGQMT_ACCOUNT_ID = _SHELL_CONFIG.get("BIGQMT_ACCOUNT_ID", "")
    BIGQMT_ACCOUNT_TYPE = _SHELL_CONFIG.get("BIGQMT_ACCOUNT_TYPE", "STOCK")
    BIGQMT_REDIS_CONFIG = _SHELL_CONFIG.get("BIGQMT_REDIS_CONFIG", {})

_MODULE_FUNCS["bigqmt_signal_trader_local_config.py"] = _mod_local_config
# ===================================================================


_LOCAL_ROOTS = (
    "bigqmt_signal_trader",
    "bigqmt_signal_trader_strategy",
    "bigqmt_signal_trader_redis_rpc_runtime",
    "bigqmt_signal_trader_local_config",
    "bigqmt_no_redis",
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
    if package_init in _MODULE_FUNCS:
        return package_init, True
    module_file = relative + ".py"
    if module_file in _MODULE_FUNCS:
        return module_file, False
    raise ModuleNotFoundError("local source not found: %s" % name, name=name)


def _set_parent_attribute(name, module):
    if "." not in name:
        return
    parent_name, child_name = name.rsplit(".", 1)
    parent = _load_local_module(parent_name)
    setattr(parent, child_name, module)


def _export_module(ns):
    """Only used by the hand-written _mod_local_config() body: its frame
    locals() are re-synced into the module namespace dict."""
    module = sys.modules.get(ns.get("__name__", ""))
    if module is None:
        return
    for key, value in ns.items():
        if key in ("__export_module__", "__builtins__"):
            continue
        if key.startswith("__") and key not in ("__name__", "__file__", "__package__", "__path__"):
            continue
        module.__dict__[key] = value


def _load_local_module(name):
    # Only reuse modules this file built itself. QMT reloads strategies in the
    # same process, so sys.modules may still hold a same-named module from a
    # previous load (e.g. an older bigqmt_signal_trader_local_config without
    # the account set). Reusing that stale module silently disables the RPC
    # service, so any module without our marker is rebuilt and replaced.
    existing = sys.modules.get(name)
    if existing is not None and getattr(existing, "__bigqmt_flat_builtin__", False):
        return existing
    source_path, is_package = _find_local_source(name)
    if "." in name:
        _load_local_module(name.rsplit(".", 1)[0])
    module = types.ModuleType(name)
    module.__bigqmt_flat_builtin__ = True
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
    module.__dict__["__export_module__"] = _export_module
    module.__dict__["_SHELL_CONFIG"] = _SHELL_CONFIG
    sys.modules[name] = module
    # QMT native allowlist rejects the root package eager exports.
    if name == "bigqmt_signal_trader":
        return module
    try:
        func = _MODULE_FUNCS[source_path]
        exec(func.__code__, module.__dict__)
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
print("[bigqmt_shell] all-in-one no-redis strategy flat modules=%d" % len(_MODULE_FUNCS))


def _fallback_account_id():
    for name in ("BIGQMT_ACCOUNT_ID", "account", "account_id", "accountID"):
        value = globals().get(name)
        if value:
            return str(value)
    return ""


try:
    _local_import("bigqmt_signal_trader.exec_events", globals(), fromlist=("*",))
except Exception as exec_events_preload_error:
    print("[bigqmt_shell] exec_events preload failed: %s" % exec_events_preload_error)
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
    # Build the local config module directly from the shell config block and
    # force it into sys.modules. This bypasses _mod_N() execution, closure
    # cells, __export_module__ and any stale same-named module a previous
    # strategy load may have left in the QMT process.
    account = _SHELL_CONFIG.get("BIGQMT_ACCOUNT_ID", "")
    redis_config = _SHELL_CONFIG.get("BIGQMT_REDIS_CONFIG", {})
    module = sys.modules.get("bigqmt_signal_trader_local_config")
    if module is None or not getattr(module, "__bigqmt_flat_builtin__", False):
        module = types.ModuleType("bigqmt_signal_trader_local_config")
        module.__bigqmt_flat_builtin__ = True
        sys.modules["bigqmt_signal_trader_local_config"] = module
    module.BIGQMT_ACCOUNT_ID = account
    module.BIGQMT_REDIS_CONFIG = redis_config
    if not account:
        print("[bigqmt_shell] WARN local account config empty (shell account=%r)" % (_SHELL_CONFIG.get("BIGQMT_ACCOUNT_ID"),))
    return module


try:
    _config = _load_local_config()
    BIGQMT_REDIS_CONFIG = getattr(_config, "BIGQMT_REDIS_CONFIG", {})
    # Force zmq transport (this is the no-redis version).
    BIGQMT_REDIS_CONFIG = dict(BIGQMT_REDIS_CONFIG or {})
    BIGQMT_REDIS_CONFIG["transport"] = "zmq"
    # setdefault: an explicit False is the #183 drain opt-in, worth 4-6x lower
    # latency on zmq, and assignment overwrote it (#188).
    BIGQMT_REDIS_CONFIG.setdefault("rpc_background_threads", True)
    # Nothing here can reach redis, so say so instead of letting the runtime
    # fill in 127.0.0.1:6379 from its defaults (issues #145 / #147).
    BIGQMT_REDIS_CONFIG["redis_enabled"] = False
    print("[bigqmt_shell] no-redis mode: transport=zmq background_threads=%s "
          "redis_enabled=False" % BIGQMT_REDIS_CONFIG["rpc_background_threads"])
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
