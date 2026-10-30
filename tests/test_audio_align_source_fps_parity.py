"""素材帧率对齐的两份拷贝必须保持同一套单位纪律。

上游 v3.3.5 只修了 .subfix_support/subfix_generate_selection_core.lua，SubFix.lua 里
规整字幕长度用的那份还是「拿时间线帧率除素材帧」——59.94 素材进 29.97 时间线就错位。
两份拷贝是有意不合并的（跨文件 load 成本 + fail-open 语义），所以这条对齐检查就是唯一的护栏：
任何一份改回单帧率、或把素材帧除时间线帧率，这里就该红。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBFIX = (ROOT / "SubFix.lua").read_text(encoding="utf-8")
CORE = (ROOT / ".subfix_support" / "subfix_generate_selection_core.lua").read_text(encoding="utf-8")


def segment(source, start_marker, end_marker):
    start = source.index(start_marker)
    return source[start:source.index(end_marker, start)]


SUBFIX_HELPER = segment(SUBFIX, "function SUBFIX_AUDIO_ALIGN.get_audio_item_source_offset_frames(",
                        "function SUBFIX_AUDIO_ALIGN.audio_source_from_item(")
SUBFIX_CONSUMER = segment(SUBFIX, "function SUBFIX_AUDIO_ALIGN.audio_source_from_item(",
                          "function SUBFIX_AUDIO_ALIGN.copy_row_for_audio_source(")
CORE_HELPER = segment(CORE, "local function get_audio_item_source_offset_frames(",
                      "local function get_audio_item_file_path(")

COPIES = {
    "SubFix.lua": SUBFIX_HELPER,
    "subfix_generate_selection_core.lua": CORE_HELPER,
}


def test_both_copies_exist_under_the_same_name():
    """同名才同查：grep 一个函数名就能找到另一份拷贝。"""
    assert "get_audio_item_source_offset_frames" in CORE_HELPER
    assert "SUBFIX_AUDIO_ALIGN.get_audio_item_source_offset_frames" in SUBFIX


def test_both_copies_read_the_source_frame_and_its_own_fps():
    for name, helper in COPIES.items():
        assert "GetSourceStartFrame" in helper, name
        assert 'GetClipProperty("FPS")' in helper, name


def test_both_copies_prefer_source_tier_and_fall_back_to_left_offset():
    for name, helper in COPIES.items():
        assert helper.index("GetSourceStartFrame") < helper.index("GetLeftOffset"), name
        assert "return offset, source_fps" in helper, name
        assert "return offset, timeline_fps" in helper, name
        # 素材帧号与素材帧率成对返回，缺一件就整体退回时间线帧（不是半对）
        assert "if offset and offset >= 0 and source_fps and source_fps > 0 then" in helper, name


def test_neither_copy_divides_source_frames_by_timeline_fps():
    forbidden = re.compile(r"source_(?:offset_)?frames?\s*/\s*(?:timeline_fps|effective_fps)")
    for name, text in (("SubFix.lua", SUBFIX_HELPER + SUBFIX_CONSUMER), ("core", CORE_HELPER)):
        assert not forbidden.search(text), f"{name} 把素材帧除以了时间线帧率: {forbidden.search(text)}"
    assert "source_offset_frames / source_offset_fps" in SUBFIX_CONSUMER


def test_subfix_consumer_pairs_the_mapping_parse_with_source_fps():
    assert "SUBFIX_AUDIO_ALIGN.parse_source_audio_channel_mapping(item, media_item, source_offset_fps," in SUBFIX_CONSUMER
    assert "parse_source_audio_channel_mapping(item, media_item, effective_fps," not in SUBFIX
    # 时长仍然来自时间线帧，不许顺手也改成素材帧率
    assert "(item_end - item_start) / effective_fps" in SUBFIX_CONSUMER


def test_core_consumer_uses_the_same_pairing():
    assert "local source_offset_frames, source_offset_fps = get_audio_item_source_offset_frames(item, effective_fps)" in CORE
    assert "parse_source_audio_channel_mapping(item, media_item, source_offset_fps, source_offset_frames, file_path)" in CORE
    assert "(source_offset_frames / source_offset_fps)" in CORE


def test_subfix_copy_points_at_the_other_copy():
    assert "subfix_generate_selection_core.lua" in SUBFIX_HELPER
