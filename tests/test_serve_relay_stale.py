"""常驻服务的就绪文件死了必须作废：否则转发方排队一小时不回退，也不再拉起新实例。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("asr_serve_relay", ROOT / "subfix_asr_transcribe.py")
asr = importlib.util.module_from_spec(spec)
sys.modules["asr_serve_relay"] = asr
spec.loader.exec_module(asr)


def _write_ready(tmp_path, port):
    asr._serve_ready_dir = lambda: tmp_path
    path = tmp_path / f"subfix_serve_{port}.json"
    path.write_text(json.dumps({"port": port, "pid": 999999, "warm_error": None}), encoding="utf-8")
    return path


def test_dead_ready_file_is_purged(tmp_path, monkeypatch):
    monkeypatch.setattr(asr, "_serve_port_listening", lambda port, host="127.0.0.1": False)
    path = _write_ready(tmp_path, 59999)
    assert asr._live_serve_ready_paths() == []
    assert not path.exists(), "死文件必须删掉，否则会一直挡住新服务拉起"


def test_live_ready_file_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(asr, "_serve_port_listening", lambda port, host="127.0.0.1": True)
    path = _write_ready(tmp_path, 59998)
    assert [p.name for p in asr._live_serve_ready_paths()] == [path.name]
    assert path.exists()


def test_garbage_ready_file_is_purged(tmp_path):
    asr._serve_ready_dir = lambda: tmp_path
    bad = tmp_path / "subfix_serve_broken.json"
    bad.write_text("{not json", encoding="utf-8")
    assert asr._live_serve_ready_paths() == []
    assert not bad.exists()
