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


def clamp_to_scope(rows, scope_start, scope_end):
    """复刻核心层 merge_generated_rows_for_writeback 的选区裁剪。"""
    kept = []
    for row in rows:
        start = max(row["start_frame"], scope_start)
        end = min(row["end_frame"], scope_end)
        if end > start:
            kept.append((start, end))
    return kept


def test_direct_rows_offset_by_timeline_start_frame():
    """达芬奇时间线默认从 01:00:00:00 起。直出行若不叠加这个起点，
    相对帧号(0 起)整体落在选区之前，被核心层裁成 end<=start，字幕一行也写不回去。"""
    lines = sd.parse_reference_lines(SCRIPT)
    tokens, _ = verbatim_tokens([line.text for line in lines], start=1.0, per_line=2.0, gap=1.0)
    mapping = sd.map_aligned_tokens_to_lines(tokens, lines, language="Chinese")
    base = 108000  # 01:00:00:00 @ 30fps
    rows = sd.build_direct_rows(mapping, fps=30.0, timeline_start_frame=base)
    assert rows[0]["start_frame"] == base + 30 and rows[0]["end_frame"] == base + 90

    scope_end = base + int(8.0 * 30)
    assert clamp_to_scope(rows, base, scope_end), "加起点后应仍有可写回行"
    assert clamp_to_scope(sd.build_direct_rows(mapping, fps=30.0), base, scope_end) == [], \
        "漏传起点必须复现『全被裁光』的故障，否则这条回归没守住"


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


LONG_LINE = (
    "大家好欢迎来到本期节目今天我们聊聊短视频的字幕到底应该做多长才算合适"
    "太长了观众读不完太短了又要频繁切屏真的很考验节奏感谢谢观看下期再见"
)


def test_split_keeps_text_lossless_and_within_cap():
    for cap in (10, 12, 25):
        pieces = sd.split_text_to_max_chars(LONG_LINE, cap)
        assert "".join(pieces) == LONG_LINE
        assert all(sd.default_text_length(piece) <= cap for piece in pieces), pieces


def test_split_prefers_punctuation_boundary():
    text = "第一句讲完了。第二句也讲完了！第三句还有一点点没讲完所以被切开了"
    pieces = sd.split_text_to_max_chars(text, 10)
    assert "".join(pieces) == text
    assert all(sd.default_text_length(p) <= 10 for p in pieces)
    assert pieces[0] == "第一句讲完了。", pieces


def test_split_never_cuts_english_word():
    text = "DaVinci Resolve 20 里边的 subtitle 长度限制需要按词边界来切分"
    pieces = sd.split_text_to_max_chars(text, 18)
    assert "".join(pieces) == text
    cursor = 0
    for piece in pieces[:-1]:
        cursor += len(piece)
        before, after = text[cursor - 1], text[cursor]
        assert not (before.isascii() and after.isascii() and before.isalnum() and after.isalnum()), \
            f"切点落进英文词内部: ...{text[max(0, cursor - 10):cursor + 10]}..."


def test_single_overlong_word_is_not_dropped():
    text = "超短。" + "Internationalization" + "尾巴"
    pieces = sd.split_text_to_max_chars(text, 5)
    assert "".join(pieces) == text
    assert any("Internationalization" in p for p in pieces)


def test_build_direct_rows_splits_long_lines_and_keeps_frames_contiguous():
    lines = sd.parse_reference_lines(LONG_LINE)
    tokens = [{"text": LONG_LINE, "start": 0.0, "end": 20.0}]
    mapping = sd.map_aligned_tokens_to_lines(tokens, lines, language="Chinese")
    assert len(mapping.blocks) == 1
    rows = sd.build_direct_rows(mapping, fps=30.0, timeline_start_frame=108000, max_chars=25,
                                text_length=sd.default_text_length)
    assert len(rows) > 1
    assert all(sd.default_text_length(r["text"]) <= 25 for r in rows)
    assert "".join(r["text"] for r in rows) == mapping.blocks[0].text
    assert rows[0]["start_frame"] == 108000
    assert rows[-1]["end_frame"] == 108000 + 600
    for previous, current in zip(rows, rows[1:]):
        assert current["start_frame"] == previous["end_frame"], (previous, current)
        assert current["end_frame"] > current["start_frame"]
    assert [r["index"] for r in rows] == list(range(1, len(rows) + 1))


