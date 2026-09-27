"""Python 入口真机 QA：全部在 Windows 上真实执行。

覆盖：ASR helper / v4 / v5 / textnorm 的 argparse 与导入、qwen 管理器 status
（Windows 路径布局）与 msvcrt 安装锁互斥、process_group 退出码透传、更新器的
Windows 屏蔽、ffmpeg 解析第一行选择。

运行：python tests/qa_python_entries.py
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "dist" / "SubFix-v3.3.0-Windows.zip"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, cwd=str(ROOT), **kw)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    return r.returncode, out


# ---------- 0. 安装布局准备（从 zip 解压） ----------
stage = Path(tempfile.mkdtemp(prefix="subfix_qa_py_"))
with zipfile.ZipFile(ZIP) as zf:
    zf.extractall(stage)
support = stage / ".subfix_support"

# ---------- 1. CLI 入口可运行（导入链完整，重依赖需懒加载；用安装布局） ----------
# v4/v5/textnorm 是纯库（上游无 CLI 入口），直接执行=完整导入后静默退出，rc==0 即通过
for script in ("subfix_asr_transcribe.py", "subfix_generate_v4.py",
               "subfix_generate_v5.py", "subfix_generate_textnorm.py"):
    rc, out = run([sys.executable, str(support / script), "--help"])
    check(f"导入链完整: {script}", rc == 0 and "Traceback" not in out, out[:200])

# ---------- 2. qwen 管理器：status（Windows 布局） ----------
rc, out = run([sys.executable, "-B", str(support / "subfix_qwen_local_manager.py"),
               "--action", "status", "--root", str(stage), "--output", str(stage / "status.json")])
check("qwen 管理器: status 正常退出", rc == 0, out[:300])
status_file = stage / "status.json"
if status_file.exists():
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    check("qwen 管理器: 空环境返回 state=missing", payload.get("state") == "missing", str(payload))
else:
    check("qwen 管理器: status.json 生成", False, out[:300])

# status 不应触碰系统 %APPDATA%\SubFix（数据目录只在 install 使用）
# 这里直接验证模块内路径计算
sys.path.insert(0, str(support))
import subfix_qwen_local_manager as mgr  # noqa: E402

paths_obj = mgr.SubFixQwenPaths(stage.resolve(), Path.home() / "AppData" / "Roaming" / "SubFix")
check("qwen 管理器: env_python 为 Scripts/python.exe", str(paths_obj.env_python).endswith("Scripts\\python.exe"), str(paths_obj.env_python))
check("qwen 管理器: base_python 回退当前解释器", Path(paths_obj.base_python) == Path(sys.executable), str(paths_obj.base_python))

# msvcrt 锁互斥：线程持锁时再次加锁必须报"正在安装"
lock_file = stage / "locks" / "qa.lock"
acquired = {}
with mgr.exclusive_install_lock(lock_file):
    def try_second():
        try:
            with mgr.exclusive_install_lock(lock_file):
                acquired["ok"] = True
        except RuntimeError as exc:
            acquired["err"] = str(exc)
    t = threading.Thread(target=try_second)
    t.start()
    t.join()
check("qwen 管理器: msvcrt 锁互斥生效", acquired.get("ok") is None and "正在安装" in acquired.get("err", ""), str(acquired))
with mgr.exclusive_install_lock(lock_file):
    check("qwen 管理器: 锁释放后可重入", True)

# ---------- 3. process_group（Windows 分支） ----------
rc, out = run([sys.executable, str(support / "subfix_process_group.py"), 'echo hello & exit /b 5'])
check("process_group: 执行并透传退出码 5", rc == 5 and "hello" in out, f"rc={rc} out={out[:120]}")

# ---------- 4. 更新器 Windows 屏蔽 ----------
rc, out = run([sys.executable, str(support / "subfix_update.py"), "check", "--current-version", "3.3.0"])
check("更新器: Windows 上拒绝并给出指引", rc == 1 and "GitHub Releases" in out, f"rc={rc} {out[:150]}")

# ---------- 5. ffmpeg 解析（Python 侧，与 Lua 修复对齐） ----------
sys.path.insert(0, str(support / ".."))  # helper 在 .subfix_support 内
sys.path.insert(0, str(support))
import importlib.util
spec = importlib.util.spec_from_file_location("subfix_asr", support / "subfix_asr_transcribe.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
try:
    ff = mod.resolve_ffmpeg(None)
    check("resolve_ffmpeg: 返回存在的单一路径", Path(ff).is_file() and "\n" not in str(ff), str(ff))
except RuntimeError as exc:
    check("resolve_ffmpeg: 无 ffmpeg 时报错信息清晰", "未找到 ffmpeg" in str(exc), str(exc)[:200])

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("Python 入口 QA 全部通过")
