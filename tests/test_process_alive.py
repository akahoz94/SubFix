"""判活必须真能分辨活进程与死进程。
上一版写成 `SUBFIX_WIN_FFI and subfix_process_alive(pid) or nil`：进程死了时 true and false → false，
再 or nil → nil，于是 alive == false 永远不触发，Windows 判活整条空转。"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from lupa.luajit21 import LuaRuntime

SRC = Path(__file__).resolve().parents[1] / ".subfix_support" / "subfix_generate_selection_core.lua"


@pytest.fixture(scope="module")
def alive_probe():
    if os.name != "nt":
        pytest.skip("OpenProcess 判活只在 Windows 上有意义")
    lua = LuaRuntime()
    if not lua.execute("return pcall(require, 'ffi')"):
        pytest.skip("这个 LuaJIT 没有 ffi")
    target = Path(tempfile.mkdtemp(prefix="subfix_alive_")) / "core.lua"
    shutil.copyfile(SRC, target)  # LuaJIT 的 io 走 Windows ANSI，中文目录打不开
    lua.execute("return dofile([[%s]])" % target.as_posix())
    if not lua.execute("return SUBFIX_WIN_FFI == true"):
        pytest.skip("core.lua 没启用 Windows FFI")
    return lambda pid: lua.execute("return subfix_process_alive(%d)" % int(pid))


def test_running_process_reads_alive(alive_probe):
    assert alive_probe(os.getpid()) is True


def test_exited_child_reads_dead(alive_probe):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    child = subprocess.Popen([sys.executable, "-B", "-c", "pass"], creationflags=flags)
    assert child.wait() == 0
    assert alive_probe(child.pid) is False


def test_garbage_pid_is_not_alive(alive_probe):
    assert alive_probe(0x7FFFFF00) is False
