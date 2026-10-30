"""v3.3.6 编辑断句开关 SUBFIX_V4_EDITORIAL_BOUNDARY 的 A/B 回归。

BASELINE 是门控落地前实测的行形状（文本 + 帧），开关关闭时必须逐行一致；
开关打开只改变「字如何分行与时序」，字符流本身与文字守恒不能变。
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("v4_clause_boundaries", ROOT / "subfix_generate_v4.py")
v4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v4)

FPS = 30
ENV = "SUBFIX_V4_EDITORIAL_BOUNDARY"
SENTENCE_MERGE_ENV = "SUBFIX_V4_SENTENCE_MERGE"


@pytest.fixture(autouse=True)
def _keep_sentence_merge_off(monkeypatch):
    """整句合并是另一个独立开关，本文件只钉边界规则。

    直接调用 segment_canonical_units 的用例不吃 run() 里的环境，所以放在 autouse：
    少了这一步，合并会把边界用例的行数改掉，"开关关闭=与改动前逐行一致"这条基线失去意义。
    合并本身在 tests/test_sentence_merge.py 里测。
    """
    monkeypatch.setenv(SENTENCE_MERGE_ENV, "0")
TEMPORAL = "在启动的第二天小车就开始认真地尝试识别道路的标记"
COPULAR = "前面模型上的这块小挡板就是车辆模型的外部结构"
COPULAR_PAUSE = COPULAR.index("型的外部结构")
MODEL_TEXT = "开始整理这些实验记录之后"
UNIFORM = "这个完整语义区间长度还没有超过字幕上限"
COMMA = "看得没有那么清楚或者画面小一点"
NEGATIVE_MODEL = {"boundary_model": {"weights": [0.0] * v4.MODEL_BUCKETS, "bias": -8.0}}


def unit(text, start, end, track=1):
    return {
        "text": text,
        "start_frame": start,
        "end_frame": end,
        "track_index": track,
        "source_score": 10.0,
        "window_index": 1,
    }


def chars(text, base=0, step=5, pause=None, gap=0):
    """逐字 unit；pause 起的 unit 整体后移 gap 帧，模拟句中剪辑停顿。"""
    return [
        unit(char, base + index * step + (gap if pause is not None and index >= pause else 0),
             base + (index + 1) * step + (gap if pause is not None and index >= pause else 0))
        for index, char in enumerate(text)
    ]


def case(units, mode, profile=None, max_chars=None, post=False, model_boundary=None):
    return {"units": units, "mode": mode, "profile": profile, "max_chars": max_chars,
            "post": post, "model_boundary": model_boundary}


NOUN_PHRASES = [
    "在启动的第二天的课堂上小车开始尝试模仿老师的发音",
    "在训练的第三天内完成老师安排的全部练习",
    "在启动的第二天到第三天之间反复进行测试",
    "在启动的第二天下午小车开始尝试模仿老师的发音",
    "在启动的第二天我们见面了",
    "我们讨论在启动的第二天进行声音测试的方案",
    "在第二天就开始的课程中孩子尝试模仿老师的发音",
    "在启动的第二天结束后小车就开始尝试模仿老师的发音",
]
COPULAR_PAIRS = [
    ("屏幕上这条红色的曲线", "就是传感器记录的实时温度"),
    ("我们刚刚看到的那块面板", "正是这台设备的外部保护结构"),
    ("每天坚持训练积累的经验", "才是帮助我们解决问题的关键"),
    ("眼前这片逐渐亮起的区域", "便是系统当前正在处理的范围"),
    ("屏幕上这条红色的曲线", "也就是传感器记录的实时温度"),
]
COPULAR_KEEP = [
    "这就是设备的外壳",
    "如果你认真观察就是没有仪器也能够发现这里的问题",
    "只要你坚持练习就是再难的动作也能够慢慢熟悉",
    "我们讨论的就是这台设备在实际运行过程中产生的问题",
    "这个按钮按下去不就是让设备进入正常工作的状态吗",
]
TEMPORAL_SPLITS = [
    ("在启动的第二天", "小车就开始认真地尝试识别道路的标记"),
    ("在训练的第三天", "小明已经能够独立完成老师安排的练习"),
    ("在启动的第12天", "孩子开始主动模仿老师说话时的声音"),
    ("在完成训练之后", "孩子已经能够独立完成老师安排的练习"),
    ("在第二天", "孩子已经能够独立完成老师安排的练习"),
]

CASES = {
    "temporal_narration_25": case(chars(TEMPORAL, 100), "narration", None, 25, True),
    "temporal_live_25": case(chars(TEMPORAL, 100), "live", None, 25, True),
    "temporal_narration_10": case(chars(TEMPORAL), "narration", None, 10, True),
    "temporal_single_unit": case([unit(TEMPORAL, 100, 250)], "narration", None, 25),
    "copular_live_25": case(chars(COPULAR), "live", None, 25, True),
    "copular_narration_25": case(chars(COPULAR), "narration", None, 25, True),
    "copular_live_gap3": case(chars(COPULAR, pause=COPULAR_PAUSE, gap=3), "live", None, 25, True),
    "copular_live_gap6": case(chars(COPULAR, pause=COPULAR_PAUSE, gap=6), "live", None, 25, True),
    "copular_live_gap12": case(chars(COPULAR, pause=COPULAR_PAUSE, gap=12), "live", None, 25, True),
    "copular_narration_gap12": case(chars(COPULAR, pause=COPULAR_PAUSE, gap=12), "narration", None, 25, True),
    "copular_single_unit": case([unit(COPULAR, 100, 240)], "live", None, 25),
    "copular_unit_short_limit": case([unit(COPULAR, 100, 240)], "live", None, 10),
    "copular_short_limit": case(chars(COPULAR), "live", None, 10, True),
    "model_live_default": case(chars(MODEL_TEXT, step=4), "live", {"boundary_model": {"decision_threshold": 0.7}}),
    "model_live_25": case(chars(MODEL_TEXT, step=4), "live", {"boundary_model": {"decision_threshold": 0.7}}, 25),
    "model_narration_default": case(chars(MODEL_TEXT, step=4), "narration", {"boundary_model": {"decision_threshold": 0.7}}),
    "model_narration_25": case(chars(MODEL_TEXT, step=4), "narration", {"boundary_model": {"decision_threshold": 0.7}}, 25),
    "uniform_0": case([unit(c, i * 5, i * 5 + 5) for i, c in enumerate(UNIFORM)], "live", None, 25),
    "uniform_1": case([unit(c, i * 6, i * 6 + 5) for i, c in enumerate(UNIFORM)], "live", None, 25),
    "uniform_3": case([unit(c, i * 8, i * 8 + 5) for i, c in enumerate(UNIFORM)], "live", None, 25),
    "uniform_4": case([unit(c, i * 9, i * 9 + 5) for i, c in enumerate(UNIFORM)], "live", None, 25),
    "short_comma_clause": case(chars(COMMA, step=4), "live", NEGATIVE_MODEL, 25),
}
for _entry in ("model_live_default", "model_live_25", "model_narration_default", "model_narration_25"):
    CASES[_entry]["model_boundary"] = 4

for _index, _text in enumerate(NOUN_PHRASES):
    CASES[f"noun_{_index}"] = case(chars(_text), "narration", None, 40)
for _index, (_prefix, _predicate) in enumerate(COPULAR_PAIRS):
    CASES[f"pair_{_index}"] = case(chars(_prefix + _predicate), "live", None, 25)
for _index, _text in enumerate(COPULAR_KEEP):
    CASES[f"keep_{_index}"] = case(chars(_text), "live", None, 40)

for _mode in ("live", "narration"):
    _punctuation_units = chars("我们最近整理了所有设备接下来需要逐一检查它们的运行情况")
    _punctuation_units[10]["asr_punctuation_strength"] = 0.55
    CASES[f"pause_punctuation_{_mode}"] = case(_punctuation_units, _mode, NEGATIVE_MODEL, 25, True)
    CASES[f"pause_gap_{_mode}"] = case(
        chars("大家已经完成了今天的练习明天继续学习", pause=12, gap=15), _mode, NEGATIVE_MODEL, 25, True)

CASES["short_comma_clause"]["units"][7]["asr_punctuation_strength"] = 0.55

# 硬边界（静音/说话人/句末标点）不能被紧凑谓语判定吞掉。
_speaker_units = [unit(c, i * 5, (i + 1) * 5, 2 if i >= 5 else 1) for i, c in enumerate("就是设备的外部保护结构")]
CASES["hard_speaker"] = case(_speaker_units, "live", None, 25)
_punctuation_break_units = chars("就是设备的外部保护结构")
_punctuation_break_units[4]["asr_punctuation_strength"] = 1.0
CASES["hard_punctuation"] = case(_punctuation_break_units, "live", None, 25)
_silence_units = chars("就是设备的外部保护结构")
for _item in _silence_units[5:]:
    _item["start_frame"] += 24
    _item["end_frame"] += 24
CASES["hard_silence"] = case(_silence_units, "live", None, 25)

# 改动前实测的门控关闭形状：(文本, start_frame, end_frame)。
# copular_short_limit 只被守恒/上限用例覆盖，门控开合都只看性质。
BASELINE = {
    "temporal_narration_25": [(TEMPORAL, 100, 220)],
    "temporal_live_25": [(TEMPORAL, 100, 220)],
    "temporal_narration_10": [("在启动的第二天小", 0, 40), ("车就开始认真地尝", 40, 80), ("试识别道路的标记", 80, 120)],
    "temporal_single_unit": [(TEMPORAL, 100, 250)],
    "copular_live_25": [(COPULAR, 0, 110)],
    "copular_narration_25": [(COPULAR, 0, 110)],
    "copular_live_gap3": [(COPULAR, 0, 113)],
    "copular_live_gap6": [(COPULAR, 0, 116)],
    "copular_live_gap12": [("前面模型上的这块小挡板就是车辆模", 0, 92), ("型的外部结构", 92, 122)],
    "copular_narration_gap12": [("前面模型上的这块小挡板就是车辆模", 0, 92), ("型的外部结构", 92, 122)],
    "copular_single_unit": [(COPULAR, 100, 240)],
    "copular_unit_short_limit": [(COPULAR, 100, 240)],
    "model_live_default": [("开始整理", 0, 16), ("这些实验记录之后", 16, 48)],
    "model_live_25": [("开始整理", 0, 16), ("这些实验记录之后", 16, 48)],
    "model_narration_default": [("开始整理", 0, 16), ("这些实验记录之后", 16, 48)],
    "model_narration_25": [("开始整理", 0, 16), ("这些实验记录之后", 16, 48)],
    "pause_punctuation_live": [("我们最近整理了所有设备", 0, 55), ("接下来需要逐一检查它们的运行情况", 55, 135)],
    "pause_punctuation_narration": [("我们最近整理了所有设备", 0, 55), ("接下来需要逐一检查它们的运行情况", 55, 135)],
    "pause_gap_live": [("大家已经完成了今天的练习明天继续学习", 0, 105)],
    "pause_gap_narration": [("大家已经完成了今天的练习明天继续学习", 0, 105)],
    "hard_silence": [("就是设备的", 0, 25), ("外部保护结构", 49, 79)],
    "hard_speaker": [("就是设备的", 0, 25), ("外部保护结构", 25, 55)],
    "hard_punctuation": [("就是设备的", 0, 25), ("外部保护结构", 25, 55)],
    "uniform_0": [(UNIFORM, 0, 95)],
    "uniform_1": [(UNIFORM, 0, 113)],
    "uniform_3": [(UNIFORM, 0, 149)],
    "uniform_4": [(UNIFORM, 0, 167)],
    "short_comma_clause": [(COMMA, 0, 60)],
    "noun_0": [(NOUN_PHRASES[0], 0, 120)],
    "noun_1": [(NOUN_PHRASES[1], 0, 95)],
    "noun_2": [(NOUN_PHRASES[2], 0, 95)],
    "noun_3": [(NOUN_PHRASES[3], 0, 110)],
    "noun_4": [(NOUN_PHRASES[4], 0, 60)],
    "noun_5": [(NOUN_PHRASES[5], 0, 100)],
    "noun_6": [(NOUN_PHRASES[6], 0, 110)],
    "noun_7": [(NOUN_PHRASES[7], 0, 120)],
    "pair_0": [(COPULAR_PAIRS[0][0] + COPULAR_PAIRS[0][1], 0, 110)],
    "pair_1": [(COPULAR_PAIRS[1][0] + COPULAR_PAIRS[1][1], 0, 120)],
    "pair_2": [(COPULAR_PAIRS[2][0] + COPULAR_PAIRS[2][1], 0, 120)],
    "pair_3": [(COPULAR_PAIRS[3][0] + COPULAR_PAIRS[3][1], 0, 120)],
    "pair_4": [(COPULAR_PAIRS[4][0] + COPULAR_PAIRS[4][1], 0, 115)],
    "keep_0": [(COPULAR_KEEP[0], 0, 40)],
    "keep_1": [(COPULAR_KEEP[1], 0, 115)],
    "keep_2": [(COPULAR_KEEP[2], 0, 105)],
    "keep_3": [(COPULAR_KEEP[3], 0, 120)],
    "keep_4": [(COPULAR_KEEP[4], 0, 115)],
}

assert set(BASELINE) == set(CASES) - {"copular_short_limit"}, "每个用例都要有门控关闭时的基线"


def run(name, gate, monkeypatch, post=None):
    entry = CASES[name]
    monkeypatch.setenv(ENV, "1" if gate else "0")
    # 整句合并是另一个独立开关，本文件只钉边界规则；混在一起会让"门控关闭=与改动前一致"
    # 这条基线失去意义。合并本身在 tests/test_sentence_merge.py 里测。
    monkeypatch.setenv("SUBFIX_V4_SENTENCE_MERGE", "0")
    original = v4.boundary_probability
    if entry["model_boundary"] is not None:
        model_boundary = entry["model_boundary"]
        v4.boundary_probability = lambda _text, position, _model: (
            0.98 if position == model_boundary else 0.1)
    try:
        rows, diagnostic = v4.segment_canonical_units(
            entry["units"], entry["mode"], entry["profile"], FPS, max_chars=entry["max_chars"])
    finally:
        v4.boundary_probability = original
    apply_post = entry["post"] if post is None else post
    if apply_post and entry["max_chars"]:
        rows, _ = v4.protect_word_boundaries(rows, entry["units"], FPS)
        rows, _ = v4.enforce_hard_char_limit(rows, entry["units"], entry["max_chars"])
    return rows, diagnostic


def shapes(rows):
    return [(row["text"], row["start_frame"], row["end_frame"]) for row in rows]


@pytest.mark.parametrize("name", sorted(BASELINE))
def test_gate_off_rows_are_identical_to_pre_change_baseline(name, monkeypatch):
    rows, diagnostic = run(name, False, monkeypatch)
    assert shapes(rows) == BASELINE[name]
    assert diagnostic["text_conservation_failed_count"] == 0


@pytest.mark.parametrize("name", ["temporal_narration_25", "copular_live_gap12",
                                  "model_live_25", "pause_gap_live", "hard_silence"])
def test_switch_unset_defaults_to_old_behaviour(name, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    rows, _ = run(name, False, monkeypatch)
    assert shapes(rows) == BASELINE[name]


@pytest.mark.parametrize("name", sorted(CASES))
def test_characters_are_never_rewritten_by_the_switch(name, monkeypatch):
    stream = "".join(v4.normalize_text(item["text"]) for item in CASES[name]["units"])
    for gate in (False, True):
        rows, diagnostic = run(name, gate, monkeypatch)
        assert diagnostic["text_conservation_failed_count"] == 0
        assert "".join(v4.normalize_text(row["text"]) for row in rows) == stream


@pytest.mark.parametrize("mode", ["live", "narration"])
@pytest.mark.parametrize("prefix,clause", TEMPORAL_SPLITS)
def test_gate_on_splits_leading_temporal_clause_without_punctuation(mode, prefix, clause, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    units = chars(prefix + clause, base=100)
    rows, diagnostic = v4.segment_canonical_units(units, mode, None, FPS, max_chars=25)
    rows, _ = v4.protect_word_boundaries(rows, units, FPS)
    rows, _ = v4.enforce_hard_char_limit(rows, units, 25)
    assert [row["text"] for row in rows] == [prefix, clause]
    assert rows[0]["start_frame"] == 100
    assert rows[0]["end_frame"] == rows[1]["start_frame"] == 100 + len(prefix) * 5
    assert rows[-1]["end_frame"] == 100 + len(prefix + clause) * 5
    assert diagnostic["text_conservation_failed_count"] == 0


@pytest.mark.parametrize("name", [f"noun_{index}" for index in range(len(NOUN_PHRASES))])
def test_gate_on_does_not_split_inside_temporal_noun_phrase(name, monkeypatch):
    rows, _ = run(name, True, monkeypatch)
    assert shapes(rows) == BASELINE[name]


@pytest.mark.parametrize("mode", ["live", "narration"])
@pytest.mark.parametrize("gap", [0, 3, 6, 12])
def test_gate_on_copular_boundary_beats_misleading_internal_pause(mode, gap, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    prefix, predicate = COPULAR[:11], COPULAR[11:]
    units = chars(COPULAR, pause=COPULAR_PAUSE, gap=gap)
    rows, diagnostic = v4.segment_canonical_units(units, mode, None, FPS, max_chars=25)
    rows, _ = v4.protect_word_boundaries(rows, units, FPS)
    rows, _ = v4.enforce_hard_char_limit(rows, units, 25)
    assert [row["text"] for row in rows] == [prefix, predicate]
    assert rows[0]["end_frame"] == rows[1]["start_frame"] == len(prefix) * 5
    assert rows[-1]["end_frame"] == units[-1]["end_frame"]
    assert diagnostic["text_conservation_failed_count"] == 0


@pytest.mark.parametrize("prefix,predicate", COPULAR_PAIRS)
def test_gate_on_copular_boundary_generalises(prefix, predicate, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    rows, _ = v4.segment_canonical_units(chars(prefix + predicate), "live", None, FPS, max_chars=25)
    assert [row["text"] for row in rows] == [prefix, predicate]


@pytest.mark.parametrize("text", COPULAR_KEEP)
def test_gate_on_keeps_short_and_dependent_copulas_together(text, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    rows, _ = v4.segment_canonical_units(chars(text), "live", None, FPS, max_chars=40)
    assert [row["text"] for row in rows] == [text]


@pytest.mark.parametrize("name", sorted(k for k in BASELINE if k.startswith("model_")))
def test_gate_on_model_probability_alone_cannot_detach_a_short_verb_phrase(name, monkeypatch):
    # 模型 0.98 对阈值 0.7：门控关闭时旧行为会把 "开始整理" 单独摘成一行。
    assert len(run(name, False, monkeypatch)[0]) == 2
    rows, diagnostic = run(name, True, monkeypatch)
    assert shapes(rows) == [(MODEL_TEXT, 0, 48)]
    assert rows[0]["start_frame"] == CASES[name]["units"][0]["start_frame"]
    assert rows[0]["end_frame"] == CASES[name]["units"][-1]["end_frame"]
    assert diagnostic["text_conservation_failed_count"] == 0


@pytest.mark.parametrize("mode", ["live", "narration"])
def test_gate_on_editorial_pause_uses_real_unit_times(mode, monkeypatch):
    rows, diagnostic = run(f"pause_gap_{mode}", True, monkeypatch, post=False)
    assert shapes(rows) == [("大家已经完成了今天的练习", 0, 60), ("明天继续学习", 75, 105)]
    # 断点贴的是人工剪辑帧，误差只有一个 unit。
    assert abs(rows[1]["start_frame"] - (12 * 5 + 15)) <= 3
    assert rows[0]["end_frame"] <= rows[1]["start_frame"]
    assert diagnostic["text_conservation_failed_count"] == 0
    # 标点本来就能给出的边界，开关前后不变。
    assert shapes(run(f"pause_punctuation_{mode}", True, monkeypatch)[0]) == BASELINE[f"pause_punctuation_{mode}"]


@pytest.mark.parametrize("name", [f"uniform_{gap}" for gap in (0, 1, 3, 4)])
def test_gate_on_uniform_spacing_never_invents_a_break(name, monkeypatch):
    rows, _ = run(name, True, monkeypatch)
    assert shapes(rows) == BASELINE[name]


@pytest.mark.parametrize("name", ["hard_silence", "hard_speaker", "hard_punctuation"])
def test_gate_on_hard_boundaries_still_win(name, monkeypatch):
    rows, _ = run(name, True, monkeypatch)
    assert shapes(rows) == BASELINE[name]


@pytest.mark.parametrize("name", ["temporal_narration_10", "copular_short_limit"])
def test_gate_on_hard_char_limit_still_bounds_every_row(name, monkeypatch):
    limit = CASES[name]["max_chars"]
    stream = "".join(item["text"] for item in CASES[name]["units"])
    rows, diagnostic = run(name, True, monkeypatch)
    assert "".join(row["text"] for row in rows) == stream
    assert all(len(row["text"]) <= limit for row in rows)
    assert all(left["end_frame"] <= right["start_frame"] for left, right in zip(rows, rows[1:]))
    assert diagnostic["text_conservation_failed_count"] == 0


@pytest.mark.parametrize("name", ["temporal_single_unit", "copular_single_unit", "copular_unit_short_limit"])
def test_gate_on_never_fabricates_time_inside_a_multi_character_unit(name, monkeypatch):
    rows, _ = run(name, True, monkeypatch)
    assert shapes(rows) == BASELINE[name]


def test_editorial_pause_rejects_a_break_inside_an_ascii_word():
    """偏移按 unit 原文长度计：改用 normalize 长度会把 ASCII 词内切分放行。"""
    units = [unit("the camera ", 0, 5), unit("slow", 5, 10), unit("ly", 25, 30),
             unit("past the rig", 30, 35)]
    bounds = v4._unit_word_boundary_positions(units)
    assert 15 not in bounds and 13 in bounds
    assert v4._editorial_pause_boundary(units, FPS) is None


def test_editorial_pause_splits_an_ascii_row_on_a_word_edge(monkeypatch):
    units = [unit("the", 0, 5), unit("camera", 5, 10), unit("slowly", 10, 15),
             unit("moves", 30, 35), unit("past", 35, 40)]
    for index in (1, 2, 3, 4):
        units[index]["space_before"] = True
    assert v4._editorial_pause_boundary(units, FPS) == 3

    monkeypatch.setenv(ENV, "0")
    unsplit, _ = v4.segment_canonical_units(units, "live", None, FPS, max_chars=25)
    monkeypatch.setenv(ENV, "1")
    rows, diagnostic = v4.segment_canonical_units(units, "live", None, FPS, max_chars=25)
    assert shapes(unsplit) == shapes(rows)  # 0.5s 间隙本身就是非模型证据，开关前后同一处断开
    assert shapes(rows) == [("thecameraslowly", 0, 15), ("movespast", 30, 40)]
    assert diagnostic["text_conservation_failed_count"] == 0
