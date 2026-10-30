"""Qwen3 GGUF 混合推理后端：发现逻辑、payload 同构、auto 链接入与回退。

真实引擎的 e2e 用 test_qwen3_gguf_e2e.py 单独门控；这里全部走桩，
保证无模型环境也能验证接线。
"""

import importlib.util
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_core():
    spec = importlib.util.spec_from_file_location(
        "gguf_core_tests", ROOT / "subfix_asr_transcribe.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gguf_core_tests"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_gguf():
    spec = importlib.util.spec_from_file_location(
        "gguf_backend_tests", ROOT / "subfix_qwen3_gguf.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gguf_backend_tests"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def core():
    return _load_core()


@pytest.fixture()
def gguf():
    return _load_gguf()


@pytest.fixture()
def tmp_path():
    path = Path(tempfile.mkdtemp(prefix="subfix-gguf-test-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


# ---------- 静态接线 ----------


def test_auto_chain_prefers_gguf_before_torch(core):
    # 用户拍板：默认走速度（现场素材快 2-3 倍），接受方言/嘈杂现场的量化差异
    assert core.AUTO_TRANSCRIBE_BACKENDS[0] == "qwen3_gguf"
    assert "qwen3_asr" in core.AUTO_TRANSCRIBE_BACKENDS
    assert "doubao_asr" not in core.AUTO_TRANSCRIBE_BACKENDS
    assert "doubao_asr_v2" not in core.AUTO_TRANSCRIBE_BACKENDS


# ---------- 发现逻辑 ----------


def test_disabled_env_blocks_candidate(gguf, monkeypatch):
    monkeypatch.setenv(gguf.GGUF_DISABLE_ENV, "1")
    monkeypatch.setattr(gguf, "discover_model_dir", lambda: None, raising=False)
    assert gguf._engine_available() is False


def test_missing_model_dir_blocks_candidate(gguf, monkeypatch, tmp_path):
    monkeypatch.delenv(gguf.GGUF_DISABLE_ENV, raising=False)
    empty = tmp_path / "models" / gguf.MODEL_DIR_NAME
    empty.mkdir(parents=True)
    monkeypatch.setenv(gguf.GGUF_MODEL_DIR_ENV, str(empty))
    assert gguf._engine_available() is False


def test_complete_model_dir_is_candidate(gguf, monkeypatch, tmp_path):
    monkeypatch.delenv(gguf.GGUF_DISABLE_ENV, raising=False)
    model_dir = tmp_path / "m"
    model_dir.mkdir()
    for name in gguf.MODEL_FILES:
        (model_dir / name).write_bytes(b"x")
    monkeypatch.setenv(gguf.GGUF_MODEL_DIR_ENV, str(model_dir))
    assert gguf._engine_available() is True


def test_env_override_missing_dir_raises_clearly(gguf, monkeypatch):
    monkeypatch.setenv(gguf.GGUF_MODEL_DIR_ENV, str(Path("Z:/不存在")))
    with pytest.raises(RuntimeError, match="不存在"):
        gguf.discover_model_dir()


# ---------- payload 同构 ----------


class _FakeAlignItem:
    def __init__(self, text, start, end):
        self.text = text
        self.start_time = start
        self.end_time = end


class _FakeAlignment:
    def __init__(self, items):
        self.items = items


class _FakeResult:
    def __init__(self, text, items):
        self.text = text
        self.alignment = _FakeAlignment(items)


def test_alignment_words_conversion(gguf):
    result = _FakeResult(
        "你好世界。",
        [_FakeAlignItem("你", 0.0, 0.24), _FakeAlignItem("好", 0.24, 0.48)],
    )
    words = gguf._alignment_words(result)
    assert words == [
        {"text": "你", "start": 0.0, "end": 0.24},
        {"text": "好", "start": 0.24, "end": 0.48},
    ]


def test_payload_segments_carry_words(core, gguf, monkeypatch, tmp_path):
    wav = tmp_path / "a.wav"
    import wave

    with wave.open(str(wav), "wb") as h:
        h.setnchannels(1)
        h.setsampwidth(2)
        h.setframerate(16000)
        h.writeframes(b"\x00\x00" * 16000)

    words = [{"text": "你", "start": 0.0, "end": 0.24}]
    monkeypatch.setattr(
        gguf, "transcribe_payload", None, raising=False
    )  # 防误用：本测试直接调 _segments_from_words
    segments = gguf._segments_from_words("你好", words, wav)
    assert segments == [{"start": 0.0, "end": 0.24, "text": "你好", "words": words}]


def test_payload_without_words_falls_back_to_duration(core, gguf, tmp_path):
    wav = tmp_path / "b.wav"
    import wave

    with wave.open(str(wav), "wb") as h:
        h.setnchannels(1)
        h.setsampwidth(2)
        h.setframerate(16000)
        h.writeframes(b"\x00\x00" * 16000)
    segments = gguf._segments_from_words("你好", [], wav)
    assert len(segments) == 1
    assert segments[0]["start"] == 0.0
    assert segments[0]["end"] == pytest.approx(1.0)
    assert segments[0]["text"] == "你好"


# ---------- auto 链行为 ----------


def test_auto_candidate_gates_dispatch(core, monkeypatch):
    monkeypatch.setenv("SUBFIX_DISABLE_QWEN3_GGUF", "1")
    assert core.qwen3_gguf_auto_candidate() is False


def test_transcribe_with_backend_auto_falls_back_when_gguf_missing(
    core, monkeypatch, tmp_path
):
    called = {}

    fake_torch_payload = {"backend": "qwen3_asr", "text": "x", "segments": []}
    monkeypatch.setattr(
        core,
        "transcribe_qwen3_asr",
        lambda *a, **k: (called.__setitem__("torch", True), fake_torch_payload)[1],
        raising=True,
    )
    payload = core.transcribe_with_backend(tmp_path / "a.wav", "m", "zh", "auto")
    assert payload["backend"] == "qwen3_asr"
    assert called.get("torch") is True


def test_explicit_gguf_backend_dispatch(core, monkeypatch, tmp_path):
    payload_expected = {
        "backend": "qwen3_gguf",
        "text": "你好",
        "segments": [{"start": 0.0, "end": 0.5, "text": "你好", "words": []}],
    }
    monkeypatch.setattr(
        core, "transcribe_qwen3_gguf", lambda *a, **k: payload_expected, raising=True
    )
    payload = core.transcribe_with_backend(tmp_path / "a.wav", "m", "zh", "qwen3_gguf")
    assert payload["backend"] == "qwen3_gguf"


def test_v4_window_batch_gguf_branch(core, monkeypatch):
    args = types.SimpleNamespace(
        backend="qwen3_gguf", language="zh", hotword_context="", model="m"
    )
    sent = []

    def fake_gguf(path, language, context=None):
        sent.append(path)
        return {"backend": "qwen3_gguf", "text": "t", "segments": []}

    monkeypatch.setattr(core, "transcribe_qwen3_gguf", fake_gguf, raising=True)
    payloads, diagnostic = core.transcribe_v4_window_batch(
        [Path("a.wav"), Path("b.wav")], args
    )
    assert [p["backend"] for p in payloads] == ["qwen3_gguf", "qwen3_gguf"]
    assert diagnostic["asr_backend_used"] == "qwen3_gguf"
    assert len(sent) == 2


def test_v4_window_batch_auto_falls_back_on_gguf_error(core, monkeypatch):
    args = types.SimpleNamespace(
        backend="auto", language="zh", hotword_context="", model="m"
    )

    monkeypatch.setattr(core, "qwen3_gguf_auto_candidate", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("GGUF 引擎初始化失败")

    monkeypatch.setattr(core, "transcribe_qwen3_gguf", boom, raising=True)
    monkeypatch.setattr(
        core,
        "transcribe_qwen3_asr_batch",
        lambda paths, *a, **k: [
            {"backend": "qwen3_asr", "text": "t", "segments": []} for _ in paths
        ],
        raising=True,
    )
    payloads, diagnostic = core.transcribe_v4_window_batch([Path("a.wav")], args)
    assert payloads[0]["backend"] == "qwen3_asr"
    assert diagnostic["asr_backend_used"] == "qwen3_asr"


def test_v4_window_batch_explicit_gguf_error_propagates(core, monkeypatch):
    args = types.SimpleNamespace(
        backend="qwen3_gguf", language="zh", hotword_context="", model="m"
    )

    def boom(*a, **k):
        raise RuntimeError("GGUF 坏了")

    monkeypatch.setattr(core, "transcribe_qwen3_gguf", boom, raising=True)
    with pytest.raises(RuntimeError, match="GGUF 坏了"):
        core.transcribe_v4_window_batch([Path("a.wav")], args)


def test_align_retry_routes_to_gguf(core, monkeypatch, tmp_path):
    import subfix_qwen3_gguf as gguf_mod

    called = {}
    monkeypatch.setattr(
        gguf_mod,
        "align_items",
        lambda audio, text, language: called.setdefault("items", [{"text": "你"}]),
    )
    items = core.align_v4_retry_audio(tmp_path / "a.wav", "你", "zh", backend="qwen3_gguf")
    assert items == [{"text": "你"}]
