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
    -- Resolve 给的 chunk 名带前导 @（"@C:/.../文稿匹配.lua"），不剥掉后面每条路径探测都会静默失败
    source = tostring(source or ""):gsub("^@", "")
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
    local uid = tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
    local path = base_dir .. "/SubFix_ScriptMatch_" .. uid .. ".txt"
    local file = io.open(path, "wb")
    if not file then return nil end
    file:write(script_text)
    file:close()
    return path
end

-- 分阶段日志：窗口打不开/按钮无响应时，%TEMP%\subfix_script_match_error.log 指出停在哪一步
local LOG_PATH = (os.getenv("TEMP") or os.getenv("TMP") or "/tmp") .. "/subfix_script_match_error.log"
local log_is_fresh = true
local function log_stage(stage, detail)
    local fh = io.open(LOG_PATH, log_is_fresh and "w" or "a")
    if not fh then return end
    log_is_fresh = false
    fh:write(os.date("%H:%M:%S") .. " " .. tostring(stage) .. ": " .. tostring(detail or "ok") .. "\n")
    fh:close()
end
log_stage("启动", "脚本已加载 目录=" .. script_dir())

-- Workspace 菜单上下文里 fusion.UIManager 才会泵控制件事件（主程序 SubFix.lua 用的就是它）；fu 留作兜底
local ui_impl = (fusion and fusion.UIManager) or (fu and fu.UIManager)
if not ui_impl then
    log_stage("启动失败", "fusion.UIManager 与 fu.UIManager 都取不到")
    print("[SubFix 文稿匹配] 启动失败: 当前上下文取不到 UIManager")
    return
end
local disp = bmd.UIDispatcher(ui_impl)

local MODE_IDS = { "auto", "proofread", "direct" }
local chosen = nil  -- { script_text, script_direct_mode }

win = disp:AddWindow({
    ID = "ScriptMatchWin",
    WindowTitle = "SubFix · 文稿匹配（贴稿字幕三合一）",
    Geometry = {280, 120, 640, 620},
    Spacing = 8,
}, ui_impl:VGroup{
    ID = "Root",
    Weight = 1,
    ui_impl:Label{Text = "<b>文稿匹配</b> — 粘贴整段文稿，自动截取时间线音频并回填字幕", Weight = 0},
    ui_impl:HGroup{
        Weight = 0,
        Spacing = 8,
        ui_impl:Label{Text = "文稿用途：", Weight = 0},
        ui_impl:ComboBox{ID = "UsageCombo", Weight = 1}
    },
    ui_impl:Label{Text = "<font color='#9a9a9a'>自动＝照稿念走直出（秒级），没照稿念自动回退转录校对；仅校对＝不尝试直出；照稿直出＝强制直出，失败报原因。</font>", Weight = 0, WordWrap = true},
    ui_impl:Label{Text = "文稿内容（按换行分句更好）：", Weight = 0},
    ui_impl:TextEdit{
        ID = "ScriptInput",
        Text = "",
        Weight = 1,
        MinimumSize = {0, 300}
    },
    ui_impl:Label{ID = "StatusLabel", Text = "", Weight = 0, WordWrap = true},
    ui_impl:HGroup{
        Weight = 0,
        ui_impl:HGap(0, 1),
        ui_impl:Button{ID = "StartBtn", Text = "开始匹配", Weight = 0, MinimumSize = {140, 30}},
        ui_impl:Button{ID = "CancelBtn", Text = "取消", Weight = 0, MinimumSize = {100, 30}},
        ui_impl:HGap(0, 1)
    },
})

-- GetItems 是文档写法，Find 在部分 Resolve 版本上不存在，两级取不到就返回 nil 并由调用方报原因
local function find_item(id)
    if not win then return nil end
    local ok_items, items = pcall(function() return win:GetItems() end)
    if ok_items and items then
        local ok_child, child = pcall(function() return items[id] end)
        if ok_child and child then return child end
    end
    if win.Find then
        local ok_found, found = pcall(function() return win:Find(id) end)
        if ok_found and found then return found end
    end
    return nil
