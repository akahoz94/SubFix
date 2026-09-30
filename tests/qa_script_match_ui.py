"""文稿匹配.lua 无头 UI 回归：用 lupa 桩模拟 fusion.UIManager/bmd.UIDispatcher，真的派发一次点击。

覆盖三类历史故障（都是「点了没反应」这一症状的不同成因）：
  1. chunk 名前导 @ 没剥离 → .subfix_support 核心模块永远探测不到，整条功能链失效
  2. 用 .Text 读 TextEdit → 永远读成空 → 走「请先粘贴文稿」分支，看上去就是没反应
  3. win:Find 在部分上下文不存在 → 处理器第一句就抛错，被 pcall 静默吞掉

桩环境刻意比 Resolve 严格：事件处理器里抛错会直接冒到 Python，Resolve 是吞掉的。

运行：python tests/qa_script_match_ui.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "文稿匹配.lua"
REAL_CORE = ROOT / ".subfix_support" / "subfix_generate_selection_core.lua"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lupa.luajit21 import LuaRuntime  # noqa: E402

failures = []
PASTE = "这是第一段文稿。\n这是第二段，带标点、换行。"


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------- fusion / bmd 桩 ----------
STUB = r"""
local function make_control_ctor(kind)
    return function(self, first)
        local node = (type(first) == "table") and first or {}
        node._kind = kind
        return node
    end
end

fusion = {}
fusion.UIManager = setmetatable({}, {
    __index = function(self, kind) return make_control_ctor(tostring(kind)) end
})
fu = fusion

local function new_event_namespace()
    local per_id = {}
    return setmetatable({}, {
        __index = function(t, id)
            if not per_id[id] then
                per_id[id] = setmetatable({}, {__index = function(inner, ev) return rawget(inner, ev) end})
            end
            return per_id[id]
        end
    })
end

UIDEST = {}

local function collect_ids(container, out)
    for _, value in pairs(container) do
        if type(value) == "table" and value._kind then
            if value.ID then out[value.ID] = value end
            collect_ids(value, out)
        end
    end
end

