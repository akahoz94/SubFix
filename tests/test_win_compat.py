"""Windows 兼容层自检：Lua 语法、兼容层行为、后台批处理机制。

运行：python tests/test_win_compat.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lupa.luajit21 import LuaRuntime  # noqa: E402  # Resolve 内置即 LuaJIT(5.1)，不能用默认的 Lua 5.4 判语法

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def lua_source(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    # Lua 规范允许首行 shebang，但 lupa 的 compile 不跳过；Resolve 环境按规范跳过。
    if text.startswith("#!"):
        text = text.split("\n", 1)[1]
    return text


# ---------- 1. Lua 语法检查（LuaJIT = Lua 5.1 兼容，与 Resolve 内置一致） ----------
lua = LuaRuntime(unpack_returned_tuples=True)
for lua_file in ["SubFix.lua", "生成选区字幕.lua", ".subfix_support/subfix_generate_selection_core.lua"]:
    try:
        lua.compile(lua_source(lua_file))
        check(f"Lua 语法: {lua_file}", True)
    except Exception as exc:
        check(f"Lua 语法: {lua_file}", False, str(exc)[:300])

# ---------- 2. 兼容层行为（从 SubFix.lua 源码中截取真实代码执行） ----------
source = lua_source("SubFix.lua")
start = source.index("-- ========== Windows 兼容层 ==========")
end = source.index("SUBFIX_VERSION = ")
block = source[start:end]

test_lua = LuaRuntime(unpack_returned_tuples=True)
test_lua.execute("""
EXECUTED = {}
os.execute = function(cmd) table.insert(EXECUTED, tostring(cmd)) return true, nil, 0 end
bmd = nil  -- 模拟无 bmd.wait，走 ping 兜底
""")
try:
    test_lua.execute(block)
    check("兼容层加载执行", True)
except Exception as exc:
    check("兼容层加载执行", False, str(exc)[:300])

g = test_lua.globals()
check("SUBFIX_IS_WINDOWS 为 true（本机）", bool(g.SUBFIX_IS_WINDOWS))

# subfix_ensure_dir 在 Windows 上应发 cmd 版 mkdir
g.subfix_ensure_dir("C:/tmp dir/子目录")
lines = [str(v) for v in g.EXECUTED.values()]
check("subfix_ensure_dir 用 mkdir+反斜杠", any('mkdir "C:\\tmp dir\\子目录" 2>nul' in ln for ln in lines), str(lines[-1:]))

# subfix_kill_tree 应发 taskkill
g.subfix_kill_tree(4321)
lines = [str(v) for v in g.EXECUTED.values()]
check("subfix_kill_tree 用 taskkill /T /F", any("taskkill /PID 4321 /T /F" in ln for ln in lines), str(lines[-1:]))

# subfix_write_bg_batch 批处理内容（注意传 Lua table，不是 python dict）
tmp = Path(tempfile.mkdtemp(prefix="subfix_compat_"))
batch = tmp / "bg_1.cmd"
env_pairs = test_lua.table_from([{"name": "SUBFIX_QWEN3_ASR_MODEL", "value": "m"}])
options = test_lua.table_from({
    "env_pairs": env_pairs,
    "pid_file": str(tmp / "p"),
    "stdout_file": str(tmp / "o"),
    "exit_file": str(tmp / "e"),
    "done_file": str(tmp / "d"),
})
ok = g.subfix_write_bg_batch(str(batch), '"C:\\\\Python\\\\python.exe" helper.py --flag', options)
text = batch.read_text(encoding="utf-8") if ok else ""
check("subfix_write_bg_batch 返回 true", bool(ok))
check("批处理含 chcp 65001", "chcp 65001 >nul" in text, text[:200])
check("批处理清空 PYTHONHOME/PYTHONPATH", 'set "PYTHONHOME="' in text and 'set "PYTHONPATH="' in text)
check("批处理含 env_pairs", 'set "SUBFIX_QWEN3_ASR_MODEL=m"' in text)
check("批处理含 PID 记录(CIM ParentProcessId)", "ParentProcessId" in text)
check("批处理含退出码与完成标记", "echo %errorlevel%" in text and "type nul >" in text)

# ---------- 3. 后台批处理机制端到端（真实 cmd 执行，验证模板本身） ----------
work = Path(tempfile.mkdtemp(prefix="subfix_bg_e2e_"))
stdout_f = work / "out"
pid_f = work / "pid"
done_f = work / "done"
exit_f = work / "exit"
batch = work / "e2e.cmd"
py = sys.executable
cmd_line = f'"{py}" -c "print(\'e2e-ok\'); raise SystemExit(0)"'


def win(p):
    return str(p).replace("/", "\\")


lines = [
    "@echo off",
    "chcp 65001 >nul",
    'set "PYTHONHOME="',
    'set "PYTHONPATH="',
    f"powershell -NoProfile -Command \"(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID)).ParentProcessId | Set-Content -LiteralPath '{win(pid_f)}'\" >nul 2>&1",
    f'{cmd_line} > "{win(stdout_f)}" 2>&1',
    f'echo %errorlevel% > "{win(exit_f)}"',
    f'type nul > "{win(done_f)}"',
]
batch.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))

# os.execute 等价于 system()：整串交给 cmd /c，无参数重排。用 shell=True 复现。
subprocess.run(f'start "SubFixBG" /b cmd /c "{win(batch)}"', shell=True, cwd=str(work))

deadline = time.time() + 60
while time.time() < deadline and not done_f.exists():
    time.sleep(0.2)
check("端到端: done 文件生成", done_f.exists())
check("端到端: 退出码 0", exit_f.exists() and exit_f.read_text().strip().endswith("0"),
      exit_f.read_text() if exit_f.exists() else "exit 文件缺失")
check("端到端: stdout 捕获", stdout_f.exists() and "e2e-ok" in stdout_f.read_text(encoding="utf-8", errors="replace"),
      stdout_f.read_text(errors="replace")[:120] if stdout_f.exists() else "stdout 缺失")
pid_text = pid_f.read_text().strip() if pid_f.exists() else ""
check("端到端: pid 文件记录了 cmd.exe PID", pid_text.isdigit() and int(pid_text) > 0, repr(pid_text))

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过")
