"""文稿直出引擎：映射数学、质量门六项、帧换算。"""

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("script_direct_tests", ROOT / "subfix_script_direct.py")
sd = importlib.util.module_from_spec(spec)
sys.modules["script_direct_tests"] = sd  # py3.14 dataclass 需要 sys.modules 里有本模块
spec.loader.exec_module(sd)


def verbatim_tokens(lines_text, start=1.0, per_line=2.0, gap=1.0):
    """按行构造完全照稿的对齐 token（每行一个 token，覆盖该行全部字符）。"""
    tokens = []
    cursor = start
    for text in lines_text:
        tokens.append({"text": text, "start": round(cursor, 3), "end": round(cursor + per_line, 3)})
        cursor += per_line + gap
    return tokens, cursor


SCRIPT = "大家好，欢迎来到本期节目。\n今天我们聊性能优化。\n感谢观看，下期再见。"


def test_perfect_verbatim_passes_gate_and_builds_rows():
    lines = sd.parse_reference_lines(SCRIPT)
    tokens, total = verbatim_tokens([line.text for line in lines], start=1.0, per_line=2.0, gap=1.0)
    duration = total
    mapping = sd.map_aligned_tokens_to_lines(tokens, lines, language="Chinese")
    quality = sd.evaluate_direct_quality(tokens, mapping, line_count=len(lines), duration=duration)
    assert quality.passed, quality.reasons
    assert len(mapping.blocks) == 3
    rows = sd.build_direct_rows(mapping, fps=30.0)
    assert [row["text"] for row in rows] == [line.text for line in lines]
    assert rows[0]["start_frame"] == 30 and rows[0]["end_frame"] == 90
    assert rows[0]["timing_decision"] == "script_direct"


def test_half_spoken_script_fails_gate():
    lines = sd.parse_reference_lines(SCRIPT)
    # 只念了第一行：后两行无对齐覆盖
    tokens = [{"text": lines[0].text, "start": 1.0, "end": 3.0}]
    mapping = sd.map_aligned_tokens_to_lines(tokens, lines, language="Chinese")
    quality = sd.evaluate_direct_quality(tokens, mapping, line_count=len(lines), duration=10.0)
    assert not quality.passed
    assert "UNMAPPED_SCRIPT_LINES" in quality.reasons


def test_compressed_span_fails_gate():
    lines = sd.parse_reference_lines(SCRIPT)
    tokens, _ = verbatim_tokens([line.text for line in lines], start=0.0, per_line=0.2, gap=0.1)
    # 20 秒音频，全部语音挤在最后 1 秒内
    tokens = [
        {"text": t["text"], "start": 19.0 + i * 0.3, "end": 19.0 + i * 0.3 + 0.25}
        for i, t in enumerate(tokens)
    ]
    mapping = sd.map_aligned_tokens_to_lines(tokens, lines, language="Chinese")
    quality = sd.evaluate_direct_quality(tokens, mapping, line_count=len(lines), duration=20.0)
    assert not quality.passed
    assert "COMPRESSED_ALIGNMENT_SPAN" in quality.reasons


def test_collapsed_intervals_fail_gate():
    lines = sd.parse_reference_lines("这是一段比较长的测试文稿内容。\n第二行内容也不短。")
    # ≥10 个 token 全部塌缩到同一区间
    tokens = [
        {"text": "字", "start": 5.0, "end": 5.0} for _ in range(12)
    ]
    mapping = sd.map_aligned_tokens_to_lines(tokens, lines, language="Chinese")
    quality = sd.evaluate_direct_quality(tokens, mapping, line_count=len(lines), duration=10.0)
    assert not quality.passed
    assert "COLLAPSED_ALIGNMENT_INTERVALS" in quality.reasons


def test_long_audio_over_limit_is_caller_decision():
    # 引擎只提供常量；调用方按 AUDIO_TOO_LONG_FOR_DIRECT_ALIGNMENT 回退
    assert sd.DIRECT_MAX_AUDIO_SECONDS == 300.0


def test_latin_word_grouping_maps_english_lines():
    lines = sd.parse_reference_lines("Hello world\nDaVinci Resolve 20")
    tokens = [
        {"text": "Hello world", "start": 1.0, "end": 2.0},
        {"text": "DaVinci Resolve 20", "start": 3.0, "end": 5.0},
    ]
    mapping = sd.map_aligned_tokens_to_lines(tokens, lines, language="English")
    assert len(mapping.blocks) == 2
    assert mapping.mapping_coverage == pytest.approx(1.0)


def test_locally_collapsed_line_flagged():
    lines = sd.parse_reference_lines("第一行正常语速。\n第二行被压成一瞬。")
    tokens = [
        {"text": "第一行正常语速", "start": 1.0, "end": 3.0},
        {"text": "第二行被压成一瞬", "start": 3.0, "end": 3.2},
    ]
    mapping = sd.map_aligned_tokens_to_lines(tokens, lines, language="Chinese")
    quality = sd.evaluate_direct_quality(tokens, mapping, line_count=len(lines), duration=6.0)
    assert not quality.passed
    assert "LOCALLY_COLLAPSED_SCRIPT_INTERVALS" in quality.reasons
    assert quality.locally_collapsed_line_indices == (1,)
