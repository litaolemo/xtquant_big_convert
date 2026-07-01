"""Optional xtquant import shim backed by Big QMT Redis RPC.

Put this package before the real xtquant package on PYTHONPATH only when the
caller intentionally wants Big QMT RPC compatibility.
"""

from . import xtconstant, xtdata, xttrader, xttype

__all__ = ["xtconstant", "xtdata", "xttrader", "xttype"]
