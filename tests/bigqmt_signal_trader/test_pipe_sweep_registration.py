"""#431：清扫存活快照与注册必须有同一线性化边界。"""
import threading
from unittest import mock

from bigqmt_signal_trader.transports.pipe_transport import NamedPipeTransport


def test_sweep_does_not_close_newly_registered_live_thread(monkeypatch):
    transport = NamedPipeTransport()
    dll = mock.Mock()
    transport._dll = lambda: dll
    original = threading.enumerate
    ready, release = threading.Event(), threading.Event()
    def register():
        with transport._server_lock:
            transport._client_threads[threading.current_thread().ident] = (321, object())
        ready.set()
        release.wait(timeout=3)
    worker = threading.Thread(target=register)
    def snapshot():
        live = original()
        worker.start()
        # 旧代码在锁外采样，新线程可在快照之后、清扫之前注册并被误关。
        # 同锁后注册会等待 sweep 完成；这里的超时只用于放行该正确调度。
        ready.wait(timeout=0.2)
        return live
    monkeypatch.setattr(threading, "enumerate", snapshot)
    try:
        transport._sweep_dead_thread_handles()
        assert ready.wait(timeout=2)
        dll.CloseHandle.assert_not_called()
        assert worker.ident in transport._client_threads
    finally:
        release.set()
        worker.join(timeout=3)
        transport.stop()
