#coding:gbk
import importlib
import os
import sys


def _known_qmt_python_dir():
    root = "".join(chr(value) for value in (0x56fd, 0x91d1, 0x8bc1, 0x5238))
    suffix = "".join(chr(value) for value in (0x4ea4, 0x6613, 0x7aef))
    return "D:\\" + root + "QMT" + suffix + "\\python"


_qmt_paths = []
_file_name = globals().get("__file__", "")
if _file_name:
    _qmt_paths.append(os.path.dirname(os.path.abspath(_file_name)))
_qmt_paths.append(_known_qmt_python_dir())

for _qmt_path in _qmt_paths:
    if _qmt_path and os.path.isdir(_qmt_path) and _qmt_path not in sys.path:
        sys.path.insert(0, _qmt_path)

try:
    print("[bigqmt_shell] reload entry paths=%s" % _qmt_paths)
except Exception:
    pass


def _fallback_account_id():
    for _name in ("BIGQMT_ACCOUNT_ID", "account", "account_id", "accountID"):
        _value = globals().get(_name)
        if _value:
            return str(_value)
    return ""

try:
    import bigqmt_signal_trader.adapters.redis_common as _redis_common
    _redis_common = importlib.reload(_redis_common)
except Exception as _redis_common_err:
    try:
        print("[bigqmt_shell] redis_common reload failed: %s" % _redis_common_err)
    except Exception:
        pass

try:
    import bigqmt_signal_trader.redis_rpc as _redis_rpc
    _redis_rpc = importlib.reload(_redis_rpc)
except Exception as _redis_rpc_err:
    try:
        print("[bigqmt_shell] redis_rpc reload failed: %s" % _redis_rpc_err)
    except Exception:
        pass

try:
    import bigqmt_signal_trader_strategy as _strategy
    try:
        _strategy.reset_app()
    except Exception:
        pass
    _strategy = importlib.reload(_strategy)
except Exception:
    pass

import bigqmt_signal_trader_redis_rpc_runtime as _runtime
_runtime = importlib.reload(_runtime)

def _load_local_config():
    import bigqmt_signal_trader_local_config as _local_config
    return importlib.reload(_local_config)


try:
    _local_config = _load_local_config()
    BIGQMT_REDIS_CONFIG = getattr(_local_config, "BIGQMT_REDIS_CONFIG", {})
    try:
        print("[bigqmt_shell] local redis config loaded keys=%s" % sorted((BIGQMT_REDIS_CONFIG or {}).keys()))
    except Exception:
        pass
    _runtime.configure_runtime_redis(BIGQMT_REDIS_CONFIG)
except Exception as _redis_cfg_err:
    try:
        print("[bigqmt_shell] local redis config load failed: %s" % _redis_cfg_err)
    except Exception:
        pass

try:
    _local_config = _load_local_config()
    BIGQMT_ACCOUNT_ID = getattr(_local_config, "BIGQMT_ACCOUNT_ID", "")
    try:
        print("[bigqmt_shell] local account config loaded=%s" % bool(BIGQMT_ACCOUNT_ID))
    except Exception:
        pass
    _runtime.configure_runtime_account(BIGQMT_ACCOUNT_ID)
except Exception as _account_cfg_err:
    try:
        print("[bigqmt_shell] local account config load failed: %s" % _account_cfg_err)
    except Exception:
        pass
    _account_id = _fallback_account_id()
    if _account_id:
        try:
            print("[bigqmt_shell] fallback account loaded=True")
        except Exception:
            pass
        _runtime.configure_runtime_account(_account_id)

try:
    _runtime.bind_runtime_api(
        passorder_func=passorder,
        cancel_func=cancel,
        get_trade_detail_data_func=get_trade_detail_data,
    )
except NameError:
    pass

init = _runtime.init
handlebar = _runtime.handlebar
adjust = _runtime.adjust
order_callback = _runtime.order_callback
deal_callback = _runtime.deal_callback
