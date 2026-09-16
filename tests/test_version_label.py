from pathlib import Path
import ctypes
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_version_label_survives_status_updates():
    source = (ROOT / "SubFix.lua").read_text()
    assert 'ID = "VersionLabel"' in source
    version = re.search(r'^SUBFIX_VERSION = "([^"]+)"', source, re.M).group(1)
    layout = source[source.index("local function create_full_content()"):source.index("local function create_mini_window()")]
    library = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/libluajit-5.1.2.dylib")
    if not library.exists():
        pytest.skip("Resolve LuaJIT unavailable")
    lua = ctypes.CDLL(str(library))
    lua.luaL_newstate.restype = ctypes.c_void_p
    lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
    lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    lua.lua_tolstring.restype = ctypes.c_char_p
    lua.lua_close.argtypes = [ctypes.c_void_p]
    code = 'SUBFIX_VERSION = "' + version + '"\n' + '''
local items = {}
local ui = setmetatable({}, {__index = function(_, kind)
    return function(_, value)
        if type(value) ~= "table" then return {} end
        value.kind = kind
        if value.ID then items[value.ID] = value end
        return value
    end
end})
''' + layout + '''
create_full_content()
assert(items.StatusRow.kind == "HGroup")
assert(items.StatusRow[1] == items.StatusLabel)
assert(items.StatusRow[2] == items.VersionLabel)
assert(items.VersionLabel.Text == "v" .. SUBFIX_VERSION)
assert(items.VersionLabel.Alignment.AlignRight)
assert(items.StatusLabel.Weight == 1 and items.VersionLabel.Weight == 0)
assert(items.StatusLabel.WordWrap)
items.StatusLabel.Text = string.rep("更新状态 ", 100)
assert(items.VersionLabel.Text == "v" .. SUBFIX_VERSION)
'''
    state = lua.luaL_newstate()
    try:
        lua.luaL_openlibs(state)
        assert lua.luaL_loadstring(state, source.encode()) == 0, lua.lua_tolstring(state, -1, None)
        assert lua.luaL_loadstring(state, code.encode()) == 0, lua.lua_tolstring(state, -1, None)
        assert lua.lua_pcall(state, 0, 0, 0) == 0, lua.lua_tolstring(state, -1, None)
    finally:
        lua.lua_close(state)
