"""FFI UTF-16 化 QA：验证中文路径在 Lua 兼容层下被根治。

用自动桩全量加载 SubFix.lua(安装布局,从发行 ZIP 解压),驱动真实函数。
核心标准:所有路径以 python(Unicode)视角验证——不再出现"乱码名"。
运行:python tests/qa_ffi_paths.py
"""
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "dist" / "SubFix-v3.4.0-Windows.zip"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lupa.luajit21 import LuaRuntime  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------- 0. 安装布局准备 ----------
stage = Path(tempfile.mkdtemp(prefix="subfix_ffi_layout_"))
with zipfile.ZipFile(ZIP) as zf:
    zf.extractall(stage)

# ---------- 1. 全量桩加载 ----------
lua = LuaRuntime(unpack_returned_tuples=True)
lua.execute(r"""
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
        __call = function(self, ...)
            local result = make_proxy()
            return result
        end,
        __tostring = function() return "[mock]" end,
    })
    return p
end
fusion = make_proxy()
fu = fusion
bmd = make_proxy()
app = make_proxy()
resolve = make_proxy()
dispatcher_proxy = make_proxy()
bmd.UIDispatcher = function(ui) return dispatcher_proxy end
""")
source = (stage / "SubFix" / "SubFix.lua").read_text(encoding="utf-8")
if source.startswith("#!"):
    source = source.split("\n", 1)[1]
chunk = lua.eval('function(src, name) return load(src, name) end')(source, "@" + str(stage / "SubFix" / "SubFix.lua"))
chunk()
g = lua.globals()
check("全量加载 + SUBFIX_WIN_FFI 启用", bool(g.SUBFIX_WIN_FFI))

# ---------- 2. 中文目录真实创建(根治验证:python Unicode 视角) ----------
root_cn = Path(tempfile.gettempdir()) / "subfix_ffi_qa_中文目录测试"
shutil.rmtree(root_cn, ignore_errors=True)
target = root_cn / "里层目录"
r = g.SUBFIX_AUDIO_ALIGN.file_exists(str(target))
check("file_exists(FFI): 不存在返回 false", r is False)
r = g.subfix_ensure_dir(str(target))
check("ensure_dir 返回 true", r is True, repr(r))
check("中文目录 python 视角真实存在(根治)", target.is_dir(), str(target))
check("file_exists(FFI): 中文目录存在", g.SUBFIX_AUDIO_ALIGN.file_exists(str(target)) is True)
check("file_exists(FFI): 中文文件名判定", (lambda p: (p.write_bytes(b"x"), g.SUBFIX_AUDIO_ALIGN.file_exists(str(p)))[1])(root_cn / "文件名字.txt"))

# ---------- 3. 环境变量与 temp root ----------
check("temp_root 是真实 TEMP 且 python 视角存在", Path(str(g.subfix_temp_root())).is_dir(), str(g.subfix_temp_root()))
check("home_dir 是真实 USERPROFILE", Path(str(g.subfix_home_dir())).is_dir(), str(g.subfix_home_dir()))

# ---------- 4. list_backup_files 中文目录枚举(根治验证) ----------
for name in ("Backup_20260101_120000.srt", "Backup_20260102_090000.srt", "别的.txt"):
    (root_cn / name).write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
    time.sleep(0.02)
g.current_backup_path = str(root_cn)
files = g.list_backup_files(10)
names = [str(f) for f in files.values()] if files else []
check("list_backup_files: 中文目录枚举 2 个 srt", len(names) == 2 and all(n.endswith(".srt") for n in names), str(names))
check("list_backup_files: python 视角文件真实存在", all(os.path.isfile(n) for n in names), str(names))

# ---------- 5. 后台批处理全流程在中文目录下跑通 ----------
work = root_cn / "后台任务目录"
work.mkdir(exist_ok=True)
batch = work / "bg.cmd"
stdout_f, pid_f, done_f, exit_f = work / "out", work / "pid", work / "done", work / "exit"
env_pairs = lua.table_from([{"name": "SUBFIX_QWEN3_ASR_MODEL", "value": "模型"}])
options = lua.table_from({
    "env_pairs": env_pairs,
    "pid_file": str(pid_f),
    "stdout_file": str(stdout_f),
    "exit_file": str(exit_f),
    "done_file": str(done_f),
})
py = sys.executable
ok = g.subfix_write_bg_batch(str(batch), f'"{py}" -c "print(\'ffi-ok\')"', options)
check("write_bg_batch 中文目录写入", bool(ok))
if ok:
    g.subfix_launch_bg_batch(str(batch))
    deadline = time.time() + 30
    while time.time() < deadline and not done_f.exists():
        time.sleep(0.2)
    check("中文目录批处理: done 生成", done_f.exists())
    check("中文目录批处理: stdout 捕获", stdout_f.exists() and "ffi-ok" in stdout_f.read_text(encoding="utf-8", errors="replace"))
    pid_text = pid_f.read_text().strip() if pid_f.exists() else ""
    check("中文目录批处理: pid 记录", pid_text.isdigit(), repr(pid_text))

shutil.rmtree(root_cn, ignore_errors=True)
shutil.rmtree(stage, ignore_errors=True)

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("FFI UTF-16 化 QA 全部通过")
