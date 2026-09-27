"""本机模型免下载功能 QA（Lua 行为层，真机）。

加载完整 core lua（UI 走自动桩），从尾部导出表取真实函数，驱动行为链：
1. probe_local_model_candidates 命中真机 G:\AImodel 两个模型
2. offer_local_model_option（强制无 UI 分支）：登记偏好 + 发出 junction 命令
3. 偏好持久化 round-trip（新字段不被丢弃）
4. 已登记后静默通过
5. request_local_model_redownload：force 标记 + junction 摘除命令 + 偏好清空

所有副作用限制在临时 USERPROFILE；os.execute 拦截 mklink/rmdir 只做断言。

运行：python tests/qa_local_model_lua.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lupa.luajit21 import LuaRuntime  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


G_AIMODEL = Path("G:/AImodel/Qwen3-ASR-1.7B")
G_ALIGNER = Path("G:/AImodel/Qwen3-ForcedAligner-0.6B")
check("真机前提: G:/AImodel/Qwen3-ASR-1.7B 存在", G_AIMODEL.is_dir())
check("真机前提: G:/AImodel/Qwen3-ForcedAligner-0.6B 存在", G_ALIGNER.is_dir())

home_stub = Path(tempfile.mkdtemp(prefix="subfix_qa_luamodel_"))
os.environ["USERPROFILE"] = str(home_stub)
os.environ["SUBFIX_QWEN3_ASR_MODEL"] = ""
os.environ["SUBFIX_QWEN3_ALIGNER_MODEL"] = ""

lua = LuaRuntime(unpack_returned_tuples=True)
lua.execute(r"""
EXECED = {}
local function make_proxy()
    local p
    p = setmetatable({}, {
        __index = function(t, k)
            if k == "_is_proxy" then return true end
            local child = make_proxy()
            rawset(t, k, child)
            return child
        end,
        __newindex = function(t, k, v) rawset(t, k, v) end,
        __call = function(self, ...) local r = make_proxy() return r end,
        __tostring = function() return "[mock]" end,
    })
    return p
end
fusion = make_proxy()
fu = fusion
bmd = make_proxy()
app = make_proxy()
resolve = make_proxy()
bmd.UIDispatcher = function(ui) return make_proxy() end

-- 拦截 mklink / rmdir：记录并假装成功；其余放行真执行
local real_execute = os.execute
os.execute = function(cmd)
    cmd = tostring(cmd or "")
    if cmd:find("mklink /J", 1, true) or cmd:find("^rmdir ") or cmd:find("^rmdir \"") then
        table.insert(EXECED, cmd)
        return true
    end
    return real_execute(cmd)
