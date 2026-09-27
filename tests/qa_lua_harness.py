"""SubFix.lua 全量加载 QA（安装布局版）：用自动桩模拟 Fusion UI，加载完整脚本后驱动真实函数。

加载的是从发行 ZIP 解压出的真实安装布局：
    <stage>/SubFix/SubFix.lua          ← chunk 位置（等价 Utility/SubFix/）
    <stage>/.subfix_support/...        ← 等价 Utility/.subfix_support/

运行：python tests/qa_lua_harness.py
"""
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "SubFix-v3.3.0-Windows.zip"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lupa.luajit21 import LuaRuntime  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run_capture(cmd):
    """bytes 捕获 + GBK 解码，避免控制台工具的 ANSI 输出炸掉 UTF-8 管道。"""
    r = subprocess.run(cmd, capture_output=True)
    return (r.stdout + r.stderr).decode("gbk", "replace")


# ---------- 0. 准备真实安装布局 ----------
stage = Path(tempfile.mkdtemp(prefix="subfix_qa_layout_"))
with zipfile.ZipFile(ZIP) as zf:
    zf.extractall(stage)
lua_entry = stage / "SubFix" / "SubFix.lua"
support = stage / ".subfix_support"
check("安装布局: SubFix/SubFix.lua 存在", lua_entry.exists())
check("安装布局: .subfix_support/subfix_asr_transcribe.py 存在", (support / "subfix_asr_transcribe.py").exists())
check("安装布局: .subfix_support/setup_asr_env.cmd 存在", (support / "setup_asr_env.cmd").exists())

# ---------- 1. 全量加载 ----------
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
            rawset(result, "_called_with", {...})
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

source = lua_entry.read_text(encoding="utf-8")
if source.startswith("#!"):
    source = source.split("\n", 1)[1]
chunk_name = "@" + str(lua_entry).replace("\\", "/")
load = lua.eval('function(src, name) return load(src, name) end')
loaded = load(source, chunk_name)
check("安装布局: SubFix.lua 编译", loaded is not None)
try:
    loaded()
    check("安装布局: SubFix.lua 完整加载与全部函数定义", True)
except Exception as exc:
    check("安装布局: SubFix.lua 完整加载与全部函数定义", False, str(exc)[:400])
    sys.exit(1)

g = lua.globals()

# ---------- 2. 安装布局下的路径解析 ----------
paths = g.SUBFIX_AUDIO_ALIGN.get_asr_paths()
helper = str(paths.helper).replace("\\", "/")
py = str(paths.python).replace("\\", "/")
setup = str(paths.setup).replace("\\", "/")
check("get_asr_paths: helper 指向安装布局 .subfix_support",
      helper == str((stage / ".subfix_support" / "subfix_asr_transcribe.py")).replace("\\", "/"), helper)
check("get_asr_paths: setup 指向安装布局 setup_asr_env.cmd",
      setup == str((stage / ".subfix_support" / "setup_asr_env.cmd")).replace("\\", "/"), setup)
check("get_asr_paths: python 候选为 Scripts/python.exe",
      py.endswith(".subfix_support/.subfix_asr_env/Scripts/python.exe"), py)

# ---------- 3. ffmpeg 解析（where 多行 + 失效条目回归） ----------
ff = g.SUBFIX_AUDIO_ALIGN.resolve_ffmpeg_binary()
if ff is not None:
    ff_path = str(ff)
    check("resolve_ffmpeg_binary: 返回单行真实存在的路径",
          "\n" not in ff_path and os.path.isfile(ff_path), ff_path)
else:
    check("resolve_ffmpeg_binary: 无可用 ffmpeg 时返回 nil", True)

# ---------- 4. 备份列表（dir /b /o-d 分支） ----------
backup_dir = Path(tempfile.mkdtemp(prefix="subfix_qa_backup_"))
for name in ("Backup_20260101_120000.srt", "Backup_20260102_090000.srt", "ignore.txt"):
    (backup_dir / name).write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
    time.sleep(0.03)
g.current_backup_path = str(backup_dir)
files = g.list_backup_files(10)
names = [str(f) for f in files.values()] if files else []
check("list_backup_files: 只列 Backup_*.srt", len(names) == 2 and all(n.endswith(".srt") for n in names), str(names))
check("list_backup_files: 返回完整可访问路径", all(os.path.isfile(n) for n in names), str(names))

# ---------- 5. ensure_dir：ASCII 成功 / 中文明确失败提示 ----------
ascii_dir = Path(tempfile.gettempdir()) / "subfix_qa_mkdir_ok" / "inner"
r = g.subfix_ensure_dir(str(ascii_dir).replace("\\", "/"))
check("ensure_dir: ASCII 多级目录创建成功", ascii_dir.is_dir() and r is not None)

cn_dir = Path(tempfile.gettempdir()) / "subfix_qa_mkdir_中文路径" / "inner"
print("  （中文路径：cmd/io 均 GBK 解读同一 UTF-8 字节串 → 内部自洽但系统侧目录名乱码，属已知限制）")
r_cn = g.subfix_ensure_dir(str(cn_dir).replace("\\", "/"))
check("ensure_dir: 中文路径不崩溃（返回布尔，功能自洽）", r_cn is True or r_cn is False, repr(r_cn))
check("ensure_dir: 证实边界——python 视角正确 Unicode 名不存在（乱码名）", not cn_dir.exists())

# ---------- 6. kill_tree 真实终止进程树 ----------
work = Path(tempfile.mkdtemp(prefix="subfix_qa_kill_"))
batch = work / "kill_e2e.cmd"
pid_file = work / "pid"
done_marker = work / "done"
py_exe = sys.executable
lines = [
    "@echo off",
    "powershell -NoProfile -Command \"(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID)).ParentProcessId | Set-Content -LiteralPath '" + str(pid_file).replace('/', '\\') + "'\" >nul 2>&1",
    f'"{py_exe}" -c "import time; time.sleep(60)" >nul 2>&1',
    f'type nul > "{done_marker}"',
]
batch.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
subprocess.run(f'start "SubFixQA" /b cmd /c "{batch}"', shell=True, cwd=str(work))
deadline = time.time() + 20
while time.time() < deadline and not pid_file.exists():
    time.sleep(0.1)
pid = pid_file.read_text().strip() if pid_file.exists() else ""
check("kill_tree: 拿到 cmd.exe PID", pid.isdigit(), repr(pid))
if pid.isdigit():
    time.sleep(0.5)
    before = run_capture(["tasklist", "/FI", f"PID eq {pid}"])
    check("kill_tree: 目标 cmd 存活确认", pid in before, before[:150])
    g.subfix_kill_tree(pid)
    time.sleep(2)
    after = run_capture(["tasklist", "/FI", f"PID eq {pid}"])
    check("kill_tree: cmd.exe 已被终止", pid not in after, after[:150])
    check("kill_tree: 子进程被连带终止（done 未写出）", not done_marker.exists())

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("Lua harness QA（安装布局）全部通过")
