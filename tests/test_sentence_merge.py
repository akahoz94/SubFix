"""整句合并 SUBFIX_V4_SENTENCE_MERGE 的回归。

v4 按 0.8 秒呼吸就断行，一句话常被切成 4~8 字的小行；合并只在"确实说完了"的
接缝把小行收回整句。这里钉五条合并判据（句末要真有停顿、不跨轨、不跨超长停顿、
不超字数上限、字符流不变）加上关掉开关时逐行不变。
"""

import os

import pytest

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("v4_sentence_merge", ROOT / "subfix_generate_v4.py")
v4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v4)

FPS = 25.0
ENV = "SUBFIX_V4_SENTENCE_MERGE"
MAX_CHARS = 18
FRAME = int(round(1.0 / FPS))  # 1 帧


def unit(text, start, end, track=1):
    return {"text": text, "start_frame": start, "end_frame": end, "track_index": track,
            "source_score": 10.0, "window_index": 1}


def build(texts, gap_frames=0, sentence_ends=(), tracks=None):
    """每段文本一行；gap_frames 是相邻行之间的真实帧间隔；sentence_ends 是"行尾被标成句末"的行号。

    单元与行同源生成，所以行尾单元的时间就是行尾时间，行首单元的时间就是行首时间。
    """
    track_list = list(tracks or [1] * len(texts))
    units = []
    rows = []
    cursor = 0
    for index, text in enumerate(texts):
        row_start = cursor
        for char in text:
            units.append(unit(char, cursor, cursor + 5, track_list[index]))
            cursor += 5
        rows.append({"index": index + 1, "text": text, "start_frame": row_start,
                     "end_frame": cursor, "track_index": track_list[index],
                     "speaker_track_index": 1})
        cursor += gap_frames
    for row_index in sentence_ends:
        offset = sum(len(text) for text in texts[: row_index + 1]) - 1
        units[offset]["asr_punctuation_strength"] = 1.0
    return rows, units


@pytest.fixture(autouse=True)
def _clean_env():
    os.environ.pop(ENV, None)
    yield
    os.environ.pop(ENV, None)


@pytest.mark.parametrize("value", ["0", "false", "No", " off "])
def test_switch_off_words_all_disable(value):
    os.environ[ENV] = value
    assert v4._sentence_merge_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "随便"])
def test_anything_else_keeps_it_on(value):
    os.environ[ENV] = value
    assert v4._sentence_merge_enabled() is True


def test_default_is_on():
    assert v4._sentence_merge_enabled() is True


def test_disabled_returns_the_rows_untouched():
    os.environ[ENV] = "0"
    rows, units = build(["前面模型上的这块", "小挡板就是车辆的", "外部结构"])
    result, joined = v4._apply_sentence_merge(rows, units, FPS, MAX_CHARS)
    assert [row["text"] for row in result] == ["前面模型上的这块", "小挡板就是车辆的", "外部结构"]
    assert joined == 0


def test_merges_a_breathing_seam_inside_one_sentence():
    rows, units = build(["前面模型上的这块", "小挡板就是车辆的", "外部结构"], gap_frames=int(0.4 * FPS))
    result, joined = v4._apply_sentence_merge(rows, units, FPS, MAX_CHARS)
    assert [row["text"] for row in result] == ["前面模型上的这块小挡板就是车辆的", "外部结构"]
    assert joined == 1
    assert result[0]["start_frame"] == rows[0]["start_frame"]
    assert result[0]["end_frame"] == rows[1]["end_frame"]


def test_a_real_pause_after_a_sentence_end_closes_it():
    # 第 2 行尾被 ASR 标成句末，且后面真的停了 0.5 秒 → 在这里收口，不再往下并。
    rows, units = build(["前面模型上的", "这块小挡板", "就是车辆的外壳"],
                        gap_frames=int(0.5 * FPS), sentence_ends=(1,))
    result, _ = v4._apply_sentence_merge(rows, units, FPS, MAX_CHARS)
    assert [row["text"] for row in result] == ["前面模型上的这块小挡板", "就是车辆的外壳"]


def test_a_marked_sentence_end_without_a_pause_is_ignored():
    # 标点打错是常态：标了句末但紧接着还在说，不该收口。
    rows, units = build(["前面模型上的", "这块小挡板", "就是外壳"], sentence_ends=(1,))
    result, _ = v4._apply_sentence_merge(rows, units, FPS, MAX_CHARS)
    assert [row["text"] for row in result] == ["前面模型上的这块小挡板就是外壳"]


def test_never_crosses_tracks():
    rows, units = build(["前面模型上的这块", "小挡板就是车辆的"], tracks=[1, 2])
    result, joined = v4._apply_sentence_merge(rows, units, FPS, MAX_CHARS)
    assert joined == 0
    assert [row["text"] for row in result] == ["前面模型上的这块", "小挡板就是车辆的"]


