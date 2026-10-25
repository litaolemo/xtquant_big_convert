"""可替换的大 QMT 信号下单包核心模块。"""

from .version import __version__, deployment_report

from .app import SignalTradingApp
from .models import (
    AccountSnapshot,
    AssetSnapshot,
    OrderRequest,
    OrderSubmitResult,
    PositionSnapshot,
    SignalAction,
    SignalStatus,
    TradeSignal,
)
from .option_analytics import (
    black_scholes_price,
    calculate_option_analytics,
    implied_volatility,
    option_greeks,
)
# python -m ...pipe_relay 也先执行包入口。提前导入客户端会初始化文件日志，
# 破坏中继的零文件 I/O（#420）。只延迟这三个便捷导出，实际使用时仍是同一类。
# QMT Python 3.6 没有模块 __getattr__，保留旧路径；flat loader 本就跳过根包。
import sys as _sys

if _sys.version_info < (3, 7):
    from .xtquant_compat import BigQmtRpcClient, BigQmtXtData, BigQmtXtTrader
else:
    def __getattr__(name):
        if name in ("BigQmtRpcClient", "BigQmtXtData", "BigQmtXtTrader"):
            from . import xtquant_compat
            value = getattr(xtquant_compat, name)
            globals()[name] = value
            return value
        raise AttributeError("module %r has no attribute %r" % (__name__, name))


__all__ = [
    "AccountSnapshot",
    "AssetSnapshot",
    "BigQmtRpcClient",
    "BigQmtXtData",
    "BigQmtXtTrader",
    "OrderRequest",
    "OrderSubmitResult",
    "PositionSnapshot",
    "SignalAction",
    "SignalStatus",
    "SignalTradingApp",
    "TradeSignal",
    "__version__",
    "black_scholes_price",
    "calculate_option_analytics",
    "implied_volatility",
    "option_greeks",
]
