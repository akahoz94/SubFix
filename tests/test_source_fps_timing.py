"""素材帧率 ≠ 时间线帧率时，音频截取起点按素材帧率换算、选区范围仍按时间线帧率。
上游 v3.3.5 修的就是这个：59.94fps 素材放进 29.97fps 时间线，认错另一段话或漏字幕。"""
import json
import shutil
import tempfile
from pathlib import Path

import pytest
from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / ".subfix_support" / "subfix_generate_selection_core.lua"
ITEM_START = 1000

HARNESS = """
    media = {
        GetClipProperty = function(_, key)
            if key == 'FPS' and fps_raises then error('unavailable') end
            if key == 'FPS' then return clip_fps end
            if key == 'Sample Rate' then return 48000 end
            if key == 'File Path' then return [[__MEDIA__]] end
        end,
    }
    item = {
        GetStart = function() return __ITEM_START__ end,
        GetEnd = function() return __ITEM_START__ + 10 * timeline_fps end,
        GetSourceStartFrame = function() return source_frame end,
        GetLeftOffset = function() return 246 end,
        GetMediaPoolItem = function() return media end,
        GetSourceAudioChannelMapping = function() return mapping end,
        GetProperty = function() return false end,
    }
    timeline = {
        GetTrackCount = function() return 1 end,
        GetTrackName = function() return 'A1' end,
        GetItemListInTrack = function() return {item} end,
    }
"""


@pytest.fixture(scope="module")
def lua_core():
    if not SRC.exists():
        pytest.skip(f"缺少 {SRC}")
    tmp = Path(tempfile.mkdtemp(prefix="subfix_fps_timing_"))
    clip = tmp / "clip.mov"
    linked = tmp / "linked.wav"
    clip.write_bytes(b"")
    linked.write_bytes(b"")
    core_lua = tmp / "core.lua"
    shutil.copyfile(SRC, core_lua)  # LuaJIT 的文件 API 走 Windows ANSI，中文目录打不开
    lua = LuaRuntime()
    core = lua.execute("return dofile([[%s]])" % core_lua.as_posix())
    return lua, core, clip, linked


def collect(lua, core, clip, linked, *, clip_fps, timeline_fps, source_frame,
            linked_audio, scope_start_frames, fps_raises=False, has_source_api=True):
    lua.execute("clip_fps = %s; timeline_fps = %r; source_frame = %d; fps_raises = %s" % (
        "nil" if clip_fps is None else repr(float(clip_fps)),
        float(timeline_fps), source_frame, "true" if fps_raises else "false"))
    lua.execute(HARNESS.replace("__MEDIA__", clip.as_posix())
                .replace("__ITEM_START__", str(ITEM_START)))
    mapping = {
        "embedded_audio_channels": 2,
        "track_mapping": {"1": {"channel_idx": [3]}},
        "linked_audio": ({"1": {"path": linked.as_posix(), "channels": 2,
                                 "offset": 24000}} if linked_audio else {}),
    }
    lua.execute("mapping = [[%s]]" % json.dumps(mapping))
    if not has_source_api:
        lua.execute("item.GetSourceStartFrame = nil")
    scope = lua.eval("{mode='selection', start_frame=%r, end_frame=%r}" % (
        ITEM_START + scope_start_frames, ITEM_START + scope_start_frames
        + 3 * timeline_fps))
    result = core.collect_audio_sources_for_scope(lua.eval("timeline"), scope, timeline_fps)
    if not hasattr(result, "get") and isinstance(result, tuple):
        pytest.fail(f"选区没收集到音频源: {result[1]}")
    assert result is not None, "没拿到音频源"
    return result[1]


@pytest.mark.parametrize("source_fps,timeline_fps,source_frame", [
    (59.94, 29.97, 492),
    (59.94, 29.97, 1261),
    (59.94, 29.97, 2021),
    (25, 25, 250),
    (24, 30, 240),
])
@pytest.mark.parametrize("linked_audio", [False, True])
def test_audio_start_uses_source_fps_and_range_uses_timeline_fps(
        lua_core, source_fps, timeline_fps, source_frame, linked_audio):
    lua, core, clip, linked = lua_core
    source = collect(lua, core, clip, linked, clip_fps=source_fps,
                     timeline_fps=timeline_fps, source_frame=source_frame,
                     linked_audio=linked_audio, scope_start_frames=2 * timeline_fps)
    start, end = float(source.source_start_seconds), float(source.source_end_seconds)
    # 素材内起点（素材帧率）+ 选区在片段里的位置（时间线帧率）+ 关联音频偏移 0.5 秒
    expected = source_frame / source_fps + 2 + (0.5 if linked_audio else 0)
    assert abs(start - expected) < 1e-6, f"音频起点错: {start} != {expected}"
    assert abs(end - start - 3) < 1e-6, "选区时长没按时间线帧率算"
    assert str(source.file_path) == (linked.as_posix() if linked_audio
                                     else clip.as_posix())


@pytest.mark.parametrize("failure", [
    "source_api", "fps_api", "missing_fps", "zero_fps", "negative_fps",
])
def test_legacy_offset_keeps_timeline_frame_units(lua_core, failure):
    """取不到素材帧号或素材帧率时退回 GetLeftOffset（时间线帧），不许把素材帧除时间线帧率。"""
    lua, core, clip, linked = lua_core
    kwargs = {
        "clip_fps": 59.94, "timeline_fps": 29.97, "source_frame": 492,
        "linked_audio": False, "scope_start_frames": 0,
    }
    if failure == "source_api":
        kwargs["has_source_api"] = False
    elif failure == "fps_api":
        kwargs["fps_raises"] = True
    elif failure == "missing_fps":
        kwargs["clip_fps"] = None
    elif failure == "zero_fps":
        kwargs["clip_fps"] = 0
    elif failure == "negative_fps":
        kwargs["clip_fps"] = -1
    source = collect(lua, core, clip, linked, **kwargs)
    assert abs(float(source.source_start_seconds) - 246 / 29.97) < 1e-6
