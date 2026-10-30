"""后台轮询循环的判活与终止条件（v3.3.4 上游那次修复补齐到 SubFix.lua 的两条循环）。

SubFix.lua 里有两条自己写的轮询循环：规整字幕长度的后台跑批（run_subfix_background_command）
和 AI 请求的 curl 跑批（execute_ai_request 里的 B 方案）。上游只修了核心模块的 ASR helper，
这两条还是「只认 done 标记」——helper 崩了没写 done，进度窗就干等到用户手动取消。
钉住四件事：退出码落盘算终止、stage=="failed" 算终止（留 2 秒冲输出）、
每 2 秒查一次进程存活且连续两次「进程没了」才收、失败时保留 stdout 并附路径。
判活本身另附真进程行为用例，因为 `SUBFIX_WIN_FFI and subfix_process_alive(pid) or nil`
这种写法已经把 Windows 判活整条空转过一次。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "SubFix.lua").read_text(encoding="utf-8")
CORE = (ROOT / ".subfix_support" / "subfix_generate_selection_core.lua").read_text(encoding="utf-8")


def segment(source, start_marker, end_marker):
    start = source.index(start_marker)
    return source[start:source.index(end_marker, start)]


BG_LOOP = segment(SOURCE, "function run_subfix_background_command(cmd, options)",
                  "local function parse_fps(fps_str)")
CURL_LOOP = segment(SOURCE, "-- ====== B 方案：后台 curl",
                    'print("[Hooper AI 2.0] curl 输出: "')
ASR_LOOP = segment(CORE, "local function asr_process_is_alive(pid)", "    return succeeded, output, nil")

LOOPS = {"规整字幕长度": BG_LOOP, "AI curl": CURL_LOOP}
# 单跳判定的那一段：轮询每 50/150 毫秒跑一次，这里多开一次进程就是用户看得见的卡
BG_STEP = segment(BG_LOOP, "local function background_poll_step()", "register_ui_timer(poll_timer")
CURL_STEP = segment(CURL_LOOP, "local function curl_poll_step()", "-- Fusion 的 timer 事件通过")
STEPS = {"规整字幕长度": BG_STEP, "AI curl": CURL_STEP}


def test_exit_code_file_alone_is_terminal():
    for name, loop in LOOPS.items():
        assert 'finished = tonumber(trim_text(read_text_file(exit_file) or "")) ~= nil' in loop, name
        assert "if finished then return true end" in loop, name
        # 退出码是每秒查一次，不是每 50/150 毫秒的跳都开一次磁盘
        assert "now - last_exit_check_at >= 1" in loop, name
        # 旧的「只认 done 标记」写法不许回来
        assert "io.open(done_file" not in loop, name


def test_failed_progress_stage_is_terminal_after_a_grace_period():
    assert 'if payload and payload.stage == "failed" then' in BG_LOOP
    assert "if now - failed_since_at >= 2 then" in BG_LOOP, "要留 2 秒让 helper 把输出冲干净"
    assert 'terminal_error = tostring(payload.message or "后台任务执行失败")' in BG_LOOP
    assert "kill_normalize_background_process(pid_file)" in BG_LOOP


def test_process_liveness_uses_ffi_and_needs_two_consecutive_misses():
    for name, loop in LOOPS.items():
        assert "subfix_process_alive(" in loop, name
        assert "if now - last_process_check_at >= 2 then" in loop, name
        assert "if missing_process_checks >= 2 then" in loop, name
        assert "missing_process_checks = 0" in loop, name
        step = STEPS[name]
        # Windows 没有 kill -0，也不许每跳起一个 cmd 去查进程
        assert "kill -0" not in step, name
        assert "io.popen" not in step, name
        assert "os.execute" not in step, name
    assert 'terminal_error = "后台任务进程已退出，但未返回完成状态；请查看诊断日志"' in BG_LOOP
    assert 'terminal_error = "AI 请求进程已退出，但未返回完成状态；请查看诊断日志"' in CURL_LOOP


def test_liveness_result_is_not_read_through_and_or():
    """`SUBFIX_WIN_FFI and subfix_process_alive(pid) or nil` 会把 false 吞成 nil。"""
    assert not re.search(r"SUBFIX_WIN_FFI\s+and\s+\w*\s*subfix_process_alive[^)]*\bor\b", SOURCE)
    assert "if alive == true then" in BG_LOOP and "elseif alive == false then" in BG_LOOP
    assert "if alive == true then" in CURL_LOOP and "elseif alive == false then" in CURL_LOOP


def test_missing_pid_file_is_never_treated_as_a_dead_process():
    for name, loop in LOOPS.items():
        assert "判定会误杀" in loop, name
    assert "pid 文件由批处理写入" in SOURCE


def test_no_ff_falls_back_to_file_markers_only():
    assert "if not SUBFIX_WIN_FFI then return nil end" in BG_LOOP
    assert "SUBFIX_IS_WINDOWS and SUBFIX_WIN_FFI" in CURL_LOOP


def test_both_poll_paths_share_one_terminal_check():
    """嵌套 RunLoop 和同步兜底必须同一份判定，否则兜底那条还是能干等。"""
    assert "if background_poll_step() then break end" in BG_LOOP
    assert "if curl_poll_step() then break end" in CURL_LOOP
    assert BG_LOOP.count("background_poll_step()") == 3
    assert CURL_LOOP.count("curl_poll_step()") == 3
    # 兜底循环里的忙等（os.execute("sleep 0.05") 在 cmd 下不成立）不许留着
    assert 'os.execute("sleep' not in CURL_LOOP
    assert "subfix_sleep(0.05)" in CURL_LOOP


def first_cleanup_call(text):
    tail = text[text.index("subfix_remove_files("):]
    return tail.splitlines()[0]


def test_stdout_log_is_kept_on_failure_and_its_path_is_shown():
    assert 'output = output .. "\\n诊断日志: " .. stdout_file' in BG_LOOP
    assert '"；诊断日志: " .. stdout_file' in CURL_LOOP
    assert "local succeeded = exit_code == 0 and terminal_error == nil" in BG_LOOP
    assert "return exit_code == 0, output, nil" not in BG_LOOP
    # 失败分支的清理调用里不许再出现 stdout_file
    bg_fail = BG_LOOP[BG_LOOP.index("if not succeeded then"):]
    bg_fail = bg_fail[:bg_fail.index("return false, output, nil")]
    assert first_cleanup_call(bg_fail).strip() == \
        "subfix_remove_files(pid_file, done_file, exit_file, progress_file, batch_file)"
    curl_fail = CURL_LOOP[CURL_LOOP.index("if terminal_error then"):]
    curl_fail = curl_fail[:curl_fail.index("return nil, nil,")]
    # .cmd 里是带密钥的 curl 命令，失败也要删；只有 stdout 留档。
    assert first_cleanup_call(curl_fail).strip() == "subfix_remove_files(pid_file, done_file, exit_file, batch_file)"


def test_curl_batch_now_records_exit_code():
    assert 'local exit_file   = temp_root .. "/hooper_curl_exit_"   .. req_uid' in CURL_LOOP
    assert "exit_file = exit_file," in CURL_LOOP
    assert "subfix_remove_files(stdout_file, pid_file, done_file, exit_file, batch_file)" in CURL_LOOP


def test_subfix_ffi_declares_openprocess_and_alive():
    assert "void *OpenProcess(unsigned long dwDesiredAccess, int bInheritHandle, unsigned long dwProcessId);" in SOURCE
    assert SOURCE.count("function subfix_process_alive(pid)") == 1
    assert "return code[0] == 259" in SOURCE


def test_core_asr_loop_still_has_all_three_conditions():
    assert 'finished = tonumber(trim_text(read_text_file(exit_file) or "")) ~= nil' in ASR_LOOP
    assert 'if payload and payload.stage == "failed" then' in ASR_LOOP
    assert "if missing_process_checks >= 2 then" in ASR_LOOP


@pytest.fixture(scope="module")
def subfix_alive():
    if os.name != "nt":
        pytest.skip("OpenProcess 判活只在 Windows 上有意义")
    lua = LuaRuntime()
    if not lua.execute("return pcall(require, 'ffi')"):
        pytest.skip("这个 LuaJIT 没有 ffi")
    # 兼容层整段拷到 ASCII 临时路径执行：LuaJIT 的文件 API 走 Windows ANSI，中文目录打不开
    start = SOURCE.index("-- ========== Windows 兼容层 ==========")
    block = SOURCE[start:SOURCE.index("SUBFIX_VERSION = ")]
    target = Path(tempfile.mkdtemp(prefix="subfix_alive_")) / "compat.lua"
    target.write_text(block, encoding="utf-8")
    lua.execute("dofile([[%s]])" % target.as_posix())
    if not lua.execute("return SUBFIX_WIN_FFI == true"):
        pytest.skip("SubFix.lua 的兼容层没启用 Windows FFI")
    return lambda pid: lua.execute("return subfix_process_alive(%d)" % int(pid))


def test_running_process_reads_alive(subfix_alive):
    assert subfix_alive(os.getpid()) is True


def test_exited_child_reads_dead(subfix_alive):
    child = subprocess.Popen([sys.executable, "-B", "-c", "pass"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    assert child.wait() == 0
    assert subfix_alive(child.pid) is False


def test_missing_pid_is_nil_not_dead(subfix_alive):
    """pid 读不到时返回 nil（未知），不是 false——否则会被当成「进程没了」误杀。"""
    assert subfix_alive(0) is None
