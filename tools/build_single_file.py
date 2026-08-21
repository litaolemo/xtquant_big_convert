# coding: utf-8
"""Generate a single-file, ZMQ-only build of the bridge (issue #56).

Some brokers (国泰) run QMT with a whitelist: the strategy cannot read files, and
it cannot import anything outside the sandbox. A package spread over 38 modules
is unusable there, so this emits one .py that carries the whole bridge.

Why generated rather than hand-written: a hand-maintained copy drifts from the
package within a release or two, and the drift is silent -- the single-file
users are exactly the ones who cannot diff against the original. Running this
from the real sources means the two cannot disagree.

Approach -- embed, do not flatten
---------------------------------
Concatenating modules into one namespace would collide on names that legitimately
repeat across modules (``_attr`` exists in three), and every relative import
would need rewriting. Instead each module's source is embedded verbatim and
registered in ``sys.modules`` before anything imports it, so ``from .code_utils
import x`` resolves exactly as it does on disk. The import system checks
``sys.modules`` first and never reaches for a finder.

That needs no ``importlib``, no ``__file__``, and no filesystem access -- only
``sys`` and ``types``.

Usage::

    python tools/build_single_file.py -o dist/BIGQMT_ZMQ_SINGLE.py
"""

import argparse
import ast
import io
import os
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
PACKAGE = "bigqmt_signal_trader"

# Modules to leave out. Deliberately short: every module in this package imports
# its third-party dependency lazily (redis, pymysql and pandas are all imported
# inside functions), so nothing has to be dropped merely for touching redis. An
# earlier version excluded everything matching "redis" by name and produced a
# build that died on import -- redis_rpc needs decode_text from redis_common,
# whose only top-level import is os.
#
# So the rule is: exclude a module only when it is useless INSIDE QMT, and let
# dependency discovery decide the rest.
EXCLUDED_MODULES = frozenset([
    # Manages the QMT terminal from the outside (process control, window
    # automation); meaningless from within it, and nothing imports it.
    "bigqmt_signal_trader.qmt_launcher",
])

# Modules the server entry needs. Everything they import is pulled in
# automatically, so this list only has to name the roots.
ENTRY_MODULES = (
    # The QMT-facing entry: init / handlebar / adjust / order_callback all live
    # here, in a top-level module beside the package rather than inside it.
    "bigqmt_signal_trader_strategy",
    "bigqmt_signal_trader.redis_rpc",
    "bigqmt_signal_trader.adapter_factory",
    "bigqmt_signal_trader.runner",
    "bigqmt_signal_trader.runtime_bigqmt",
    "bigqmt_signal_trader.transports.factory",
    "bigqmt_signal_trader.transports.zmq_transport",
)


class BuildError(RuntimeError):
    """The build cannot produce a correct single file."""


def module_path(name):
    """Locate a module under src/, package or top-level.

    The strategy entry (bigqmt_signal_trader_strategy) is a top-level module
    beside the package, not inside it, so a package-only lookup would miss it.
    """
    parts = name.split(".")
    base = os.path.join(SRC, *parts)
    if os.path.isdir(base):
        return os.path.join(base, "__init__.py"), True
    return base + ".py", False


def read_source(name):
    path, is_pkg = module_path(name)
    if not os.path.isfile(path):
        raise BuildError("module not found: %s (%s)" % (name, path))
    with io.open(path, encoding="utf-8") as handle:
        return handle.read(), is_pkg


def _resolve_relative(name, module_name, level, is_pkg):
    """Turn a relative import inside ``module_name`` into an absolute name."""
    base = module_name.split(".")
    if not is_pkg:
        base = base[:-1]          # a module resolves against its package
    if level > 1:
        base = base[:-(level - 1)]
    if not base:
        raise BuildError("relative import escapes the package in %s" % module_name)
    return ".".join(base + ([name] if name else []))


