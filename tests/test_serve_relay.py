"""常驻 serve 转发：请求解析、排队不回退、拉起与超时回退分支。"""

import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("serve_tests", ROOT / "subfix_asr_transcribe.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class _FakeServe(threading.Thread):
    """最小 serve 替身：可控响应延迟与内容，记录收到的 argv。"""

    def __init__(self, delay=0.0, ok=True, error=None):
        super().__init__(daemon=True)
        self.delay = delay
        self.ok = ok
        self.error = error
        self.received = []
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(1)
        self.sock.settimeout(0.2)

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                buffer = b""
                while b"\n" not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                request = json.loads(buffer.decode("utf-8") or "{}")
                self.received.append(request)
                if self.delay:
                    self._stop.wait(self.delay)
                body = {"ok": self.ok} if self.ok else {"ok": False, "error": self.error}
                conn.sendall((json.dumps(body) + "\n").encode("utf-8"))
            except (OSError, ValueError):
                pass
            finally:
                conn.close()
        self.sock.close()

    def stop(self):
        self._stop.set()


@pytest.fixture
def serve_factory(tmp_path, monkeypatch):
    servers = []
    monkeypatch.setattr(mod, "_serve_ready_dir", lambda: tmp_path)

    def make(delay=0.0, ok=True, error=None, write_ready=True):
        server = _FakeServe(delay=delay, ok=ok, error=error)
        server.start()
        servers.append(server)
        if write_ready:
            (tmp_path / f"subfix_serve_{server.port}.json").write_text(
                json.dumps({"port": server.port, "pid": 0}), encoding="utf-8"
            )
        return server

    yield make
    for server in servers:
        server.stop()


def teardown_function():
    mod._AUTO_BATCH_STATE.clear()


def test_serve_request_sends_argv_and_parses_ok(serve_factory):
    server = serve_factory()
    assert mod._serve_request(["--mode", "transcribe"], "127.0.0.1", server.port) is True
    assert server.received == [{"argv": ["--mode", "transcribe"]}]


def test_serve_request_raises_on_error_response(serve_factory):
    server = serve_factory(ok=False, error="boom")
    with pytest.raises(RuntimeError, match="boom"):
        mod._serve_request(["--mode", "transcribe"], "127.0.0.1", server.port)


def test_relay_waits_for_busy_serve_instead_of_falling_back(serve_factory, monkeypatch):
    """serve 忙（响应慢）时必须排队等结果，绝不回退冷启动抢显存。"""
    server = serve_factory(delay=3.0)
    spawn_called = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: spawn_called.append(1))
    started = __import__("time").monotonic()
    assert mod.try_serve_relay(["--mode", "transcribe"]) is True
    assert __import__("time").monotonic() - started >= 2.9
    assert not spawn_called, "有就绪文件时不得拉起新 serve"
    assert server.received


def test_relay_spawns_serve_when_no_ready_file(serve_factory, monkeypatch, tmp_path):
    """无就绪文件：拉起 serve，等其写就绪文件后转发成功。"""
    server = serve_factory(delay=0.0, write_ready=False)  # 起着但不写就绪文件
    def fake_popen(cmd, **kwargs):
        # 模拟 serve 进程启动后写就绪文件
        (tmp_path / f"subfix_serve_{server.port}.json").write_text(
            json.dumps({"port": server.port, "pid": 0}), encoding="utf-8")
        return None
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("SUBFIX_SERVE_SPAWN_TIMEOUT", "15")
    assert mod.try_serve_relay(["--mode", "transcribe"]) is True
    assert server.received


def test_relay_falls_back_after_spawn_timeout(serve_factory, monkeypatch):
    """拉起 serve 后等待超时仍无就绪文件：回退普通流程（返回 False）。"""
    monkeypatch.setenv("SUBFIX_SERVE_SPAWN_TIMEOUT", "3")
    spawn_calls = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: spawn_calls.append(1))
    started = __import__("time").monotonic()
    result = mod.try_serve_relay([])
    assert result is False
    assert spawn_calls, "无就绪文件时应尝试拉起 serve"
    assert __import__("time").monotonic() - started < 20
