"""文稿直出的两段实测：先按 In/Out 截选区，再把行号抬到时间线绝对帧。

历史故障（两处 P0，都在这里钉住）：
  · 对齐用的是整条素材 → 时长/质量门量错，且 01:00:00:00 起的时间线里选区只是其中一段
  · build_direct_rows 忘了加 timeline_start_frame → 行号从 0 起，被核心层 scope 裁光

对齐器与 refine 是重依赖（模型/PCM 分析），这里用假对象替掉；
截取走真 ffmpeg、映射与质量门走真代码，所以断言的是链路契约而不是数学细节。
"""
import importlib.util
import sys
import time
import types
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_asr_module():
    name = "asr_direct_pipeline_probe"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "subfix_asr_transcribe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def make_wav(path: Path, seconds: float, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))


SCRIPT = "大家好，欢迎来到本期节目。\n今天我们聊性能优化。\n感谢观看，下期再见。"
BASE = 108000  # 达芬奇默认 01:00:00:00 @ 30fps
SOURCE_SECONDS = 20.0
SELECT_START = 2.0
SELECT_SECONDS = 10.0


class FakeAlignment:
    def __init__(self, items):
        self.items = items


class FakeItem:
    def __init__(self, text, start, end):
        self.text = text
        self.start_time = start
        self.end_time = end


class FakeAligner:
    """按文稿逐行给时间戳（每行 2s + 1s 间隔），足以通过质量门。"""

    def align(self, audio=None, text=None, language=None):
        script_text = text[0]
        lines = [line for line in script_text.splitlines() if line.strip()]
        items = []
        cursor = 1.0
        for line in lines:
            items.append(FakeItem(line, cursor, cursor + 2.0))
            cursor += 3.0
        return [FakeAlignment(items)]


def install_fake_qwen_asr(monkeypatch, seen):
    """try_script_direct_subtitles 内部 `from qwen_asr.inference.utils import normalize_audios`。"""
    utils = types.ModuleType("qwen_asr.inference.utils")

    def normalize_audios(paths):
        for path in paths:
            file_path = Path(str(path))
            seen.append(file_path)
            with wave.open(str(file_path), "rb") as handle:
                seen[-1] = (file_path, handle.getnframes() / float(handle.getframerate()))
        return [([0.0] * 16000, 16000)]

    utils.normalize_audios = normalize_audios
    inference = types.ModuleType("qwen_asr.inference")
    inference.utils = utils
    package = types.ModuleType("qwen_asr")
    package.inference = inference
    monkeypatch.setitem(sys.modules, "qwen_asr", package)
    monkeypatch.setitem(sys.modules, "qwen_asr.inference", inference)
    monkeypatch.setitem(sys.modules, "qwen_asr.inference.utils", utils)


def test_direct_output_cuts_selection_and_offsets_frames(tmp_path, monkeypatch, ):
    _run_direct_payload(tmp_path, monkeypatch, None)


def test_direct_output_honours_subtitle_length_cap(tmp_path, monkeypatch):
    payload, rows = _run_direct_payload(tmp_path, monkeypatch, 10)
    assert len(rows) > 3, f"长句应被拆细，实际 {len(rows)} 行"
    for row in rows:
        assert len("".join(str(row["text"]).split())) <= 10, row
    assert "".join(str(row["text"]) for row in rows) == SCRIPT.replace("\n", "")


def _run_direct_payload(tmp_path, monkeypatch, max_chars):
    asr = load_asr_module()
    try:
        ffmpeg = asr.resolve_ffmpeg(None)
    except Exception as exc:  # 没有 ffmpeg 的机器上不该判失败
        pytest.skip(f"本机没有可用 ffmpeg: {exc}")

    source = tmp_path / "source.wav"
    make_wav(source, SOURCE_SECONDS)
    script_file = tmp_path / "script.txt"
    script_file.write_text(SCRIPT, encoding="utf-8")

    seen: list = []
    install_fake_qwen_asr(monkeypatch, seen)
    monkeypatch.setattr(asr, "load_qwen3_forced_aligner", lambda: FakeAligner())
    # refine 需要 PCM/Silero，本用例只验截取与帧偏移
    monkeypatch.setattr(
        asr,
        "generate_v5",
        types.SimpleNamespace(
            refine_subtitle_boundaries=lambda rows, *a, **k: (rows, {}),
            preserve_refined_row_order=lambda rows: (rows, {}),
        ),
    )

    args = types.SimpleNamespace(
        script_file=str(script_file),
        script_direct_mode="direct",
        language="zh-CN",
        ffmpeg=None,
        generate_engine="v5",
        max_chars=max_chars,
    )
    batches = [{
        "audio": str(source),
        "fps": 30.0,
        "source_start": SELECT_START,
        "source_end": SELECT_START + SELECT_SECONDS,
        "timeline_start_frame": BASE,
        "timeline_end_frame": BASE + int(SELECT_SECONDS * 30),
        "track_index": 1,
        "batch_id": "b1",
    }]

    payload = asr.try_script_direct_subtitles(batches, args, tmp_path / "progress.json")
    assert payload is not None, getattr(args, "_direct_fail_reasons", None)

    assert seen, "对齐器没收到任何音频输入"
    cut_path, cut_seconds = seen[0]
    assert Path(cut_path).suffix == ".wav", str(cut_path)
    assert abs(cut_seconds - SELECT_SECONDS) < 0.2, f"截出来的选区应约 {SELECT_SECONDS}s，实际 {cut_seconds}s"
    assert not Path(cut_path).exists(), "直出临时目录应已清理"

    rows = payload["subtitle_rows"]
    frame_span = int(SELECT_SECONDS * 30)
    for row in rows:
        assert BASE <= row["start_frame"] < row["end_frame"] <= BASE + frame_span, row
    if max_chars is None:
        assert len(rows) == 3
        assert rows[0]["start_frame"] == BASE + 30, rows[0]
    return payload, rows


def test_script_far_longer_than_selection_skips_alignment(tmp_path, monkeypatch):
    """整篇稿 ≫ 选区时不许白跑一次对齐（实测那次 13.9 秒后必挂，直接走转录+校对）。"""
    asr = load_asr_module()
    monkeypatch.setattr(asr, "load_qwen3_forced_aligner",
                        lambda: (_ for _ in ()).throw(AssertionError("不该加载对齐器")))
    script = tmp_path / "script.txt"
    script.write_text(" filler。" * 400, encoding="utf-8")   # ~2400 字
    batch = {
        "audio": str(tmp_path / "missing.wav"), "fps": 25.0,
        "source_start": 12.08, "source_end": 19.04,
        "timeline_start_frame": 268, "track_index": 1, "batch_id": "b1",
    }
    args = types.SimpleNamespace(script_file=str(script), script_direct_mode="auto",
                                 language="zh-CN", ffmpeg=None, generate_engine="v5", max_chars=25)
    t0 = time.monotonic()
    out = asr.try_script_direct_subtitles([batch], args, None)
    elapsed = time.monotonic() - t0
    assert out is None
    assert args._direct_fail_reasons == ["SCRIPT_MUCH_LONGER_THAN_SELECTION"]
    assert elapsed < 1.0, f"应该秒退，实际 {elapsed:.1f}s"
