"""自适应推理批大小：显存分档、env 覆盖、OOM 减半重试与会话内记忆。"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("auto_batch_tests", ROOT / "subfix_asr_transcribe.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class _FakeTorch:
    """最小 torch 替身：可控的 cuda 可用性、mem_get_info 与 OOM 类型。"""

    def __init__(self, free_bytes, available=True):
        self._free = free_bytes
        self.cuda = types.SimpleNamespace(
            is_available=lambda: available,
            mem_get_info=lambda: (free_bytes, 8 * 1024 ** 3),
            OutOfMemoryError=type("OutOfMemoryError", (RuntimeError,), {}),
            empty_cache=lambda: None,
        )


@pytest.fixture(autouse=True)
def fresh_state():
    mod._AUTO_BATCH_STATE.clear()
    yield
    mod._AUTO_BATCH_STATE.clear()


@pytest.fixture
def fake_torch(monkeypatch):
    def install(free_gb, available=True):
        fake = _FakeTorch(int(free_gb * 1024 ** 3), available)
        monkeypatch.setitem(sys.modules, "torch", fake)
        return fake
    return install


def test_tier_boundaries(monkeypatch):
    monkeypatch.delenv("SUBFIX_QWEN3_ASR_MAX_BATCH", raising=False)
    monkeypatch.setattr(mod, "_read_free_gb_before_model_load", lambda: None)
    assert mod.auto_inference_batch_size() == 1  # 读数失败（cuda 不可用等）→ 保命档
    for free_gb, expected in [(0.1, 1), (5.99, 1), (6.0, 2), (7.49, 2), (7.5, 4), (24.0, 4)]:
        monkeypatch.setattr(mod, "_read_free_gb_before_model_load", lambda v=free_gb: v)
        assert mod.auto_inference_batch_size() == expected, f"free={free_gb}GB"


def test_read_free_gb_caches_and_handles_failure(fake_torch):
    # cuda 不可用 → None → 缓存住，不再重读
    fake_torch(6.0, available=False)
    assert mod._read_free_gb_before_model_load() is None
    fake_torch(6.0, available=True)
    assert mod._read_free_gb_before_model_load() is None  # 缓存值
    # 正常路径
    mod._AUTO_BATCH_STATE.clear()
    fake_torch(7.0)
    first = mod._read_free_gb_before_model_load()
    assert first == pytest.approx(7.0, abs=0.01)
    fake_torch(0.1)
    assert mod._read_free_gb_before_model_load() == first  # 首读缓存，不随行情变


def test_mem_get_info_failure_falls_back_to_one(fake_torch, monkeypatch):
    monkeypatch.delenv("SUBFIX_QWEN3_ASR_MAX_BATCH", raising=False)
    fake = fake_torch(6.0)
    def boom():
        raise RuntimeError("驱动抽风")
    fake.cuda.mem_get_info = boom
    assert mod.auto_inference_batch_size() == 1


def test_env_overrides_everything(fake_torch, monkeypatch):
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MAX_BATCH", "3")
    fake_torch(0.1)
    model = types.SimpleNamespace(max_inference_batch_size=1)
    mod._apply_auto_batch_size(model)
    assert model.max_inference_batch_size == 3


def test_remembered_lesson_beats_tier(fake_torch, monkeypatch):
    monkeypatch.delenv("SUBFIX_QWEN3_ASR_MAX_BATCH", raising=False)
    mod._AUTO_BATCH_STATE["value"] = 1
    fake_torch(8.0)
    model = types.SimpleNamespace(max_inference_batch_size=4)
    mod._apply_auto_batch_size(model)
    assert model.max_inference_batch_size == 1


def test_oom_halves_batch_and_retries_once(fake_torch, monkeypatch):
    monkeypatch.delenv("SUBFIX_QWEN3_ASR_MAX_BATCH", raising=False)
    fake_torch(8.0)
    oom = _FakeTorch(0).cuda.OutOfMemoryError("CUDA out of memory")
    calls = []
    def fake_transcribe(qwen_model, audio, language, context):
        calls.append(qwen_model.max_inference_batch_size)
        if len(calls) == 1:
            raise oom
        return ["text"], "used"
    monkeypatch.setattr(mod, "_transcribe_qwen3_model", fake_transcribe)
    model = types.SimpleNamespace(max_inference_batch_size=4)
    results, status = mod._transcribe_with_oom_retry(model, ["a.wav"], "zh", None)
    assert results == ["text"]
    assert calls == [4, 2]
    assert model.max_inference_batch_size == 2
    assert mod._AUTO_BATCH_STATE["value"] == 2


def test_oom_at_batch_one_does_not_retry(fake_torch, monkeypatch):
    monkeypatch.delenv("SUBFIX_QWEN3_ASR_MAX_BATCH", raising=False)
    fake_torch(0.1)
    calls = []
    oom = _FakeTorch(0).cuda.OutOfMemoryError("CUDA out of memory")
    def fake_transcribe(qwen_model, audio, language, context):
        calls.append(qwen_model.max_inference_batch_size)
        raise oom
    monkeypatch.setattr(mod, "_transcribe_qwen3_model", fake_transcribe)
    model = types.SimpleNamespace(max_inference_batch_size=1)
    with pytest.raises(type(oom)):
        mod._transcribe_with_oom_retry(model, ["a.wav"], "zh", None)
    assert calls == [1]


def test_non_oom_error_is_not_retried(fake_torch, monkeypatch):
    monkeypatch.delenv("SUBFIX_QWEN3_ASR_MAX_BATCH", raising=False)
    fake_torch(8.0)
    calls = []
    def fake_transcribe(qwen_model, audio, language, context):
        calls.append(qwen_model.max_inference_batch_size)
        raise ValueError("普通错误")
    monkeypatch.setattr(mod, "_transcribe_qwen3_model", fake_transcribe)
    model = types.SimpleNamespace(max_inference_batch_size=4)
    with pytest.raises(ValueError):
        mod._transcribe_with_oom_retry(model, ["a.wav"], "zh", None)
    assert calls == [4]