local function new_window(props, content)
    local by_id = {}
    collect_ids({content}, by_id)
    for _, node in pairs(by_id) do
        if node._kind == "ComboBox" then
            node.__added = {}
            node.AddItem = function(self, text) node.__added[#node.__added + 1] = tostring(text) end
        end
    end
    local win = {}
    win.__items = by_id
    win.On = new_event_namespace()
    function win:GetItems() return by_id end
    if UIDEST.find then
        function win:Find(id) return by_id[id] end
    end
    function win:Show() UIDEST.last = win; UIDEST.first = UIDEST.first or win; return true end
    function win:Hide() return true end
    function win:Resize(s) return true end
    function win:RecalcLayout() return true end
    return win
end

bmd = {}
function bmd.UIDispatcher(ui)
    local disp = {}
    disp.__windows = {}
    disp.__loops = 0
    disp.On = new_event_namespace()
    function disp:AddWindow(props, content)
        local win = new_window(props, content)
        disp.__windows[#disp.__windows + 1] = win
        return win
    end
    function disp:RunLoop()
        disp.__loops = disp.__loops + 1
        if UIDEST.on_loop then UIDEST.on_loop(disp, disp.__loops, disp.__windows[1]) end
        return true
    end
    function disp:ExitLoop() return true end
    return disp
end
"""

# 第一次 RunLoop 里模拟用户贴稿 + 点「开始匹配」；之后的 RunLoop 属于结果提示窗
SCENARIO = r"""
UIDEST.find = %(find)s
UIDEST.paste = %(paste_literal)s
UIDEST.combo_index = %(combo_index)s
UIDEST.prop = "%(prop)s"
UIDEST.on_loop = function(disp, loop_index, main_win)
    if loop_index > 1 then
        for i = 2, #disp.__windows do
            local notice = disp.__windows[i]
            local closer = notice.On.NoticeCloseBtn.Clicked
            if closer then closer({}) end
            return
        end
        return
    end
    local items = main_win:GetItems()
    local input = items.ScriptInput
    if input then
        if UIDEST.prop ~= "" then
            input[UIDEST.prop] = UIDEST.paste
        else
            -- 模拟绑定只肯给非字符串（真实故障里 .Text 会返回非 string）
            input.PlainText = nil
            input.Text = 0
        end
    end
    if items.UsageCombo then
        items.UsageCombo.CurrentIndex = UIDEST.combo_index
    end
    local handler = main_win.On.StartBtn.Clicked
    if not handler then error("StartBtn.Clicked 没绑上") end
    handler({})
end
"""


def build_stage(tmp: Path) -> Path:
    """复刻真实安装布局：Utility/SubFix/文稿匹配.lua + Utility/.subfix_support/核心(桩)"""
    scripts = tmp / "Utility"
    entry_dir = scripts / "SubFix"
    entry_dir.mkdir(parents=True, exist_ok=True)
    entry = entry_dir / "文稿匹配.lua"
    shutil.copyfile(ENTRY, entry)
    support = scripts / ".subfix_support"
    support.mkdir(exist_ok=True)
    (support / "subfix_generate_selection_core.lua").write_text(
        """
local marker_path = os.getenv("SUBFIX_TEST_MARKER")
return {
    run = function(opts)
        local fh = io.open(marker_path, "wb")
        if not fh then return false, "无法写测试标记" end
        fh:write("mode=" .. tostring(opts.script_direct_mode) .. "\\n")
        fh:write("root=" .. tostring(opts.script_root) .. "\\n")
        fh:write("track=" .. tostring(opts.target_subtitle_track) .. "\\n")
        local temp_path = tostring(opts.script_file_path or "")
        fh:write("temp=" .. temp_path .. "\\n")
        local sf = io.open(temp_path, "rb")
        if sf then
            local body = sf:read("*a")
            sf:close()
            fh:write("bytes=" .. #body .. "\\n")
            fh:write("text=" .. body .. "\\n")
        else
            fh:write("bytes=-1\\n")
        end
        fh:close()
        return true
    end
}
""",
        encoding="utf-8",
    )
    return entry


def run_entry(entry: Path, temp_dir: Path, *, prop="PlainText", find=True,
              paste=PASTE, combo_index=0):
    marker = temp_dir / "marker.txt"
    log = temp_dir / "subfix_script_match_error.log"
    for stale in (marker, log):
        if stale.exists():
            stale.unlink()
    os.environ["SUBFIX_TEST_MARKER"] = str(marker)
    os.environ["TEMP"] = str(temp_dir)
    os.environ["TMP"] = str(temp_dir)

    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(STUB)
    lua.execute(SCENARIO % {
        "find": "true" if find else "false",
        "paste_literal": lua_to_lua_string(lua, paste),
        "combo_index": combo_index,
        "prop": prop,
    })

    source = entry.read_text(encoding="utf-8")
    if source.startswith("#!"):
        source = source.split("\n", 1)[1]
    # Resolve 交给 Lua 的 chunk 名就是 "@绝对路径"，这里一比一还原
    load = lua.eval("function(src, name) return load(src, name) end")
    compiled = load(source, "@" + str(entry).replace("\\", "/"))
    if compiled is None:
        return {}, None, "编译失败"
    error = None
    try:
        compiled()
    except Exception as exc:
        error = str(exc)[:400]

    marker_text = marker.read_text(encoding="utf-8", errors="replace") if marker.exists() else None
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else None
    kv = {}
    status = None
    added = None
    probe_errors = []
    if marker_text and "text=" in marker_text:
        kv["text"] = marker_text.split("text=", 1)[1]
    for line in (marker_text or "").splitlines():
        if line.startswith("text="):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            kv[key] = value
    try:
        status = str(lua.execute('return UIDEST.first and UIDEST.first.__items.StatusLabel and UIDEST.first.__items.StatusLabel.Text or nil') or "")
    except Exception as exc:
        probe_errors.append("status: " + str(exc)[:120])
    try:
        added = int(lua.execute('local c = UIDEST.first.__items.UsageCombo; if not c then return -2 end; if not c.__added then return -3 end; return #c.__added'))
    except Exception as exc:
        probe_errors.append("added: " + str(exc)[:120])
    kv["_status"] = status
    kv["_added"] = added
    kv["_probe"] = "; ".join(probe_errors)
    return kv, log_text, error


def lua_to_lua_string(lua, text):
    """把带换行的中文串安全嵌进 Lua 源码"""
    escaped = (text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))
    return f'"{escaped}"'


def parse_marker(pairs):
    return pairs


def main():
    print(f"入口脚本: {ENTRY}")
    check("入口脚本存在", ENTRY.exists())
    if not ENTRY.exists():
        sys.exit(1)

    # ---- 1. 正常链路 ----
    tmp1 = Path(tempfile.mkdtemp(prefix="subfix_sm_ok_"))
    entry1 = build_stage(tmp1)
    kv, log, err = run_entry(entry1, tmp1)
    check("正常链路无异常", err is None, err or "")

    # 同一份脚本也要能在开发树里跑通（.subfix_support 与脚本同级）
    dev_stage = tmp1 / "devtree"
    dev_stage.mkdir()
    dev_entry = dev_stage / "文稿匹配.lua"
    shutil.copyfile(ENTRY, dev_entry)
    (dev_stage / ".subfix_support").mkdir()
    shutil.copyfile(entry1.parent.parent / ".subfix_support" / "subfix_generate_selection_core.lua",
                    dev_stage / ".subfix_support" / "subfix_generate_selection_core.lua")
    kv_dev, _, err_dev = run_entry(dev_entry, dev_stage)
    check("开发树同级布局同样跑通", err_dev is None and kv_dev.get("mode") == "auto",
          (err_dev or "") + " root=" + str(kv_dev.get("root")))
    check("script_root = 脚本所在目录（同级 .subfix_support）",
          kv_dev.get("root", "").replace("\\", "/").endswith("devtree"), kv_dev.get("root", ""))
    check("核心桩被跑到（说明 @ 已剥离、.subfix_support 找得到）", bool(kv.get("root")), "marker 缺失；日志=" + (log or "")[:300])
    check("script_root 不含前导 @", not (kv.get("root", "@")).startswith("@"), kv.get("root", ""))
    check("script_root = Utility（父级 .subfix_support 回退生效）",
          kv.get("root", "").replace("\\", "/").endswith("Utility"), kv.get("root", ""))
    check("默认用途 = auto", kv.get("mode") == "auto", kv.get("mode", ""))
    check("目标轨 = 1", kv.get("track") == "1", kv.get("track", ""))
    check("文稿按 UTF-8 原样传给管线", "这是第一段文稿" in kv.get("text", ""), repr(kv.get("text", "")[:60]))
    check("换行已归一为 \\n", "\r" not in kv.get("text", ""), repr(kv.get("text", "")[:60]))
    temp_path = kv.get("temp", "")
    check("临时文稿用完即删", bool(temp_path) and not Path(temp_path).exists(), temp_path)
    check("用途下拉框填了 3 项", kv.get("_added") == 3, str(kv.get("_added")))
    check("日志含点击与完成", ("点击开始" in (log or "")) and ("管线完成" in (log or "")), (log or "")[-200:])

    # ---- 2. 用途 3 = 照稿直出 ----
    tmp2 = Path(tempfile.mkdtemp(prefix="subfix_sm_direct_"))
    kv2, _, err2 = run_entry(build_stage(tmp2), tmp2, combo_index=2)
    check("第三项映射为 direct", err2 is None and kv2.get("mode") == "direct", str(kv2.get("mode")) + " | " + (err2 or ""))

    # ---- 3. 第二项 = 仅校对 ----
    tmp3 = Path(tempfile.mkdtemp(prefix="subfix_sm_proof_"))
    kv3, _, err3 = run_entry(build_stage(tmp3), tmp3, combo_index=1)
    check("第二项映射为 proofread", err3 is None and kv3.get("mode") == "proofread", str(kv3.get("mode")) + " | " + (err3 or ""))

    # ---- 4. 没有 Find 的绑定（只靠 GetItems）----
    tmp4 = Path(tempfile.mkdtemp(prefix="subfix_sm_nofind_"))
    kv4, log4, err4 = run_entry(build_stage(tmp4), tmp4, find=False)
    check("win.Find 不存在时依然跑通", err4 is None and bool(kv4.get("root")), (err4 or "") + " root=" + str(kv4.get("root")))

    # ---- 5. TextEdit 只暴露 .Text 的旧绑定 ----
    tmp5 = Path(tempfile.mkdtemp(prefix="subfix_sm_text_"))
    kv5, _, err5 = run_entry(build_stage(tmp5), tmp5, prop="Text")
    check("只有 Text 时回退读取成功", err5 is None and "这是第一段文稿" in kv5.get("text", ""), (err5 or "") + repr(kv5.get("text", "")[:40]))

    # ---- 6. 两个读取口都拿不到：必须报因，不能再表现为「没反应」----
    tmp6 = Path(tempfile.mkdtemp(prefix="subfix_sm_noread_"))
    kv6, log6, err6 = run_entry(build_stage(tmp6), tmp6, prop="")
    check("读不到正文时不进管线", not kv6.get("root"), str(kv6.get("root")))
    check("读不到正文时日志报因", "读取文稿" in (log6 or ""), (log6 or "")[:200])
    check("读不到正文时界面给提示", "读不到文稿" in (kv6.get("_status") or ""), str(kv6.get("_status")))

    # ---- 7. 空文稿：提示但不进管线 ----
    tmp7 = Path(tempfile.mkdtemp(prefix="subfix_sm_empty_"))
    kv7, log7, err7 = run_entry(build_stage(tmp7), tmp7, paste="   \n  ")
    check("空文稿不进管线", not kv7.get("root"), str(kv7.get("root")) + " | " + (err7 or ""))
    check("空文稿日志记字数", "有效0字" in (log7 or ""), (log7 or "")[:200])
    check("空文稿界面提示", "先把文稿粘" in (kv7.get("_status") or ""), str(kv7.get("_status")))

    # ---- 8. 解析出来的真实核心模块可编译（拷到 ASCII 路径：LuaJIT 的 loadfile 走 ANSI，中文目录打不开）----
    if REAL_CORE.exists():
        tmp8 = Path(tempfile.mkdtemp(prefix="subfix_sm_core_"))
        ascii_core = tmp8 / "Utility" / ".subfix_support" / "subfix_generate_selection_core.lua"
        ascii_core.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REAL_CORE, ascii_core)
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(STUB)
        compile_chunk = lua.eval("""
function(core_path)
    local chunk, err = loadfile(core_path)
    if not chunk then return false, tostring(err) end
    return true, "ok"
end
""")
        ok, detail = compile_chunk(str(ascii_core).replace("\\", "/"))
        check("真实核心模块在解析路径上可编译", bool(ok), str(detail)[:200])
    else:
        check("真实核心模块存在", False, str(REAL_CORE))

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项: {failures}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
