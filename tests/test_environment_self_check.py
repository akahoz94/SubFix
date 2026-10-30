"""环境自检：缺 API 的宿主上必须一行都不崩，而且一次都不写时间线。

沿用仓库做法（test_suspicious_subtitle_review.py）：把 SubFix.lua 里的目标函数原文切出来，
在 LuaJIT 里用桩宿主跑一遍（整体 require 不了，它要 Resolve 宿主），再加源码文本断言钉住接线。
「探测没包 pcall」「按钮接线写两遍」「顺手调了 SetFades」——这几种改法都该在这里红。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "SubFix.lua").read_text(encoding="utf-8")


def segment(start_marker, end_marker):
    start = SOURCE.index(start_marker)
    return SOURCE[start:SOURCE.index(end_marker, start)]


SELFCHECK = segment("function run_environment_self_check(target_window)",
                    "local function format_subtitle_track_delta_summary(")

# 桩宿主：SubFix.lua 里这些是被主 chunk 捕获的名字，切片后一律按全局找
STUBS = '''
    SUBFIX_VERSION = "test"
    SUBFIX_WIN_FFI = false
    SUBFIX_SUBTITLE_FADES = {disabled = false}
    LOG_LINES = {}
    STATUS = {}
    WRITES = 0
    OPENED = 0
    LogMsg = function(text) table.insert(LOG_LINES, text) end
    update_shared_status = function(_, text) table.insert(STATUS, text) end
    show_log_window = function() OPENED = OPENED + 1 end
    get_subtitle_track_type_and_count = function(tl)
        if tl == nil then return "subtitle", 0 end
        return "subtitle", 1
    end
    get_resolve = function() return RESOLVE end
'''

FULL_HOST = '''
    clip = { GetClipProperty = function(_, key)
        if key == "FPS" then return "59.94" end
        return nil
    end }
    audio_item = { GetMediaPoolItem = function() return clip end }
    video_item = {
        GetSourceStartFrame = function() return 492 end,
        GetMediaPoolItem = function() return clip end,
    }
    sub_item = {
        GetUniqueId = function() return "sub-1" end,
        GetFades = function() return {FadeIn = 2, FadeOut = 0} end,
        -- 自检是只读的：只要这里被调到，WRITES 就涨，测试立刻红
        SetFades = function() WRITES = WRITES + 1 return true end,
    }
    timeline = {
        GetName = function() return "成片" end,
        GetSetting = function(_, key)
            if key == "timelineFrameRate" then return "29.97" end
            return nil
        end,
        GetStartFrame = function() return 86400 end,
        GetTrackCount = function(_, kind)
            if kind == "audio" then return 1 end
            if kind == "video" then return 1 end
            return 0
        end,
        GetIsTrackEnabled = function(_, kind, index) return kind == "audio" and index == 1 end,
        GetItemListInTrack = function(_, kind, index)
            if index ~= 1 then return false end
            if kind == "audio" then return {audio_item} end
            if kind == "subtitle" then return {sub_item} end
            if kind == "video" then return {video_item} end
            return false
        end,
    }
    project = {
        GetName = function() return "演示工程" end,
        GetCurrentTimeline = function() return timeline end,
    }
    pm = { GetCurrentProject = function() return project end }
    RESOLVE = {
        GetVersion = function() return "19.1.3" end,
        GetProjectManager = function() return pm end,
    }
    subfix_process_alive = function() return true end
'''

BARE_HOST = '''
    RESOLVE = {}   -- 老 Resolve / 桩宿主：一个方法都没有
    subfix_process_alive = nil
'''

RAISING_HOST = '''
    RESOLVE = {
        GetVersion = function() error("no such method") end,
        GetProjectManager = function() error("no such method") end,
    }
    subfix_process_alive = nil
'''


def run_self_check(host_lua):
    from lupa.luajit21 import LuaRuntime

    lua = LuaRuntime()
    lua.execute(STUBS + host_lua + SELFCHECK)
    lua.execute("ok, err = pcall(function() return run_environment_self_check(nil) end)")
    assert lua.execute("return ok") is True, lua.execute("return tostring(err)")
    return (
        str(lua.execute("return table.concat(LOG_LINES, '\\n')")),
        str(lua.execute("return STATUS[#STATUS]")),
        int(lua.execute("return WRITES")),
        int(lua.execute("return OPENED")),
    )


def test_full_host_reports_every_capability():
    report, summary, writes, opened = run_self_check(FULL_HOST)
    assert writes == 0, "环境自检是只读探测，一次写操作都不许发生"
    assert opened == 1, "自检结果要直接摊给用户看，日志窗口得自己打开"
    assert "Resolve 版本: 19.1.3" in report
    assert "插件版本: test" in report
    assert "项目名称: 演示工程" in report
    assert "时间线名称: 成片" in report
    assert "timelineFrameRate: 29.97" in report
    assert "时间线起始帧: 86400" in report
    assert "音频轨 1 启用: true" in report
    assert "音频轨 1 片段数: 1" in report
    assert "音频轨 1 首片段素材FPS: 59.94" in report
    assert "不一致" in report, "素材帧率与时间线帧率不同必须明写"
    assert "字幕轨 1 片段数: 1" in report
    assert "字幕轨 1 首条 GetUniqueId: sub-1" in report
    assert "字幕轨 1 首条 GetFades: {FadeIn=2, FadeOut=0}" in report
    assert "首个视频片段 GetSourceStartFrame: 492" in report
    assert "首个视频片段素材FPS: 59.94" in report
    assert "字幕淡入淡出保留: 未禁用" in report
    assert "进程判活 subfix_process_alive: 可用" in report
    assert summary == "环境自检完成：接口全部可用，详情见运行日志"


def test_missing_methods_print_unavailable_instead_of_erroring():
    report, summary, writes, _ = run_self_check(BARE_HOST)
    assert writes == 0
    assert "不可用" in report
    assert "Resolve 版本 方法: 不可用" in report
    assert "ProjectManager: 不可用" in report
    assert "进程判活 subfix_process_alive: 不可用" in report
    assert "接口不可用" in summary and "详情见运行日志" in summary


def test_raising_methods_are_caught_too():
    report, _, writes, _ = run_self_check(RAISING_HOST)
    assert writes == 0
    assert "Resolve 版本: 不可用（调用失败）" in report
    assert "ProjectManager: 不可用" in report


def test_fade_keep_disabled_by_env_is_shown():
    from lupa.luajit21 import LuaRuntime

    lua = LuaRuntime()
    lua.execute(STUBS.replace("{disabled = false}", "{disabled = true}") + BARE_HOST + SELFCHECK)
    lua.execute("pcall(function() return run_environment_self_check(nil) end)")
    report = str(lua.execute("return table.concat(LOG_LINES, '\\n')"))
    assert "已由环境变量 SUBFIX_DISABLE_SUBTITLE_FADE_KEEP 关闭" in report


def test_setfades_is_never_probed_or_called():
    assert ":SetFades(" not in SELFCHECK
    assert '"SetFades"' not in SELFCHECK
    assert "SetFades 是写" in SELFCHECK


def test_every_api_call_site_is_wrapped_in_pcall():
    lines = SELFCHECK.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r":Get[A-Za-z]*\(", line):
            continue
        context = "\n".join(lines[max(0, index - 3):index + 1])
        assert "pcall(function()" in context or "read_object(function()" in context, line
    # 方法名形式的探测（probe 内部已 pcall）不许绕过包装直接点出来
    assert "method(obj)" in SELFCHECK or "method(obj, unpack(args))" in SELFCHECK


def test_env_check_button_exists_exactly_once():
    assert SOURCE.count('ID = "EnvCheckBtn"') == 1
    assert SOURCE.count('Text = "环境自检"') == 1
    assert SOURCE.count("win.On.EnvCheckBtn.Clicked") == 1
    assert SOURCE.count("function run_environment_self_check(") == 1
    # 按钮就在运行日志旁边、同一行同一风格，不另起窗口形状
    topbar = segment('ID = "TopBar"', 'ID = "SearchRow"')
    assert topbar.index('ID = "LogWindowBtn"') < topbar.index('ID = "EnvCheckBtn"')
    assert 'run_environment_self_check(win)' in segment(
        "function win.On.EnvCheckBtn.Clicked", "========== 八步流水线")
