"""禁用轨不送识别（省掉重复麦的 2 倍转录），判据必须 fail-open：读不到状态≠禁用。"""
import shutil
import tempfile
from pathlib import Path

import pytest
from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / ".subfix_support" / "subfix_generate_selection_core.lua"

FAKES = """
function make(enabled_map, raise)
  return { GetIsTrackEnabled = function(self, track_type, index)
    if raise then error("no such method") end
    return enabled_map[index]
  end }
end
"""


@pytest.fixture(scope="module")
def lua_core():
    if not SRC.exists():
        pytest.skip(f"缺少 {SRC}")
    lua = LuaRuntime()
    # LuaJIT 的文件 API 走 Windows ANSI，中文目录打不开，先拷到 ASCII 临时路径
    target = Path(tempfile.mkdtemp(prefix="subfix_track_scope_")) / "core.lua"
    shutil.copyfile(SRC, target)
    core = lua.execute("return dofile([[%s]])" % target.as_posix())
    lua.execute(FAKES)
    return lua, core


def indices(lua, core, lua_source, count=3):
    lua.execute("tl = " + lua_source)
    enabled, skipped = core.enabled_audio_track_indices(lua.eval("tl"), count)
    return [int(enabled[i]) for i in range(1, len(enabled) + 1)], [
        int(skipped[i]) for i in range(1, len(skipped) + 1)
    ]


def test_disabled_track_is_skipped_others_kept(lua_core):
    lua, core = lua_core
    assert indices(lua, core, 'make({ [1]=true, [2]=false, [3]=true })') == ([1, 3], [2])


def test_enabled_tracks_untouched(lua_core):
    lua, core = lua_core
    assert indices(lua, core, 'make({ [1]=true, [2]=true, [3]=true })') == ([1, 2, 3], [])


def test_missing_api_fails_open_not_closed(lua_core):
    """老版本 Resolve 没这个方法：宁可多转一条轨，也不能一条字幕都不出。"""
    lua, core = lua_core
    assert indices(lua, core, 'make({}, true)') == ([1, 2, 3], [])


def test_nil_return_counts_as_enabled(lua_core):
    lua, core = lua_core
    assert indices(lua, core, 'make({})') == ([1, 2, 3], [])