def test_build_direct_rows_without_cap_is_unchanged():
    lines = sd.parse_reference_lines(SCRIPT)
    tokens, _ = verbatim_tokens([line.text for line in lines])
    mapping = sd.map_aligned_tokens_to_lines(tokens, lines, language="Chinese")
    rows = sd.build_direct_rows(mapping, fps=30.0)
    assert [row["text"] for row in rows] == [line.text for line in lines]


def test_parse_reference_lines_auto_splits_paragraph():
    """粘贴进来的经常是一整段：不分句就是一条字幕=一整段。"""
    paragraph = "第一句话讲完了。第二句话也讲完了！第三句用问号？后面还有没句末标点的尾巴一直拖下去"
    lines = sd.parse_reference_lines(paragraph)
    assert len(lines) >= 4
    assert "".join(line.text for line in lines) == paragraph
    assert all(sd.default_text_length(line.text) <= sd.DIRECT_MAX_SENTENCE_CHARS + 12 for line in lines)


def test_parse_reference_lines_keeps_newline_scripts_unchanged():
    lines = sd.parse_reference_lines(SCRIPT)
    assert [line.text for line in lines] == SCRIPT.splitlines()
    assert [line.index for line in lines] == list(range(len(lines)))


def test_prune_drops_tokens_pushed_beyond_the_audio():
    tokens = [
        {"text": "念到的", "start": 0.2, "end": 1.2},
        {"text": "没念的", "start": 6.9, "end": 8.0},   # 选区只有 6.96s，尾部是被硬塞的
    ]
    kept = sd.prune_out_of_bounds_tokens(tokens, 6.96)
    assert [t["text"] for t in kept] == ["念到的"]


def test_window_picks_spoken_sentences_and_drops_implausible_ones():
    """整篇稿里只念了中间一句：头尾被挤成极窄区间，必须按语速筛掉。"""
    whole = "开场白欢迎语从来没被念出来过。真正念到的这一句话在这里。\n结尾告别语同样没有被念到哦。"
    lines = sd.parse_reference_lines(whole)
    spoken = [line for line in lines if "真正念到" in line.text][0]
    # 对齐器按文稿顺序出 token；没念的句子被挤到相邻说话的边界上，宽度接近 0
    tokens = [
        {"text": "开场白欢迎语从来没被念出来过。", "start": 0.0, "end": 0.05},
        {"text": spoken.text, "start": 0.1, "end": 3.1},
        {"text": "结尾告别语同样没有被念到哦。", "start": 3.15, "end": 3.16},
    ]
    mapping, used, note = sd.align_script_to_spoken_window(
        tokens, lines, language="Chinese", duration=3.2
    )
    assert note != ""
    assert [line.text for line in used] == [spoken.text]
    rows = sd.build_direct_rows(mapping, fps=25.0, timeline_start_frame=268)
    assert [r["text"] for r in rows] == [spoken.text]
    assert rows[0]["start_frame"] >= 268


def test_every_direct_reason_code_has_a_chinese_label():
    """回退原因没标签=用户只看到机器码，正是他投诉的"莫名变慢"。新增码必须配中文。"""
    import re

    spec_asr = importlib.util.spec_from_file_location("sd_labels", ROOT / "subfix_asr_transcribe.py")
    asr = importlib.util.module_from_spec(spec_asr)
    sys.modules["sd_labels"] = asr
    spec_asr.loader.exec_module(asr)

    codes: set[str] = set()
    for name in ("subfix_script_direct.py", "subfix_asr_transcribe.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        codes |= set(re.findall(r'reasons\.append\("([A-Z_]+)"\)', text))
    assert codes, "扫描不到任何质量门代码，说明这个检查本身失效了"
    missing = sorted(code for code in codes if code not in asr.DIRECT_REASON_LABELS)
    assert not missing, f"缺中文标签的回退原因: {missing}"
