"""Consumer regressions for a shared client cache; no terminal or broker calls."""
import multiprocessing
import os
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.local_cache import LocalMarketCache
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData


def _client(directory):
    data = BigQmtXtData.__new__(BigQmtXtData)
    data.client = SimpleNamespace(
        local_cache_config={"enabled": True, "dir": str(directory), "format": "pkl"},
        call=lambda *args, **kwargs: True,
    )
    data._cache_obj = None
    return data


@pytest.mark.parametrize("indexed", [True, False])
def test_narrow_query_keeps_unrequested_cached_fields(tmp_path, indexed):
    data = _client(tmp_path)
    stamp = "20260930150000"
    frame = pd.DataFrame({"open": [10.], "high": [11.], "low": [9.], "close": [10.5], "volume": [100.]}, index=[stamp])
    if not indexed:
        frame = frame.rename_axis("stime").reset_index()
    responses = [frame, frame[["close"]] if indexed else frame[["stime", "close"]]]
    data._get_market_data_ex_batch = lambda *args, **kwargs: {"X": responses.pop(0)}
    for fields in ([], ["close"]):
        data.get_market_data_ex(field_list=fields, stock_list=["X"], period="5m", subscribe=False,
                                backfill_pre_close=False, resynth_ongoing_multiday=False)
    out = data._local_cache().read("X", "5m")
    assert out.iloc[0]["open"] == 10.
    assert out.iloc[0]["volume"] == 100.


def test_partial_update_preserves_only_omitted_columns(tmp_path):
    cache = LocalMarketCache(str(tmp_path), fmt="pkl")
    cache.write("X", "5m", pd.DataFrame({"open": [10.], "close": [10.5], "volume": [100.]}, index=["20260930150000"]))
    cache.write("X", "5m", pd.DataFrame({"close": [10.6], "volume": [float("nan")]}, index=["20260930150000"]))
    out = cache.read("X", "5m").iloc[0]
    assert out["open"] == 10.
    assert out["close"] == 10.6
    assert pd.isna(out["volume"])  # An explicitly supplied null is not an omitted field.


def _writer(directory, stamp, entered, release, fmt):
    cache = LocalMarketCache(directory, fmt=fmt)
    read = cache._read_file

    def held_read(path):
        frame = read(path)
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test reader was not released")
        return frame

    cache._read_file = held_read
    cache.write("X", "5m", pd.DataFrame({"close": [11.]}, index=[stamp]))


@pytest.mark.parametrize("formats", [("pkl", "pkl"), ("pkl", "parquet")])
def test_two_processes_keep_both_history_updates(tmp_path, formats):
    if "parquet" in formats:
        pytest.importorskip("pyarrow")
    cache = LocalMarketCache(str(tmp_path), fmt="pkl")
    cache.write("X", "5m", pd.DataFrame({"close": [10.]}, index=["20260930093000"]))
    context = multiprocessing.get_context("spawn")
    first, second, release = context.Event(), context.Event(), context.Event()
    workers = [context.Process(target=_writer, args=(str(tmp_path), stamp, entered, release, fmt))
               for stamp, entered, fmt in (("20260930093500", first, formats[0]), ("20260930094000", second, formats[1]))]
    try:
        workers[0].start()
        assert first.wait(10)
        workers[1].start()
        # Old code lets both readers capture the same old frame. Correct code
        # blocks the second read until the first complete merge is committed.
        second.wait(1)
        release.set()
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert list(cache.read("X", "5m").index) == ["20260930093000", "20260930093500", "20260930094000"]
    finally:
        release.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            if worker.pid:
                worker.join(5)


def test_download_cache_poll_does_not_create_implicit_subscription(tmp_path):
    data = _client(tmp_path)
    calls = []

    def query(**kwargs):
        calls.append(kwargs)
        return {"X": pd.DataFrame({"close": [10.]}, index=["20260930150000"])}

    data.get_market_data_ex = query
    data.download_history_data2(["X"], "5m", "20260930", "20260930", data_wait_seconds=0)
    assert calls and all(call.get("subscribe") is False for call in calls)


@pytest.mark.parametrize("rebuild", [False, True])
@pytest.mark.parametrize("subscribe", [False, True, None])
def test_internal_daily_reads_preserve_subscription_choice(tmp_path, monkeypatch, rebuild, subscribe):
    import datetime
    from bigqmt_signal_trader import xtquant_compat

    data = _client(tmp_path)
    calls = []
    weekly = pd.DataFrame({"open": [10.], "high": [11.], "low": [9.], "close": [10.5],
                           "volume": [100.], "amount": [1000.], "preClose": [10. if rebuild else 0.]},
                          index=["20260913"])
    daily = weekly.copy()
    daily.index = ["20260908"]
    daily["preClose"] = 9.9

    def query(params, **kwargs):
        calls.append(params)
        return {"X": weekly if params["period"] == "1w" else daily}

    monkeypatch.setattr(xtquant_compat, "_now", lambda: datetime.datetime(2026, 9, 8, 13, 30))
    data._get_market_data_ex_batch = query
    data.get_market_data_ex(stock_list=["X"], period="1w", start_time="20260907", end_time="20260908",
                            subscribe=subscribe, resynth_ongoing_multiday=rebuild)
    assert [call["period"] for call in calls] == ["1w", "1d"]
    assert all(call.get("subscribe") is subscribe for call in calls)


def test_cache_only_hit_and_miss_never_call_terminal(tmp_path):
    data = _client(tmp_path)
    data.client.local_cache_config["fallback_rpc"] = False
    data._local_cache().write("X", "5m", pd.DataFrame({"close": [10.]}, index=["20260930150000"]))

    def forbidden(*args, **kwargs):
        pytest.fail("cache-only reads must not contact or subscribe to QMT")

    data.client.call = forbidden
    result = data.get_local_data(stock_list=["X", "missing"], period="5m")
    assert set(result) == {"X"}


def test_automatic_cache_write_failure_is_logged_without_losing_rpc_data(tmp_path, caplog):
    data = _client(tmp_path)
    frame = pd.DataFrame({"close": [10.]}, index=["20260930150000"])
    data._get_market_data_ex_batch = lambda *a, **k: {"X": frame}

    def failed(*args, **kwargs):
        raise OSError("disk full")

    data._local_cache().write = failed
    result = data.get_market_data_ex(stock_list=["X"], period="5m", subscribe=False,
                                     backfill_pre_close=False, resynth_ongoing_multiday=False)
    assert result["X"] is frame
    assert "local cache write failed code=X period=5m: disk full" in caplog.text
