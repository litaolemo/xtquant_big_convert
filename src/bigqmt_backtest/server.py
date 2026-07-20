"""Command-line entry for the standalone ZMQ backtest bridge."""

import argparse
import json
import os
import sys
import uuid

from .data_feed import CsvBarFeed
from .engine import BacktestConfig, BacktestEngine
from .protocol import BacktestBridgeProtocol
from .zmq_server import ZmqBacktestServer


def _load_config(path):
    if not path:
        return {}
    with open(os.path.abspath(path), encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("config JSON must be an object")
    return payload


def _parser():
    parser = argparse.ArgumentParser(description="Standalone ZMQ backtest bridge")
    parser.add_argument("--data", required=True, help="UTF-8 CSV historical bar file")
    parser.add_argument("--config", default="", help="Optional UTF-8 JSON backtest config")
    parser.add_argument("--run-id", default="", help="Unique run identity")
    parser.add_argument("--output-dir", default="", help="Artifact directory")
    parser.add_argument("--bind", default="tcp://127.0.0.1:16661", help="ZMQ REP bind endpoint")
    parser.add_argument("--default-symbol", default="", help="Used when CSV has no symbol column")
    parser.add_argument("--initial-cash", type=float, default=None)
    parser.add_argument("--slippage-bps", type=float, default=None)
    parser.add_argument("--max-volume-participation", type=float, default=None)
    parser.add_argument("--keep-running", action="store_true", help="Do not stop server after finish")
    return parser


def build_engine(args):
    payload = _load_config(args.config)
    run_id = str(args.run_id or payload.pop("run_id", "") or ("bt-" + uuid.uuid4().hex[:12]))
    output_dir = args.output_dir or payload.pop("output_dir", "") or os.path.join("backtest_runs", run_id)
    if args.initial_cash is not None:
        payload["initial_cash"] = args.initial_cash
    if args.slippage_bps is not None:
        payload["slippage_bps"] = args.slippage_bps
    if args.max_volume_participation is not None:
        payload["max_volume_participation"] = args.max_volume_participation
    config = BacktestConfig(run_id=run_id, output_dir=output_dir, **payload)
    feed = CsvBarFeed(args.data, default_symbol=args.default_symbol)
    return BacktestEngine(feed, config)


def main(argv=None):
    args = _parser().parse_args(argv)
    engine = build_engine(args)
    server = ZmqBacktestServer(
        BacktestBridgeProtocol(engine),
        endpoint=args.bind,
        exit_on_finish=not args.keep_running,
    )
    startup = {
        "event": "backtest_bridge_starting",
        "run_id": engine.config.run_id,
        "bind": args.bind,
        "data_hash": engine.feed.data_hash,
        "output_dir": engine.config.output_dir,
        "live_ready": False,
    }
    print(json.dumps(startup, ensure_ascii=False, sort_keys=True), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