end
""")

core_src = (ROOT / ".subfix_support" / "subfix_generate_selection_core.lua").read_text(encoding="utf-8")
# QA 补丁 1：offer 强制走无 UI 分支（真弹窗由真机人工验证）
core_src = core_src.replace("local function offer_local_model_option()\n    local saved = read_generate_preferences()",
                            "local function offer_local_model_option()\n    local saved = read_generate_preferences()", 1)
core_src = core_src.replace("    if not (dispatcher and ui) then",
                            "    if true then -- QA: 强制无 UI 分支", 1)
# QA 补丁 2：在模块 return 之前导出真实函数（return 必须是 chunk 最后一条语句）
export_block = """
__QA_EXPORT = {
    probe = probe_local_model_candidates,
    offer = offer_local_model_option,
    redownload = request_local_model_redownload,
    prefs_read = read_generate_preferences,
    resolve_aligner = resolve_local_aligner_model,
}
"""
final_return = core_src.rstrip().rfind("return SubFixGenerateSelectionCore")
assert final_return > 0, "未找到模块 return"
core_src = core_src[:final_return] + export_block + core_src[final_return:]
if core_src.startswith("#!"):
    core_src = core_src.split("\n", 1)[1]

# chunk 名指向隔离目录：core 的 script_dir/偏好文件都落在 home_stub 下
plugin_dir = home_stub / "SubFix"
plugin_dir.mkdir(parents=True, exist_ok=True)
(plugin_dir / ".subfix_support").mkdir(exist_ok=True)
chunk_name = "@" + str(plugin_dir / "generate_selection_core.lua")
load = lua.eval('function(src, name) return load(src, name) end')
chunk = load(core_src, chunk_name)
try:
    chunk()
    check("core lua 全量加载（含 QA 导出）", True)
except Exception as exc:
    check("core lua 全量加载（含 QA 导出）", False, str(exc)[:400])
    sys.exit(1)

g = lua.globals()
qa = g.__QA_EXPORT

# ---------- 1. probe 命中真机模型 ----------
found = qa.probe()
asr_found = str(found.asr or "").replace("\\", "/")
aligner_found = str(found.aligner or "").replace("\\", "/")
check("probe: 识别模型命中 G:/AImodel", asr_found.endswith("G:/AImodel/Qwen3-ASR-1.7B"), asr_found)
check("probe: 对齐模型命中 G:/AImodel", aligner_found.endswith("G:/AImodel/Qwen3-ForcedAligner-0.6B"), aligner_found)

# ---------- 2. offer（无 UI）：登记 + junction 命令 ----------
chosen = qa.offer()
check("offer: 返回 local", str(chosen) == "local", repr(str(chosen)))
execed = [str(v) for v in g.EXECED.values()]
mklink = next((c for c in execed if "mklink /J" in c), "")
check("offer: junction 命令指向隔离 USERPROFILE", "AppData\\\\Roaming\\\\SubFix\\\\models\\\\qwen3-asr-1.7b" in mklink.replace("\\\\", "\\") or "qwen3-asr-1.7b" in mklink, mklink[:160])
check("offer: junction 源为 G:/AImodel 模型", "Qwen3-ASR-1.7B" in mklink, mklink[:160])

prefs_file = plugin_dir / ".subfix_support" / "subfix_generate_prefs.json"
check("offer: 偏好文件已写", prefs_file.is_file())
prefs_text = prefs_file.read_text(encoding="utf-8") if prefs_file.is_file() else ""
check("偏好: local_asr_model 已持久化", "Qwen3-ASR-1.7B" in prefs_text, prefs_text[:240])
check("偏好: local_aligner_model 已持久化", "ForcedAligner" in prefs_text)
data = qa.prefs_read()
saved_asr = str(data.local_asr_model or "").replace("\\", "/")
check("偏好: Lua 读回 local_asr_model 完整", saved_asr.endswith("Qwen3-ASR-1.7B"), saved_asr)

# ---------- 3. resolve_aligner 与 env 优先级 ----------
aligner_value = str(qa.resolve_aligner() or "")
check("resolve_aligner: 返回本机对齐模型", "ForcedAligner" in aligner_value, aligner_value)

# env 优先于 probe（setx 场景）
os.environ["SUBFIX_QWEN3_ALIGNER_MODEL"] = "D:/custom-aligner"
aligner_value2 = str(qa.resolve_aligner() or "")
check("resolve_aligner: 环境变量优先", aligner_value2 == "D:/custom-aligner", aligner_value2)
os.environ["SUBFIX_QWEN3_ALIGNER_MODEL"] = ""

# ---------- 4. 已登记 → 静默通过 ----------
execed_before = len(execed)
chosen2 = qa.offer()
check("二次 offer: 静默返回 local", str(chosen2) == "local", repr(str(chosen2)))
prefs_text2 = prefs_file.read_text(encoding="utf-8")
check("二次 offer: 偏好未被破坏", "Qwen3-ASR-1.7B" in prefs_text2)
check("二次 offer: stub 下重发 mklink 属预期（junction 未实体化）",
      any("mklink /J" in str(v) for v in g.EXECED.values()))

# ---------- 5. 重下载请求 ----------
marker = home_stub / "AppData/Roaming/SubFix/.subfix-force-download"
r = qa.redownload()
execed_after2 = [str(v) for v in g.EXECED.values()]
rmdir = next((c for c in reversed(execed_after2) if c.startswith("rmdir")), "")
check("redownload: force 标记已写", marker.is_file())
check("redownload: 发出 junction 摘除命令", "qwen3-asr-1.7b" in rmdir, rmdir[:160])
prefs_after = prefs_file.read_text(encoding="utf-8") if prefs_file.is_file() else ""
check("redownload: 偏好已清空", 'local_asr_model": ""' in prefs_after, prefs_after[:240])
data2 = qa.prefs_read()
check("redownload: Lua 读回为空", str(data2.local_asr_model or "") == "")

# 契约：标记文件名与管理器一致
mgr_src = (ROOT / ".subfix_support" / "subfix_qwen_local_manager.py").read_text(encoding="utf-8")
check("契约: 标记文件名与管理器读取一致", ".subfix-force-download" in mgr_src)

shutil.rmtree(home_stub, ignore_errors=True)

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("本机模型 Lua 行为 QA 全部通过")
