"""Deterministic historical bar feeds for the backtest bridge."""

import csv
import datetime as dt
import hashlib
import io
import json
import os
import threading

from .models import normalize_symbol


DATETIME_FIELDS = ("datetime", "timestamp", "time", "date", "stime")
SYMBOL_FIELDS = ("symbol", "stock_code", "code", "stock")
REQUIRED_PRICE_FIELDS = ("open", "high", "low", "close")


def _first(row, names, default=None):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def parse_datetime(value):
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    text = str(value or "").strip()
    if not text:
        raise ValueError("bar datetime is required")
    if text.isdigit():
        if len(text) == 8:
            return dt.datetime.strptime(text, "%Y%m%d")
        if len(text) == 14:
            return dt.datetime.strptime(text, "%Y%m%d%H%M%S")
        numeric = int(text)
        if numeric > 10 ** 12:
            numeric = numeric / 1000.0
        return dt.datetime.fromtimestamp(numeric)
    normalized = text.replace("T", " ").replace("Z", "").strip()
    from_isoformat = getattr(dt.datetime, "fromisoformat", None)
    if from_isoformat is not None:
        try:
            return from_isoformat(normalized).replace(tzinfo=None)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    raise ValueError("unsupported bar datetime: %s" % text)


def _bool_value(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _optional_float(value):
    return None if value in (None, "") else float(value)


def normalize_bar(row, default_symbol=""):
    timestamp = parse_datetime(_first(row, DATETIME_FIELDS))
    symbol = normalize_symbol(_first(row, SYMBOL_FIELDS, default_symbol))
    bar = {
        "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
    }
    for field in REQUIRED_PRICE_FIELDS:
        value = row.get(field)
        if value in (None, ""):
            raise ValueError("%s is required for %s at %s" % (field, symbol, bar["datetime"]))
        bar[field] = float(value)
        if bar[field] <= 0:
            raise ValueError("%s must be positive for %s at %s" % (field, symbol, bar["datetime"]))
    if bar["high"] < max(bar["open"], bar["close"], bar["low"]):
        raise ValueError("bar high is inconsistent for %s at %s" % (symbol, bar["datetime"]))
    if bar["low"] > min(bar["open"], bar["close"], bar["high"]):
        raise ValueError("bar low is inconsistent for %s at %s" % (symbol, bar["datetime"]))
    bar["volume"] = float(row.get("volume") or 0)
    bar["amount"] = float(row.get("amount") or 0)
    bar["prev_close"] = _optional_float(row.get("prev_close"))
    bar["up_limit"] = _optional_float(row.get("up_limit"))
    bar["down_limit"] = _optional_float(row.get("down_limit"))
    bar["suspended"] = _bool_value(row.get("suspended"))
    if row.get("price_limit_rate") not in (None, ""):
        bar["price_limit_rate"] = float(row["price_limit_rate"])
    return timestamp, bar


class InMemoryBarFeed(object):
    def __init__(self, rows, source="memory", data_hash=None, default_symbol=""):
        normalized = []
        for row in rows:
            timestamp, bar = normalize_bar(dict(row), default_symbol=default_symbol)
            normalized.append((timestamp, bar))
        normalized.sort(key=lambda item: (item[0], item[1]["symbol"]))
        seen = set()
        frames = []
        current_timestamp = None
        current_bars = None
        previous_close = {}
        for timestamp, bar in normalized:
            identity = (timestamp, bar["symbol"])
            if identity in seen:
                raise ValueError("duplicate bar for %s at %s" % (bar["symbol"], bar["datetime"]))
            seen.add(identity)
            if bar["prev_close"] is None:
                bar["prev_close"] = previous_close.get(bar["symbol"])
            previous_close[bar["symbol"]] = bar["close"]
            if current_timestamp != timestamp:
                current_timestamp = timestamp
                current_bars = {}
                frames.append(
                    {
                        "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                        "bars": current_bars,
                    }
                )
            current_bars[bar["symbol"]] = bar
        if not frames:
            raise ValueError("historical data is empty")
        self._frames = frames
        self.source = str(source)
        if data_hash:
            self.data_hash = str(data_hash)
        else:
            payload = json.dumps(frames, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self.data_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def __len__(self):
        return len(self._frames)

    def frame(self, index):
        frame = self._frames[int(index)]
        return {"datetime": frame["datetime"], "bars": {key: dict(value) for key, value in frame["bars"].items()}}

    def history(self, symbol, end_index, count=100, fields=None):
        symbol = normalize_symbol(symbol)
        end_index = min(int(end_index), len(self._frames) - 1)
        count = max(int(count or 0), 0)
        result = []
        for index in range(0, end_index + 1):
            bar = self._frames[index]["bars"].get(symbol)
            if bar is None:
                continue
            if fields:
                item = {"datetime": bar["datetime"], "symbol": symbol}
                for field in fields:
                    if field in bar:
                        item[str(field)] = bar[field]
            else:
                item = dict(bar)
            result.append(item)
        return result[-count:] if count else []


class CsvBarFeed(InMemoryBarFeed):
    def __init__(self, path, default_symbol="", encoding="utf-8-sig"):
        absolute = os.path.abspath(path)
        with open(absolute, "rb") as handle:
            raw = handle.read()
        digest = hashlib.sha256(raw).hexdigest()
        text = raw.decode(encoding)
        reader = csv.DictReader(io.StringIO(text, newline=""))
        rows = list(reader)
        super(CsvBarFeed, self).__init__(
            rows,
            source=absolute,
            data_hash=digest,
            default_symbol=default_symbol,
        )


class StreamingBarFeed(object):
    """Thread-safe feed populated by QMT ``handlebar`` callbacks.

    The external strategy can only read through ``frame``/``history`` with an
    engine-controlled end index, so bars already captured from QMT but not yet
    advanced to remain inaccessible.
    """

    def __init__(self, source="qmt_native_backtest"):
        self.source = str(source)
        self._frames = []
        self._seen = set()
        self._previous_close = {}
        self._condition = threading.Condition()
        self.closed = False

    @property
    def data_hash(self):
        with self._condition:
            payload = json.dumps(
                self._frames,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def __len__(self):
        with self._condition:
            return len(self._frames)

    def append(self, row, default_symbol=""):
        timestamp, bar = normalize_bar(dict(row), default_symbol=default_symbol)
        identity = (timestamp, bar["symbol"])
        with self._condition:
            if identity in self._seen:
                return False
            if self.closed:
                raise RuntimeError("streaming feed is closed")
            self._seen.add(identity)
            if bar["prev_close"] is None:
                bar["prev_close"] = self._previous_close.get(bar["symbol"])
            self._previous_close[bar["symbol"]] = bar["close"]
            timestamp_text = timestamp.strftime("%Y-%m-%d %H:%M:%S")
            if self._frames and self._frames[-1]["datetime"] == timestamp_text:
                self._frames[-1]["bars"][bar["symbol"]] = bar
            elif self._frames and self._frames[-1]["datetime"] > timestamp_text:
                raise ValueError("streaming bars must be appended chronologically")
            else:
                self._frames.append({"datetime": timestamp_text, "bars": {bar["symbol"]: bar}})
            self._condition.notify_all()
        return True

    def close(self):
        with self._condition:
            self.closed = True
            self._condition.notify_all()

    def wait_for_index(self, index, timeout_seconds=None):
        index = int(index)
        with self._condition:
            if len(self._frames) > index:
                return True
            self._condition.wait_for(
                lambda: len(self._frames) > index or self.closed,
                timeout=timeout_seconds,
            )
            return len(self._frames) > index

    def frame(self, index):
        with self._condition:
            frame = self._frames[int(index)]
            return {
                "datetime": frame["datetime"],
                "bars": {key: dict(value) for key, value in frame["bars"].items()},
            }

    def history(self, symbol, end_index, count=100, fields=None):
        symbol = normalize_symbol(symbol)
        count = max(int(count or 0), 0)
        with self._condition:
            end_index = min(int(end_index), len(self._frames) - 1)
            frames = self._frames[: end_index + 1]
            result = []
            for frame in frames:
                bar = frame["bars"].get(symbol)
                if bar is None:
                    continue
                if fields:
                    item = {"datetime": bar["datetime"], "symbol": symbol}
                    for field in fields:
                        if field in bar:
                            item[str(field)] = bar[field]
                else:
                    item = dict(bar)
                result.append(item)
        return result[-count:] if count else []
