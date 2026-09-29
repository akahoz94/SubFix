#!/usr/bin/env lua
-- SubFix child plugin: 文稿匹配（贴稿字幕三合一）。
-- 三种文稿用途，复用同一管线：
--   自动 = 先试文稿直出（整段对齐跳过识别，秒级），质量门不过自动回退「转录+同音校对」
--   仅校对 = 转录后按文稿校对同音别字/标点（结构差异保转录）
--   照稿直出 = 强制直出，质量门不过报原因
-- 音频从时间线自动截取、字幕自动回填，无需手动导出。
-- 直出质量门改编自 heiba-wk/DaVinci-ASR（Apache-2.0）。

local function script_dir()
    local source = debug and debug.getinfo and debug.getinfo(1, "S").source or ""
    source = tostring(source or "")
    local dir = source:match("^(.*[/\\])")
    if dir and dir ~= "" then
        return dir:gsub("[/\\]$", "")
    end
    return os.getenv("PWD") or "."
end

local function parent_dir(path)
    local cleaned = tostring(path or ""):gsub("[/\\]$", "")
    local parent = cleaned:match("^(.*)[/\\][^/\\]+$")
    if parent and parent ~= "" then
        return parent
    end
    return cleaned
end

local function file_exists(path)
    local file = io.open(tostring(path or ""), "rb")
    if file then
        file:close()
        return true
    end
    return false
end

local function resolve_generate_core_root(root)
    local cleaned = tostring(root or ""):gsub("[/\\]$", "")
    local direct_core = cleaned .. "/.subfix_support/subfix_generate_selection_core.lua"
    if file_exists(direct_core) then
        return cleaned
    end
    local parent = parent_dir(cleaned)
    local parent_core = parent .. "/.subfix_support/subfix_generate_selection_core.lua"
    if file_exists(parent_core) then
        return parent
    end
    return cleaned
end

local function load_generate_core(root)
    local core_path = tostring(root or "") .. "/.subfix_support/subfix_generate_selection_core.lua"
    local chunk, load_err = loadfile(core_path)
    if not chunk then
        error("无法加载生成模块: " .. core_path .. " " .. tostring(load_err or ""))
    end
    local ok, core_or_err = pcall(chunk)
    if not ok then
        error("生成模块初始化失败: " .. tostring(core_or_err))
    end
    if type(core_or_err) ~= "table" or type(core_or_err.run) ~= "function" then
        error("生成模块接口无效: " .. core_path)
    end
    return core_or_err
end

local function write_script_temp_file(script_text)
    local base_dir = os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
    local uid = tostring(os.time()) .. "_" .. tostring(math.floor(os.clock() * 1000))
    local path = base_dir .. "/SubFix_ScriptMatch_" .. uid .. ".txt"
    local file = io.open(path, "wb")
    if not file then return nil end
    file:write(script_text)
    file:close()
    return path
end

local ui = fu.UIManager
local disp = bmd.UIDispatcher(ui)

local chosen = nil  -- { script_text, script_direct_mode }

local win = disp:AddWindow({
    ID = "ScriptMatchWin",
    WindowTitle = "SubFix · 文稿匹配（贴稿字幕三合一）",
    Geometry = {280, 120, 640, 600},
    Spacing = 8,
}, ui:VGroup{
    ID = "Root",
    Weight = 1,
    ui:Label{Text = "<b>文稿匹配</b> — 粘贴整段文稿，自动截取时间线音频并回填字幕", Weight = 0},
    ui:HGroup{
        Weight = 0,
        Spacing = 8,
        ui:Label{Text = "文稿用途：", Weight = 0},
        ui:ComboBox{ID = "UsageCombo", Weight = 1}
    },
    ui:Label{Text = "<font color='#8a8a8a'>自动＝照稿念走直出（秒级），没照稿念自动回退转录校对；仅校对＝不尝试直出；照稿直出＝强制直出，失败报原因。</font>", Weight = 0, WordWrap = true},
    ui:TextEdit{
        ID = "ScriptInput",
        Text = "",
        PlaceholderText = "粘贴整段文稿，建议按换行分句。",
        Weight = 1,
        MinimumSize = {0, 300}
    },
    ui:HGroup{
        Weight = 0,
        ui:HGap(0, 1),
        ui:Button{ID = "StartBtn", Text = "开始匹配", Weight = 0, MinimumSize = {140, 30}},
        ui:Button{ID = "CancelBtn", Text = "取消", Weight = 0, MinimumSize = {100, 30}},
        ui:HGap(0, 1)
    },
})

