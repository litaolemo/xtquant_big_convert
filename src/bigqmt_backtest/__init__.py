"""Isolated ZMQ bridge for QMT-native and standalone backtests.

This package deliberately does not import ``bigqmt_signal_trader``.  The live
bridge and both backtest backends therefore have separate module state,
identities, and order gateways.  QMT-native mode never uses the local broker.
"""

from .client import BacktestZmqClient
from .data_feed import CsvBarFeed, InMemoryBarFeed
from .engine import BacktestConfig, BacktestEngine
from .protocol import BacktestBridgeProtocol


__all__ = [
    "BacktestBridgeProtocol",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestZmqClient",
    "CsvBarFeed",
    "InMemoryBarFeed",
]

__version__ = "1.0.0"