def test_a_gap_just_over_the_bridge_limit_is_not_crossed():
    # 判据用帧边界精确钉住，改常量时这条会跟着动，不会留下过期的硬编码秒数。
    limit = int(round(v4.SENTENCE_MERGE_MAX_GAP_SECONDS * FPS))
    rows, units = build(["前面模型上的这块", "小挡板就是车辆的"], gap_frames=limit + 1)
    result, joined = v4._apply_sentence_merge(rows, units, FPS, MAX_CHARS)
    assert joined == 0 and len(result) == 2


def test_a_gap_at_the_bridge_limit_is_crossed():
    # 呼吸级分句的阈值比这个更短，跨过它才能组出整句。
    limit = int(round(v4.SENTENCE_MERGE_MAX_GAP_SECONDS * FPS))
    rows, units = build(["前面模型上的这块", "小挡板就是车辆的"], gap_frames=limit)
    result, joined = v4._apply_sentence_merge(rows, units, FPS, MAX_CHARS)
    assert joined == 1 and len(result) == 1


def test_combines_never_exceed_the_char_limit():
    rows, units = build(["前面模型上的这块", "小挡板就是车辆的", "外部结构"])
    result, _ = v4._apply_sentence_merge(rows, units, FPS, MAX_CHARS)
    assert [len(row["text"]) for row in result] == [16, 4]
    assert all(len(row["text"]) <= MAX_CHARS for row in result)


def test_a_tighter_limit_shrinks_the_merges_too():
    # 不能靠悄悄放大上限来凑整句。
    rows, units = build(["前面模型上的这块", "小挡板就是车辆的", "外部结构"])
    result, joined = v4._apply_sentence_merge(rows, units, FPS, 10)
    assert joined == 0
    assert [row["text"] for row in result] == ["前面模型上的这块", "小挡板就是车辆的", "外部结构"]


def test_user_without_a_length_choice_gets_the_default_limit():
    rows, units = build(["前面模型上的这块", "小挡板就是车辆的", "外部结构"])
    result, _ = v4._apply_sentence_merge(rows, units, FPS, None)
    assert all(len(row["text"]) <= v4.DEFAULT_SENTENCE_MERGE_MAX_CHARS for row in result)
    assert [len(row["text"]) for row in result] == [16, 4]


def test_char_stream_and_index_survive_the_merge():
    rows, units = build(["前面模型上的这块", "小挡板就是车辆的", "外部结构"], gap_frames=int(0.4 * FPS))
    result, joined = v4._apply_sentence_merge(rows, units, FPS, MAX_CHARS)
    assert "".join(row["text"] for row in result) == "".join(row["text"] for row in rows)
    assert [row["index"] for row in result] == list(range(1, len(result) + 1))
    assert joined == len(rows) - len(result)
    assert all(left["end_frame"] <= right["start_frame"] for left, right in zip(result, result[1:]))


def test_a_single_row_is_left_alone():
    rows, units = build(["就这一行"])
    result, joined = v4._apply_sentence_merge(rows, units, FPS, MAX_CHARS)
    assert result is rows and joined == 0


@pytest.mark.parametrize("mode", ["live", "narration"])
@pytest.mark.parametrize("max_chars,off_shape,on_shape", [
    (18, [(10, 0, 50), (10, 65, 115), (10, 130, 180), (3, 195, 210)],
         [(10, 0, 50), (10, 65, 115), (13, 130, 210)]),
    (25, [(10, 0, 50), (10, 65, 115), (10, 130, 180), (3, 195, 210)],
         [(20, 0, 115), (13, 130, 210)]),
])
def test_end_to_end_the_switch_only_changes_row_shape(mode, max_chars, off_shape, on_shape):
    """三处 0.6 秒呼吸把长句切成 4 条；开关只把条拼回，字与帧的归属不许变。

    形状是实测值（长度/起止帧）：18 字上限下最后一条只能并一次，25 字上限下并成两条，
    用来同时证明"跨过呼吸能组整句"和"合并仍受所选长度上限约束"。
    """
    text = "前面模型上的这块小挡板就是车辆模型的外部结构组成部分现在把它取下来"
    units = []
    clock = 0
    for index, char in enumerate(text):
        if index in (10, 20, 30):
            clock += 15
        units.append(unit(char, clock, clock + 5))
        clock += 5

    def segment(switch):
        os.environ[ENV] = switch
        try:
            rows, diagnostic = v4.segment_canonical_units(units, mode, None, FPS, max_chars=max_chars)
        finally:
            os.environ.pop(ENV, None)
        assert diagnostic["text_conservation_failed_count"] == 0
        rows, _ = v4.protect_word_boundaries(rows, units, FPS)
        rows, _ = v4.enforce_hard_char_limit(rows, units, max_chars)
        return [(len(row["text"]), row["start_frame"], row["end_frame"]) for row in rows]

    off, on = segment("0"), segment("1")
    assert off == off_shape
    assert on == on_shape
    assert sum(s[0] for s in on) == sum(s[0] for s in off) == len(text)
