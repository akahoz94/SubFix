"""慢的原因要在开跑那一刻就说：批大小决策进进度流 + 进诊断，数字全是实测值。

Item 2 的硬要求：
  · 决策点立即播一条（每轮一次，显存格局变了才再播，不许逐窗刷屏）
  · 只播代码已经量到的数字：别人占的/空闲/整卡/批大小，没有编出来的 ETA
  · 同一份决策要活过 sanitize 白名单，截图运行日志就能定罪"为什么这次特别慢"
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("asr_batch_decision", ROOT / "subfix_asr_transcribe.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["asr_batch_decision"] = mod
spec.loader.exec_module(mod)

GIB = 1024 ** 3
BATCH_STAGE = "inference_batch_size"


class FakeTorch:
    """够用的 torch 替身：mem_get_info 给空闲/整卡，memory_* 给自占，全按 GB 传。

    reserved ⊇ allocated 是真实关系（缓存分配器先向驱动申请整块，再从里面切给
    tensor）。这里必须照实建模：旧实现写 allocated + reserved，会算出 1.9 倍自占，
    把 others 压成 0——mock 若把 reserved 设成 0，两种写法读数一样，这个 bug
    就永远测不出来。
    """

    def __init__(self, free_gb, total_gb, ours_gb, available=True):
        self.cuda = types.SimpleNamespace(
            is_available=lambda: available,
            mem_get_info=lambda: (int(free_gb * GIB), int(total_gb * GIB)),
            memory_allocated=lambda: int(ours_gb * GIB * 0.9),
            memory_reserved=lambda: int(ours_gb * GIB),
            OutOfMemoryError=type("OutOfMemoryError", (RuntimeError,), {}),
            empty_cache=lambda: None,
        )


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    mod._AUTO_BATCH_STATE.clear()
    mod._PROGRESS_CONTEXT = None
    mod._PROGRESS_STAGE_TIMES.clear()
    monkeypatch.delenv("SUBFIX_QWEN3_ASR_MAX_BATCH", raising=False)
    yield
    mod._AUTO_BATCH_STATE.clear()
    mod._PROGRESS_CONTEXT = None


@pytest.fixture
def progress(tmp_path, monkeypatch):
    """先写一条真实批次进度，再把 write_payload 装成录音笔：决策条数可直接数。"""
    path = tmp_path / "progress.json"
    recorded: list[dict] = []
    real_write = mod.write_payload

    def record(target, payload):
        recorded.append(dict(payload))
        real_write(target, payload)

    monkeypatch.setattr(mod, "write_payload", record)
    mod.write_progress(
        path, "generate_subtitle_batch", "批量识别 1/12｜窗口 1-20/120",
        batch_index=1, total_batches=12, progress_index=25, progress_total=100,
    )
    recorded.clear()
    return path, recorded


def _decide(model=None, *, free=2.7, total=8.0, ours=2.1, available=True):
    """others = 8 - 2.7 - 2.1 = 3.2GB（别人占的），走真实的读数→选档→播报链路。"""
    monkey_torch = FakeTorch(free, total, ours, available)
    previous = sys.modules.get("torch")
    sys.modules["torch"] = monkey_torch
    try:
        target = model if model is not None else types.SimpleNamespace(max_inference_batch_size=4)
        mod._apply_auto_batch_size(target)
    finally:
        if previous is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = previous
    return target


def test_batch_decision_announced_at_decision_time(progress):
    path, recorded = progress
    model = _decide()  # 8GB 卡、别人占 3.2GB → 保命档
    assert model.max_inference_batch_size == 1
    assert len(recorded) == 1, "决策播报必须是一条，不是每条进度都播"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == recorded[0]
    assert payload["stage"] == BATCH_STAGE
    message = payload["message"]
    assert message.startswith("推理批 1｜")
    assert "3.2GB" in message and "空闲 2.7GB" in message and "8GB 卡" in message
    assert "已降到保命档" in message
    # 进度条位置与批次上下文不许被决策条顶掉（Lua 每 200ms 读这些字段）
    assert payload["progress_index"] == 25 and payload["progress_total"] == 100
    assert payload["batch_index"] == 1 and payload["total_batches"] == 12
    # 没有编出来的 ETA：代码里量不出时长就不许承诺时长
    assert "秒" not in message and "预计" not in message and "ETA" not in message


def test_decision_written_once_per_window_batch(progress):
    _decide()
    _decide()  # 下一个窗口，显存格局没变
    _decide()
    assert len(progress[1]) == 1, "同一决策逐窗重播会刷屏"


def test_changed_vram_re_announces_the_new_decision(progress):
    _decide(free=7.4, ours=0.5)  # 干净桌面：别人只占 0.1GB → 批 2
    assert len(progress[1]) == 1
    model = _decide()  # 达芬奇 + 浏览器把卡挤满 → 批 1
    assert model.max_inference_batch_size == 1
    assert len(progress[1]) == 2, "显存格局变了却不吭声"
    message = progress[1][-1]["message"]
    assert message.startswith("推理批 1｜") and "3.2GB" in message and "已降到保命档" in message


def test_oom_history_downgrade_is_named(progress):
    mod._AUTO_BATCH_STATE["value"] = 1
    mod._AUTO_BATCH_STATE["oom_batch"] = 1  # 上一次真的显存溢出
    model = _decide(free=7.4, ours=0.5)  # 干净桌面本该给批 2
    assert model.max_inference_batch_size == 1
    message = progress[1][-1]["message"]
    assert "溢出" in message
    assert mod._AUTO_BATCH_STATE["batch_decision"]["inference_batch_capped_by"] == "oom_lesson"


def test_regular_downgrade_is_not_sticky(progress):
    """常规降档只代表"此刻挤"，不是"溢出过"：桌面松回来必须能爬回批 2。

    旧实现把会话内见过的低档当单向棘轮，一次早高峰读到别人占 3.2GB 就把后面
    几十个窗口全钉在批 1，40 分钟素材凭空多花近一倍时间——而中途显存早回落了。
    """
    model = types.SimpleNamespace(max_inference_batch_size=1)
    _decide(model=model)  # 别人占 3.2GB → 批 1（常规降档，没有溢出）
    assert model.max_inference_batch_size == 1
    assert "溢出" not in mod._AUTO_BATCH_STATE["batch_decision"]["inference_batch_message"]

    _decide(model=model, free=7.4, ours=0.5)  # 别人只剩 0.1GB，余量足够 → 爬回批 2
    assert model.max_inference_batch_size == 2
    decision = mod._AUTO_BATCH_STATE["batch_decision"]
    assert decision["inference_batch_capped_by"] == ""
    assert "溢出" not in decision["inference_batch_message"]


def test_climb_back_keeps_a_margin(progress):
    """回升要求比 2.0GB 拐点再宽松 0.2GB，否则在边界上逐窗抖动。"""
    model = types.SimpleNamespace(max_inference_batch_size=1)
    _decide(model=model)  # 别人占 3.2GB → 批 1
    _decide(model=model, free=6.0, ours=0.1)  # 别人占 1.9GB：过拐点但没余量
    assert model.max_inference_batch_size == 1
    decision = mod._AUTO_BATCH_STATE["batch_decision"]
    assert decision["inference_batch_capped_by"] == "climb_needs_margin"
    assert "余量不足" in decision["inference_batch_message"]


def test_oom_floor_still_blocks_climbing_back(progress):
    """真溢出过的档位仍是硬地板——放宽棘轮不许把 14 倍的口子重新打开。"""
    mod._AUTO_BATCH_STATE["oom_batch"] = 1
    model = types.SimpleNamespace(max_inference_batch_size=1)
    _decide(model=model, free=7.4, ours=0.5)  # 桌面干净到能开批 2
    assert model.max_inference_batch_size == 1
    assert mod._AUTO_BATCH_STATE["batch_decision"]["inference_batch_capped_by"] == "oom_lesson"


def test_batch_decision_rides_along_with_every_progress(progress):
    """决策条只活一瞬就被"转写 x/56"顶掉，之后每条进度都要带着它。"""
    path, _ = progress
    _decide()  # 别人占 3.2GB → 批 1
    mod.write_progress(path, "generate_subtitle_batch", "v5 Qwen 转写 40/56")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["message"] == "v5 Qwen 转写 40/56"
    assert payload["inference_batch_size"] == 1
    assert "3.2GB" in payload["inference_batch_message"]


def test_no_vram_reading_is_announced_not_silent(progress, monkeypatch):
    monkeypatch.setattr(mod, "_read_free_gb_before_model_load", lambda: None)
    model = _decide(available=False)
    assert model.max_inference_batch_size == 1
    message = progress[1][-1]["message"]
    assert "实时显存读数不可用" in message and "推理批 1" in message


def test_manual_env_override_is_announced(progress, monkeypatch):
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MAX_BATCH", "3")
    model = _decide()
    assert model.max_inference_batch_size == 3
    message = progress[1][-1]["message"]
    assert message.startswith("推理批 3｜") and "手动指定" in message
    assert mod._AUTO_BATCH_STATE["batch_decision"]["inference_batch_reason"] == "env_override"


def test_decision_survives_the_diagnostic_whitelist(tmp_path):
    _decide()
    decision = dict(mod._AUTO_BATCH_STATE["batch_decision"])
    assert decision["inference_batch_size"] == 1
    assert decision["inference_batch_gpu_gb"]["others"] == pytest.approx(3.2, abs=0.05)
    assert decision["inference_batch_gpu_gb"]["free"] == pytest.approx(2.7, abs=0.05)
    assert decision["inference_batch_gpu_gb"]["total"] == pytest.approx(8.0, abs=0.05)
    report = tmp_path / "diagnostic.json"
    args = types.SimpleNamespace(diagnostic_output=str(report))
    payload = {"ok": True, "backend": "qwen3_asr", "diagnostic": {"stage_elapsed_seconds": {"asr": 48.6}},
               "subtitle_rows": []}
    mod.write_generation_diagnostic(args, payload)
    saved = json.loads(report.read_text(encoding="utf-8"))["diagnostic"]
    for key in (
        "inference_batch_size",
        "inference_batch_reason",
        "inference_batch_message",
        "inference_batch_capped_by",
        "inference_batch_gpu_gb",
    ):
        assert key in saved, key
    assert saved["inference_batch_message"] == decision["inference_batch_message"]
    assert saved["stage_elapsed_seconds"]["asr"] == 48.6  # 计时仍在，白名单没被改窄


def test_stale_decision_does_not_leak_into_next_run(tmp_path, monkeypatch):
    """常驻服务里读不到显存的那一轮，不许把上一轮的陈旧数字播出去。"""
    _decide()  # 真读到 8GB 卡、别人占 3.2GB
    assert "gpu_gb" in mod._AUTO_BATCH_STATE
    path = tmp_path / "next.json"
    mod.write_progress(path, "generate_subtitle_batch", "批量识别 1/3", progress_index=25, progress_total=100)
    mod._AUTO_BATCH_STATE.pop("batch_reported", None)  # main() 每请求重播一次
    mod._AUTO_BATCH_STATE.pop("batch_decision", None)
    monkeypatch.setattr(mod, "_read_free_gb_before_model_load", lambda: None)
    _decide(available=False)
    message = json.loads(path.read_text(encoding="utf-8"))["message"]
    assert "实时显存读数不可用" in message
    assert mod._AUTO_BATCH_STATE["batch_decision"]["inference_batch_gpu_gb"] == {}
    assert "3.2GB" not in message and "2.7GB" not in message


def test_stages_and_batch_math_unchanged_by_the_announcement():
    """播报只加可见性：既有的选档数学与阶段机器码一字未改。"""
    assert mod.pick_inference_batch_size((0.0, 8.0)) == 2
    assert mod.pick_inference_batch_size((2.5, 8.0)) == 1
    assert mod.pick_inference_batch_size((0.1, 24.0)) == 4
    assert mod.pick_inference_batch_size(None) == 1
    source = (ROOT / "subfix_asr_transcribe.py").read_text(encoding="utf-8")
    for stage in ("load_qwen_model", "run_qwen_asr", "load_qwen_aligner", "qwen_align",
                  "generate_subtitle_batch", "segment_subtitles", "write_output", "done"):
        assert f'"{stage}"' in source, stage
