"""文稿校对引擎：五类差异分类、幂等、降级、时间轴保护、管线挂点。"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("subfix_script_proofread", ROOT / "subfix_script_proofread.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


proofread = _load()


def row(text, start=10, end=50):
    return {"text": text, "start_frame": start, "end_frame": end}


def assert_timing_untouched(rows):
    for item in rows:
        assert "start_frame" in item and "end_frame" in item
        assert item["start_frame"] <= item["end_frame"]


def test_homophone_single_char_uses_script_writing():
    rows = [row("这件事在说一遍")]
    output, diagnostic = proofread.proofread_rows(rows, "这件事再说一遍")
    assert output[0]["text"] == "这件事再说一遍"
    assert output[0]["transcript_text"] == "这件事在说一遍"
    assert output[0]["proofread_status"] == "script_corrected"
    assert diagnostic["proofread_homophone_replace_count"] == 1
    assert_timing_untouched(output)


def test_de_de_ge_homophone_corrected():
    rows = [row("他高兴的跳了起来")]
    output, diagnostic = proofread.proofread_rows(rows, "他高兴得跳了起来")
    assert output[0]["text"] == "他高兴得跳了起来"
    assert diagnostic["proofread_homophone_replace_count"] == 1


def test_multi_char_homophone_same_length_corrected():
    rows = [row("向大家反应一下情况")]
    output, diagnostic = proofread.proofread_rows(rows, "向大家反映一下情况")
    assert output[0]["text"] == "向大家反映一下情况"
    assert diagnostic["proofread_homophone_replace_count"] == 1


def test_non_homophone_structure_is_kept():
    # 智障(zhang) ≠ 智能(neng)：不同音不许改，宁保转录不误改
    rows = [row("这是人工智障的时代")]
    output, diagnostic = proofread.proofread_rows(rows, "这是人工智能的时代")
    assert output[0]["text"] == "这是人工智障的时代"
    assert diagnostic["proofread_structure_kept_count"] >= 1
    assert diagnostic["proofread_homophone_replace_count"] == 0


def test_punctuation_only_diff_follows_script():
    rows = [row("你好,世界")]
    output, diagnostic = proofread.proofread_rows(rows, "你好，世界")
    assert output[0]["text"] == "你好，世界"
    assert diagnostic["proofread_punctuation_replace_count"] == 1


def test_script_extra_sentence_never_added():
    # 用户跳读了第二句：文稿内容绝不能被新增进字幕
    rows = [row("第一句。"), row("第三句。")]
    output, diagnostic = proofread.proofread_rows(rows, "第一句。第二句。第三句。")
    assert [item["text"] for item in output] == ["第一句。", "第三句。"]
    assert diagnostic["proofread_homophone_replace_count"] == 0


def test_improvised_content_kept_against_script():
    rows = [row("开场白。"), row("即兴加的一句话。"), row("结束语。")]
    script = "开场白。结束语。"
    output, _ = proofread.proofread_rows(rows, script)
    assert [item["text"] for item in output] == ["开场白。", "即兴加的一句话。", "结束语。"]


def test_number_formatting_left_to_textnorm():
    # 数字写法不属于同音纠错职责，保守保转录（textnorm 后续处理）
    rows = [row("今年是二零二四年")]
    output, diagnostic = proofread.proofread_rows(rows, "今年是2024年")
    assert output[0]["text"] == "今年是二零二四年"
    assert diagnostic["proofread_homophone_replace_count"] == 0


def test_replacement_spanning_two_rows_is_kept():
    rows = [row("前半错字"), row("后半错字")]
    # 假想文稿把跨行的"错字"改成"锤子"——跨行块无法归属单行，保转录
    output, diagnostic = proofread.proofread_rows(rows, "前半锤子后半错字")
    assert output[0]["text"] == "前半错字"
    assert diagnostic["proofread_cross_row_kept_count"] >= 0  # 分类结果可为任一类，但绝不误改


def test_multiple_replacements_in_one_row():
    rows = [row("在承认这事实")]
    output, diagnostic = proofread.proofread_rows(rows, "再承认这事实")
    assert output[0]["text"] == "再承认这事实"
    assert diagnostic["proofread_changed_row_count"] == 1


def test_rerun_with_new_script_is_idempotent_against_transcript():
    rows = [row("这件事在说一遍")]
    first, _ = proofread.proofread_rows(rows, "这件事再说一遍")
    # 换一版文稿重校：必须拿原始转录 diff，不在第一次结果上叠加
    second, _ = proofread.proofread_rows(first, "这件事再说一遍呢")
    assert second[0]["transcript_text"] == "这件事在说一遍"
    # 文稿多出的"呢"是 insert 块——按"内容绝不新增"契约被丢弃，
    # 只有同音的 在→再 采用文稿写法
    assert second[0]["text"] == "这件事再说一遍"


def test_empty_script_is_noop():
    rows = [row("随便什么")]
    output, diagnostic = proofread.proofread_rows(rows, "   \n  ")
    assert output[0]["text"] == "随便什么"
    assert diagnostic["proofread_status"] == "noop_empty_script"


def test_missing_pinyin_degrades_to_punctuation_only(monkeypatch):
    monkeypatch.setattr(proofread, "lazy_pinyin", None)
    rows = [row("这件事在说一遍"), row("你好,世界")]
    output, diagnostic = proofread.proofread_rows(rows, "这件事再说一遍，你好，世界")
    # 同音纠错失效：错字保留
    assert output[0]["text"] == "这件事在说一遍"
    # 标点规则不依赖拼音：仍然生效
    assert output[1]["text"] == "你好，世界"
    assert diagnostic["proofread_pinyin_available"] is False


def test_script_with_line_breaks_matches_concatenated_rows():
    rows = [row("第一句在说"), row("第二句完")]
    output, _ = proofread.proofread_rows(rows, "第一句再说\n第二句完")
    assert output[0]["text"] == "第一句再说"
    assert output[1]["text"] == "第二句完"


def test_english_words_untouched():
    rows = [row("用 Python 写的脚本")]
    output, diagnostic = proofread.proofread_rows(rows, "用 Python 写的脚本")
    assert output[0]["text"] == "用 Python 写的脚本"
    assert diagnostic["proofread_status"] == "noop_no_match"


def test_pipeline_hook_sits_between_word_protection_and_char_limit():
    source = (ROOT / "subfix_asr_transcribe.py").read_text(encoding="utf-8")
    hook = source.index("script_proofread.proofread_rows(")
    assert source.index("protect_word_boundaries(") < hook
    assert hook < source.index("enforce_hard_char_limit(")
    assert 'parser.add_argument(\n        "--script-file"' in source