end

-- TextEdit 的正文在 UIManager 上是 PlainText，.Text 取不到（SubFix 主程序同款读取顺序）
local function read_plain_text(item)
    if not item then return nil, "控件不存在" end
    local ok_plain, plain = pcall(function() return item.PlainText end)
    if ok_plain and type(plain) == "string" then return plain end
    local ok_text, text = pcall(function() return item.Text end)
    if ok_text and type(text) == "string" then return text end
    return nil, "读取正文失败（PlainText/Text 均不可用）"
end

local function set_status(html)
    local label = find_item("StatusLabel")
    if label then
        pcall(function() label.Text = html end)
    end
end

local function set_button_text(text)
    local button = find_item("StartBtn")
    if button then
        pcall(function() button.Text = text end)
    end
end

local function current_mode_index()
    local combo = find_item("UsageCombo")
    if not combo then return 0, "用途下拉框取不到" end
    local ok, index = pcall(function() return tonumber(combo.CurrentIndex) end)
    if ok and index then return index, nil end
    return 0, "用途下拉框当前项读取失败"
end

local combo = find_item("UsageCombo")
if combo then
    for _, label in ipairs({
        "自动（直出优先，失败自动转录）",
        "仅校对（转录后按文稿校对）",
        "照稿直出（失败报原因）"
    }) do
        pcall(function() combo:AddItem(label) end)
    end
    pcall(function() combo.CurrentIndex = 0 end)
else
    log_stage("建窗", "UsageCombo 控件不存在，用途固定为「自动」")
end

-- 绑定失败（控件 ID 没注册上）必须留痕，否则症状就是「点了没反应」
local function bind_click(id, handler)
    local ok, err = pcall(function() win.On[id].Clicked = handler end)
    if not ok then
        log_stage("绑定失败", id .. ": " .. tostring(err))
    end
    return ok
end

