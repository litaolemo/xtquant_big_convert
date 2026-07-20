"""Example external moving-average strategy for the ZMQ backtest bridge."""

import argparse

from bigqmt_backtest.client import BacktestZmqClient
from bigqmt_backtest.strategy import ExternalStrategyRunner


class MovingAverageStrategy(object):
    def __init__(self, symbol, fast=5, slow=20):
        self.symbol = symbol
        self.fast = int(fast)
        self.slow = int(slow)
        self.sequence = 0

    def on_bar(self, context, bars):
        if self.symbol not in bars:
            return []
        rows = context.history(self.symbol, count=self.slow, fields=["close"])
        if len(rows) < self.slow:
            return []
        closes = [float(row["close"]) for row in rows]
        fast_value = sum(closes[-self.fast :]) / self.fast
        slow_value = sum(closes) / self.slow
        position = context.positions.get(self.symbol, {})
        quantity = int(position.get("quantity") or 0)
        available = int(position.get("available") or 0)
        self.sequence += 1
        if fast_value > slow_value and quantity == 0:
            return [
                {
                    "client_order_id": "ma-buy-%d" % self.sequence,
                    "symbol": self.symbol,
                    "side": "BUY",
                    "quantity": 100,
                    "order_type": "MARKET",
                }
            ]
        if fast_value < slow_value and available > 0:
            return [
                {
                    "client_order_id": "ma-sell-%d" % self.sequence,
                    "symbol": self.symbol,
                    "side": "SELL",
                    "quantity": available,
                    "order_type": "MARKET",
                }
            ]
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="tcp://127.0.0.1:16661")
    parser.add_argument("--run-id", default="", help="Optional; discovered from QMT when omitted")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--fast", type=int, default=5)
    parser.add_argument("--slow", type=int, default=20)
    args = parser.parse_args()
    with BacktestZmqClient(args.endpoint, args.run_id, client_id="ma-example") as client:
        result = ExternalStrategyRunner(
            client,
            MovingAverageStrategy(args.symbol, fast=args.fast, slow=args.slow),
        ).run()
    print(result)


if __name__ == "__main__":
    main()