def intra_package_imports(source, module_name, is_pkg, top_level_only=False):
    """Names inside PACKAGE that ``source`` imports, relative or absolute.

    ``top_level_only`` restricts the scan to imports that run when the module
    body executes. That distinction decides exec ORDER: a lazy import inside a
    function does not constrain it, and treating one as if it did invents
    cycles. redis_rpc and redis_transport look mutually dependent, but
    redis_rpc's reference sits inside a function, so at runtime there is no
    cycle at all -- only the eager half is a real ordering constraint.
    """
    found = []
    nodes = ast.parse(source).body if top_level_only else ast.walk(ast.parse(source))
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            if node.level:
                target = _resolve_relative(node.module, module_name, node.level, is_pkg)
            elif node.module and node.module.split(".")[0] == PACKAGE:
                target = node.module
            else:
                continue
            found.append(target)
            # ``from .x import y`` where y is itself a module, not a name.
            for alias in node.names:
                found.append("%s.%s" % (target, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == PACKAGE:
                    found.append(alias.name)
    return found


def is_package_name(name):
    return name.split(".")[0] == PACKAGE


def module_exists(name):
    path, _ = module_path(name)
    return os.path.isfile(path)


def collect(entries):
    """Walk the import graph, returning ``[(name, source, is_pkg), ...]``.

    Order is dependency-first: a module is emitted only after everything it
    imports, because registration执行 the source immediately.
    """
    sources = {}
    packages = {}
    deps = {}
    skipped = set()

    def visit(name):
        if name in sources or name in skipped:
            return
        if name in EXCLUDED_MODULES:
            skipped.add(name)
            return
        if not module_exists(name):
            skipped.add(name)  # ``from .x import SomeClass`` -- a name, not a module
            return
        source, is_pkg = read_source(name)
        sources[name] = source
        packages[name] = is_pkg
        deps[name] = []
        # Collect from the whole tree so a lazily-imported module still gets
        # embedded, but constrain ORDER only by the top-level imports.
        eager = set(intra_package_imports(source, name, is_pkg, top_level_only=True))
        for target in intra_package_imports(source, name, is_pkg):
            if target in EXCLUDED_MODULES:
                if target in eager and module_exists(target):
                    # Excluding a module someone imports at top level produces a
                    # build that dies on the first import. Refuse at build time
                    # rather than shipping it: the people who need this build
                    # cannot debug it inside their sandbox.
                    raise BuildError(
                        "%s imports excluded module %s at top level -- either drop the "
                        "exclusion or make the import lazy" % (name, target))
                continue
            if not module_exists(target):
                continue
            visit(target)
            if target in sources and target != name and target in eager:
                deps[name].append(target)
        # A submodule needs its parent package registered first.
        if "." in name:
            visit(name.rsplit(".", 1)[0])

    for entry in entries:
        visit(entry)

    ordered = []
    placed = set()
    visiting = set()

    def emit(name):
        if name in placed:
            return
        if name in visiting:
            # Import cycles are legal at runtime only when the cycle is resolved
            # lazily; a registry executes eagerly, so a cycle would break.
            raise BuildError("import cycle involving %s" % name)
        visiting.add(name)
        # A parent package is deliberately NOT an exec-order dependency. Python
        # itself only needs the parent's module OBJECT to exist before a child
        # imports, not its body to have run -- which is why ``__init__`` may
        # import its own children. _install_modules registers every module
        # object up front, so the same holds here; adding the edge would invent
        # a cycle out of every package.
        for dep in deps.get(name, ()):
            emit(dep)
        visiting.discard(name)
        placed.add(name)
        ordered.append(name)

    for name in sorted(sources):
        emit(name)
    return [(n, sources[n], packages[n]) for n in ordered], skipped


BOOTSTRAP = '''# coding: gbk
"""BigQMT ZMQ bridge -- single file build. DO NOT EDIT.

Generated by tools/build_single_file.py from the package sources; edit those
and rebuild. Built %(built)s from %(count)d modules.

For QMT sandboxes that forbid reading files and importing outside the whitelist
(issue #56). ZMQ transport only -- no redis, no mysql.

Load THIS file in the QMT strategy editor. It needs pyzmq in QMT's Python and a
config dict assigned to BIGQMT_CONFIG below.
"""

import sys as _sys
import types as _types

_MODULE_SOURCES = {}
_PACKAGE_FLAGS = {}
_MODULE_ORDER = []


class _EmbeddedLoader(object):
    """Loader that re-executes an embedded source, so reload() works."""

    def __init__(self, name):
        self._name = name

    def create_module(self, spec):
        return None            # use the default module object

    def exec_module(self, module):
        exec(compile(_MODULE_SOURCES[self._name], "<%%s>" %% self._name, "exec"),
             module.__dict__)

    def load_module(self, name=None):        # py2-era API, still consulted
        return _sys.modules[self._name]


class _EmbeddedSpec(object):
    """Minimal ModuleSpec stand-in. Built by hand rather than via importlib.util
    so the common path needs nothing beyond sys and types."""

    def __init__(self, name, is_pkg):
        self.name = name
        self.loader = _EmbeddedLoader(name)
        self.origin = "<bigqmt_single_file>"
        self.submodule_search_locations = [] if is_pkg else None
        self.parent = name if is_pkg else (name.rsplit(".", 1)[0] if "." in name else "")
        self.has_location = False
        self.cached = None


class _EmbeddedFinder(object):
    """Serve embedded modules to importlib.reload().

    Pre-populating sys.modules covers ordinary imports, but reload() ignores an
    existing __spec__ and calls _find_spec() again (see CPython importlib
    /__init__.py), so a module with no finder cannot be reloaded. The strategy
    reloads adapter_factory on every re-run, which QMT does without restarting
    the process -- so without this, a re-run either warns or silently keeps
    stale code.

    Registered on sys.meta_path; it answers only for embedded names and defers
    everything else.
    """

    @classmethod
    def find_spec(cls, name, path=None, target=None):
        if name in _MODULE_SOURCES:
            return _EmbeddedSpec(name, _PACKAGE_FLAGS[name])
        return None

    @classmethod
    def find_module(cls, name, path=None):   # legacy API, harmless to keep
        return _EmbeddedLoader(name) if name in _MODULE_SOURCES else None


def _install_finder():
    for _existing in _sys.meta_path:
        if getattr(_existing, "__name__", "") == "_EmbeddedFinder":
            return
    _sys.meta_path.insert(0, _EmbeddedFinder)


def _register(_name, _source, _is_pkg):
    """Record a module. Registration is deferred so order stays explicit."""
    _MODULE_SOURCES[_name] = _source
    _PACKAGE_FLAGS[_name] = _is_pkg
    _MODULE_ORDER.append(_name)


def _install_modules():
    """Publish every embedded module into sys.modules, in dependency order.

    Nothing is read from disk and importlib is never touched: the import system
    checks sys.modules before looking for a finder, so a pre-populated entry
    satisfies ``from .x import y`` exactly the way the on-disk package does.

    Re-running the strategy re-executes this file; modules are rebuilt rather
    than reused so edits to a rebuilt single file take effect without a
    terminal restart.
    """
    _install_finder()
    for _name in _MODULE_ORDER:
        _module = _types.ModuleType(_name)
        _module.__file__ = "<bigqmt_single_file>"
        _is_pkg = _PACKAGE_FLAGS[_name]
        _module.__package__ = _name if _is_pkg else (
            _name.rsplit(".", 1)[0] if "." in _name else "")
        if _is_pkg:
            _module.__path__ = []
        # A loader that re-executes the embedded source. Without a __spec__,
        # importlib.reload() raises "spec not found" -- the strategy reloads
        # adapter_factory on every re-run, so the sandbox would show a scary
        # (though harmless) error line each time. Giving reload something that
        # works keeps that path behaving as it does on disk.
        _module.__spec__ = _EmbeddedSpec(_name, _is_pkg)
        _module.__loader__ = _module.__spec__.loader
        _sys.modules[_name] = _module
    for _name in _MODULE_ORDER:
        _module = _sys.modules[_name]
        exec(compile(_MODULE_SOURCES[_name], "<%%s>" %% _name, "exec"), _module.__dict__)


'''


ENTRY_TEMPLATE = '''

# ---------------------------------------------------------------------------
# QMT entry
# ---------------------------------------------------------------------------
# Fill this in before running. Kept as a literal rather than read from a config
# file because the sandbox this build targets cannot open files.
BIGQMT_CONFIG = {
    "account_id": "",                      # 资金账号 -- 必填
    "transport": "zmq",                    # 本构建只支持 zmq
    "zmq": {"host": "127.0.0.1"},          # 端口默认由 account_id 派生
    "rpc_allow_order_methods": False,      # 下单默认关闭
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": True,        # zmq 必须自带线程
    "schedule_adjust": True,
    "schedule_adjust_interval": "200nMilliSecond",
}

_install_modules()

_strategy = _sys.modules["%(strategy_module)s"]


def init(ContextInfo):
    _strategy.configure(**_qmt_config())
    _bind_native_functions()
    return _strategy.init(ContextInfo)


def _qmt_config():
    config = dict(BIGQMT_CONFIG)
    account_id = str(config.get("account_id") or "")
    if not account_id:
        raise ValueError("BIGQMT_CONFIG['account_id'] is required")
    _strategy.set_account_id(account_id)
    return config


def _bind_native_functions():
    """Hand QMT's injected globals to the strategy.

    passorder / cancel / get_trade_detail_data exist only inside the QMT
    process, so each lookup is guarded: outside QMT this file still imports.
    """
    resolved = {}
    for _fname in ("passorder", "cancel", "get_trade_detail_data",
                   "get_history_trade_detail_data", "get_value_by_order_id",
                   "get_last_order_id", "get_ipo_data", "get_new_purchase_limit",
                   "get_assure_contract", "get_enable_short_contract",
                   "get_unclosed_compacts", "get_closed_compacts",
                   "get_debt_contract", "get_option_subject_position",
                   "get_comb_option", "get_hkt_exchange_rate", "down_history_data"):
        try:
            resolved[_fname] = globals()[_fname]
        except KeyError:
            continue
    _strategy.bind_qmt_api(
        passorder_func=resolved.pop("passorder", None),
        cancel_func=resolved.pop("cancel", None),
        get_trade_detail_data_func=resolved.pop("get_trade_detail_data", None),
        extra_funcs=resolved,
    )
    print("[bigqmt_single] bound %%d native function(s)" %% (len(resolved) + 3))


handlebar = _strategy.handlebar
adjust = _strategy.adjust
order_callback = _strategy.order_callback
deal_callback = _strategy.deal_callback
stop = getattr(_strategy, "stop", None)
'''


def build(entries, strategy_module):
    modules, skipped = collect(entries)
    if not modules:
        raise BuildError("no modules collected")

    out = io.StringIO()
    out.write(BOOTSTRAP % {"built": time.strftime("%Y-%m-%d %H:%M"),
                           "count": len(modules)})
    for name, source, is_pkg in modules:
        # repr() rather than a triple-quoted literal: every module has a
        # docstring, so whichever delimiter were chosen would collide with the
        # source it is meant to wrap. Letting Python escape the string removes
        # the whole class of quoting bugs. The cost is an unreadable line, which
        # does not matter in a generated file nobody edits.
        out.write("_register(\n    %r,\n    %r,\n    %r,\n)\n\n"
                  % (name, source, is_pkg))
    out.write(ENTRY_TEMPLATE % {"strategy_module": strategy_module})
    return out.getvalue(), modules, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", default=os.path.join(ROOT, "dist", "BIGQMT_ZMQ_SINGLE.py"))
    parser.add_argument("--strategy-module", default="bigqmt_signal_trader_strategy",
                        help="module exposing init/handlebar/adjust")
    parser.add_argument("--check", action="store_true",
                        help="build and verify, but do not write")
    args = parser.parse_args(argv)

    try:
        text, modules, skipped = build(ENTRY_MODULES, args.strategy_module)
    except BuildError as exc:
        print("build failed: %s" % exc, file=sys.stderr)
        return 2

    try:
        ast.parse(text)
    except SyntaxError as exc:
        print("generated file is not valid python: %s" % exc, file=sys.stderr)
        return 3

    print("embedded %d modules (%d KB)" % (len(modules), len(text) // 1024))
    if skipped:
        print("excluded: %s" % ", ".join(sorted(n.split(".")[-1] for n in skipped)))
    if args.check:
        return 0

    directory = os.path.dirname(os.path.abspath(args.output))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(args.output, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("wrote %s" % args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