pcall(function()
    win.UsageCombo:AddItem("自动（直出优先，失败自动转录）")
    win.UsageCombo:AddItem("仅校对（转录后按文稿校对）")
    win.UsageCombo:AddItem("照稿直出（失败报原因）")
    win.UsageCombo.CurrentIndex = 0
end)

function win.On.CancelBtn.Clicked(ev)
    disp.ExitLoop()
end

function win.On.ScriptMatchWin.Close(ev)
    disp.ExitLoop()
end

function win.On.StartBtn.Clicked(ev)
    local script_text = ""
    local mode = "auto"
    pcall(function() script_text = tostring(win.ScriptInput.Text or "") end)
    pcall(function()
        mode = ({[0] = "auto", [1] = "proofread", [2] = "direct"})[tonumber(win.UsageCombo.CurrentIndex) or 0] or "auto"
    end)
    if script_text:gsub("%s", "") == "" then
        win.StartBtn.Text = "请先粘贴文稿"
        return
    end
    chosen = { script_text = script_text, script_direct_mode = mode }
    disp.ExitLoop()
end

win:Show()
disp.RunLoop()
win:Hide()

if not chosen then
    return
end

local ok, err = pcall(function()
    local root = resolve_generate_core_root(script_dir())
    local core = load_generate_core(root)
    local script_file_path = write_script_temp_file(chosen.script_text)
    if not script_file_path then
        error("无法写文稿临时文件")
    end

    local run_ok, run_err = core.run({
        script_root = root,
        target_subtitle_track = 1,
        script_file_path = script_file_path,
        script_direct_mode = chosen.script_direct_mode
    })
    pcall(function() os.remove(script_file_path) end)
    if not run_ok then
        error(run_err or "文稿匹配失败")
    end
end)

if not ok then
    local message = tostring(err or "")
    local hint
    if message:find("质量门", 1, true) then
        hint = "没有照稿念的部分会被质量门拦截。建议把用途改为「自动」或「仅校对」：转录后同音错别字按文稿校对，未照稿部分保留识别结果。"
    elseif message:find("AUDIO_TOO_LONG", 1, true) then
        hint = "直出仅支持 5 分钟内音频；更长的素材请用「自动」或「仅校对」。"
    else
        hint = "请检查识别环境（识别模型/音频轨道）后重试。"
    end
    local fail_win = disp:AddWindow({
        ID = "MatchResult",
        WindowTitle = "文稿匹配 — 未完成",
        Geometry = {300, 200, 560, 220},
        Spacing = 8,
    }, ui:VGroup{
        ui:Label{ID = "ResultLabel", Text = "<font color='#FF4D4F'><b>" .. message:gsub("&", "&amp;"):gsub("<", "&lt;") .. "</b></font>", Weight = 1, WordWrap = true},
        ui:Label{Text = "<font color='#FAAD14'>" .. hint .. "</font>", Weight = 0, WordWrap = true},
        ui:Button{ID = "CloseResultBtn", Text = "知道了", Weight = 0, MinimumSize = {120, 28}}
    })
    function fail_win.On.CloseResultBtn.Clicked(ev)
        disp.ExitLoop()
    end
    function fail_win.On.MatchResult.Close(ev)
        disp.ExitLoop()
    end
    fail_win:Show()
    disp.RunLoop()
    fail_win:Hide()
    print("[SubFix 文稿匹配] 未完成: " .. message)
else
    local ok_win = disp:AddWindow({
        ID = "MatchResult",
        WindowTitle = "文稿匹配 — 完成",
        Geometry = {300, 200, 460, 160},
        Spacing = 8,
    }, ui:VGroup{
        ui:Label{Text = "<font color='#00AA55'><b>文稿匹配完成，字幕已写入时间线。</b></font>", Weight = 1},
        ui:Button{ID = "CloseResultBtn", Text = "完成", Weight = 0, MinimumSize = {120, 28}}
    })
    function ok_win.On.CloseResultBtn.Clicked(ev)
        disp.ExitLoop()
    end
    function ok_win.On.MatchResult.Close(ev)
        disp.ExitLoop()
    end
    ok_win:Show()
    disp.RunLoop()
    ok_win:Hide()
end