bind_click("StartBtn", function(ev)
    log_stage("点击开始", "进入处理器")
    local script_text, read_err = read_plain_text(find_item("ScriptInput"))
    if read_err then
        log_stage("读取文稿", read_err)
        set_status("<font color='#FF4D4F'>读不到文稿内容：" .. read_err .. "</font>")
        return
    end
    local mode_index, mode_err = current_mode_index()
    if mode_err then log_stage("读取用途", mode_err) end
    local mode = MODE_IDS[mode_index + 1] or "auto"
    local compact = script_text:gsub("%s", "")
    log_stage("读取完成", "mode=" .. mode .. " 有效" .. #compact .. "字")
    if compact == "" then
        set_status("<font color='#FAAD14'>先把文稿粘进上面的框里，再点「开始匹配」。</font>")
        return
    end
    set_button_text("匹配中…")
    set_status("<font color='#9a9a9a'>正在截取选区音频并匹配，进度看 SubFix 进度窗…</font>")
    chosen = {
        script_text = (script_text:gsub("\r\n", "\n"):gsub("\r", "\n")),
        script_direct_mode = mode
    }
    disp:ExitLoop()
end)

bind_click("CancelBtn", function(ev)
    disp:ExitLoop()
end)

local ok_close, close_err = pcall(function()
    win.On.ScriptMatchWin.Close = function(ev)
        log_stage("窗口关闭", "用户点了 X")
        disp:ExitLoop()
    end
end)
if not ok_close then
    log_stage("绑定失败", "ScriptMatchWin.Close: " .. tostring(close_err))
end

-- 打开窗口的同时把识别服务拉起来预热：模型冷加载要 1-2 分钟，
-- 让用户贴稿打字的那段时间去焐热模型，而不是点完「开始匹配」再干等。
local core_root = resolve_generate_core_root(script_dir())
local core_module = nil
pcall(function() core_module = load_generate_core(core_root) end)
local prewarmed = false
if core_module and type(core_module.prewarm_asr_service) == "function" then
    local ok_call, ok_result = pcall(function() return core_module.prewarm_asr_service() end)
    prewarmed = ok_call and ok_result == true
end
log_stage("预热", prewarmed and "已拉起识别服务" or "跳过（环境未就绪或服务已在运行）")

log_stage("显示窗口前")
local ok_show, show_err = pcall(function() win:Show() end)
if not ok_show then
    log_stage("Show失败", show_err)
    print("[SubFix 文稿匹配] 窗口显示失败: " .. tostring(show_err))
    return
end
local ok_loop, loop_err = pcall(function() disp:RunLoop() end)
if not ok_loop then
    log_stage("RunLoop失败", loop_err)
end
pcall(function() win:Hide() end)
log_stage("循环结束", chosen and "已选择" or (ok_loop and "用户取消" or "事件循环异常，未能派发点击"))

if not chosen then
    print("[SubFix 文稿匹配] 未完成：没拿到文稿（看 " .. LOG_PATH .. "）")
    return
end

local function notify(title, body_html, button_text)
    local notice = disp:AddWindow({
        ID = "ScriptMatchNotice",
        WindowTitle = title,
        Geometry = {300, 200, 560, 220},
        Spacing = 8,
    }, ui_impl:VGroup{
        ui_impl:Label{ID = "NoticeLabel", Text = body_html, Weight = 1, WordWrap = true},
        ui_impl:Button{ID = "NoticeCloseBtn", Text = button_text, Weight = 0, MinimumSize = {120, 28}}
    })
    if not notice then
        print("[SubFix 文稿匹配] " .. title .. ": " .. body_html)
        return
    end
    pcall(function()
        notice.On.NoticeCloseBtn.Clicked = function(ev)
            disp:ExitLoop()
        end
        notice.On.ScriptMatchNotice.Close = function(ev)
            disp:ExitLoop()
        end
    end)
    pcall(function() notice:Show() end)
    pcall(function() disp:RunLoop() end)
    pcall(function() notice:Hide() end)
end

local script_file_path = nil
local ok, err = pcall(function()
    if not core_module then
        core_module = load_generate_core(core_root)
    end
    script_file_path = write_script_temp_file(chosen.script_text)
    if not script_file_path then
        error("无法写文稿临时文件（检查 %TEMP% 可写）")
    end
    log_stage("调管线前", "core.run 开始 root=" .. core_root)

    local run_ok, run_err = core_module.run({
        script_root = core_root,
        target_subtitle_track = 1,
        script_file_path = script_file_path,
        script_direct_mode = chosen.script_direct_mode
    })
    if not run_ok then
        error(run_err or "文稿匹配失败")
    end
end)
-- 临时文件里是用户整段文稿，成功/失败/抛错都要删
if script_file_path then
    pcall(function() os.remove(script_file_path) end)
end

if not ok then
    local message = tostring(err or "")
    log_stage("文稿匹配执行失败", message)
    local hint
    if message:find("无法加载生成模块", 1, true) then
        hint = "SubFix 的 .subfix_support 目录没跟脚本放在同一层，重装或重新执行安装 bat。"
    elseif message:find("质量门", 1, true) then
        hint = "没有照稿念的部分会被质量门拦截。把用途改为「自动」或「仅校对」：转录后同音错别字按文稿校对，未照稿部分保留识别结果。"
    elseif message:find("AUDIO_TOO_LONG", 1, true) then
        hint = "直出仅支持 5 分钟内音频；更长的素材请用「自动」或「仅校对」。"
    else
        hint = "详细过程在 " .. LOG_PATH
    end
    local safe_message = message:gsub("&", "&amp;"):gsub("<", "&lt;")
    notify("文稿匹配 — 未完成",
        "<font color='#FF4D4F'><b>" .. safe_message .. "</b></font>\n<font color='#FAAD14'>" .. hint .. "</font>",
        "知道了")
    print("[SubFix 文稿匹配] 未完成: " .. message)
else
    log_stage("管线完成", "字幕已写入")
    notify("文稿匹配 — 完成",
        "<font color='#00AA55'><b>文稿匹配完成，字幕已写入时间线。</b></font>",
        "完成")
end
