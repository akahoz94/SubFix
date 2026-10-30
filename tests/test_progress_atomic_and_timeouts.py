"""进度文件原子写 + 本地 Qwen 四阶段播报 + 外部子进程超时收口。

对应上游 v3.3.3 的两条改动，以及本 fork 三处裸 subprocess.run。
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("progress_atomic_tests", ROOT / "subfix_asr_transcribe.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["progress_atomic_tests"] = mod
spec.loader.exec_module(mod)

# 本地 Qwen 四阶段机器码必须与上游 v3.3.3 一致，界面文案按用户要求逐字对齐。
QWEN_PHASES = [
    ("load_qwen_model", "正在加载语音模型"),
    ("run_qwen_asr", "正在识别语音"),
    ("load_qwen_aligner", "正在加载字幕对齐模型"),
    ("qwen_align", "正在对齐字幕时间"),
]


@pytest.fixture(autouse=True)
def _reset_progress_globals():
    """进度环境上下文与阶段计时是模块级状态，用例之间必须互不串台。"""
    mod._PROGRESS_CONTEXT = None
    mod._PROGRESS_STAGE_TIMES.clear()


def test_write_progress_replaces_atomically_and_leaves_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        replaced.append((Path(src).name, Path(dst).name))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(mod.os, "replace", spy)
    for index in range(30):
        mod.write_progress(path, "generate_subtitle_batch", f"识别 {index}/30", padding="x" * 4096)
        # Lua 侧每 200ms 正则读一次：任何一次读取都必须能完整解析、且是最新一条
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["message"] == f"识别 {index}/30"
        assert payload["stage"] == "generate_subtitle_batch"
    assert replaced, "write_progress 没有走 os.replace，仍在原地覆盖写"
    assert all(src.startswith(".") and src.endswith(".tmp") for src, _dst in replaced)
    assert [item.name for item in tmp_path.iterdir()] == ["progress.json"]


def test_write_progress_survives_a_torn_write_under_a_hot_reader(tmp_path):
    path = tmp_path / "progress.json"
    mod.write_progress(path, "done", "种子", padding="x" * 20000)
    stop = threading.Event()
    broken: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, PermissionError):
                # 替换瞬间的"文件正忙"不是半截读，Lua 侧 200ms 轮询本来就容错
                continue
            except ValueError:
                broken.append("半截 JSON")
                return
            if not str(payload.get("message") or ""):
                broken.append("缺字段")
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for index in range(60):
            mod.write_progress(path, "generate_subtitle_batch", f"识别 {index}/60", padding="x" * 20000)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not broken
    # 高频轮询下 os.replace 可能被 Windows 共享模式挡掉一次，读侧退让后必须仍能落盘
    mod.write_progress(path, "generate_subtitle_batch", "识别 60/60", padding="x" * 20000)
    assert json.loads(path.read_text(encoding="utf-8"))["message"] == "识别 60/60"
    assert [item.name for item in tmp_path.iterdir()] == ["progress.json"]


def test_write_progress_keeps_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    mod.write_progress(path, "done", "上一版完整内容")

    def boom(*args, **kwargs):
        raise OSError("模拟写盘中途失败")

    monkeypatch.setattr(mod.json, "dump", boom)
    mod.write_progress(path, "generate_subtitle_batch", "半截消息")
    assert json.loads(path.read_text(encoding="utf-8"))["message"] == "上一版完整内容"
    assert [item.name for item in tmp_path.iterdir()] == ["progress.json"], "失败路径没清掉临时文件"


def _install_fake_qwen_asr(monkeypatch) -> types.ModuleType:
    aligner_module = types.ModuleType("qwen_asr.inference.qwen3_forced_aligner")

    class ForcedAlignResult:
        def __init__(self, items=None):
            self.items = list(items or [])

    aligner_module.ForcedAlignResult = ForcedAlignResult
    utils = types.ModuleType("qwen_asr.inference.utils")
    utils.normalize_audios = lambda paths: [("wave", 16000)]
    utils.split_audio_into_chunks = lambda wav, rate, seconds: [(("wave", rate), 0.0)]
    inference = types.ModuleType("qwen_asr.inference")
    inference.utils = utils
    package = types.ModuleType("qwen_asr")
    package.inference = inference
    for name, module in (
        ("qwen_asr", package),
        ("qwen_asr.inference", inference),
        ("qwen_asr.inference.utils", utils),
        ("qwen_asr.inference.qwen3_forced_aligner", aligner_module),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return aligner_module


def test_qwen_local_phases_announced_in_process(tmp_path, monkeypatch):
    aligner_module = _install_fake_qwen_asr(monkeypatch)

    class Item:
        def __init__(self, text, start_time, end_time):
            self.text, self.start_time, self.end_time = text, start_time, end_time

    class FakeAsrModel:
        pass

    class FakeAligner:
        def align(self, audio=None, text=None, language=None):
            return [aligner_module.ForcedAlignResult(items=[Item("你", 0.0, 0.4), Item("好", 0.4, 0.8)])]

    monkeypatch.setattr(mod, "load_qwen3_asr_model", lambda model: (FakeAsrModel(), "m", "aligner", "cpu"))
    monkeypatch.setattr(mod, "load_qwen3_forced_aligner", lambda: FakeAligner())
    monkeypatch.setattr(
        mod,
        "_transcribe_with_oom_retry",
        lambda *args, **kwargs: ([types.SimpleNamespace(text="你好", language="Chinese")], "not_requested"),
    )

    path = tmp_path / "progress.json"
    recorded: list[dict] = []
    real_write_payload = mod.write_payload

    def record(target, payload):
        recorded.append(dict(payload))
        real_write_payload(target, payload)

    monkeypatch.setattr(mod, "write_payload", record)
    mod.write_progress(
        path, "generate_subtitle_batch", "本地 Qwen 转写 1/1",
        batch_index=1, total_batches=1, progress_index=25, progress_total=100,
    )
    payloads = mod.transcribe_qwen3_asr_batch([tmp_path / "window.wav"], "qwen3_asr", "zh-CN")

    assert [payload["text"] for payload in payloads] == ["你好"], "阶段播报不能改识别文本"
    assert [payload["words"][0]["start"] for payload in payloads[0]["segments"]] == [0.0], "播报不能改时序"
    assert [item["stage"] for item in recorded] == ["generate_subtitle_batch"] + [
        stage for stage, _label in QWEN_PHASES
    ]
    for stage, label in QWEN_PHASES:
        entry = next(item for item in recorded if item["stage"] == stage)
        assert label in entry["message"], f"{stage} 没带上中文阶段名"
        # 停滞时进度条仍停在 25%，但消息里必须同时保留批次号和阶段名
        assert entry["progress_index"] == 25 and entry["progress_total"] == 100
        assert "本地 Qwen 转写 1/1" in entry["message"]
        assert entry["elapsed_seconds"] >= 0
    assert recorded[-1]["stage"] == "qwen_align"
    assert set(mod._PROGRESS_STAGE_TIMES) >= {stage for stage, _label in QWEN_PHASES}


def test_phases_silent_without_any_progress_file(tmp_path):
    """没传 --progress-json 时不能凭空造文件，也不能抛异常。"""
    mod._PROGRESS_CONTEXT = None
    mod.write_qwen_local_phase("run_qwen_asr")
    assert list(tmp_path.iterdir()) == []


def test_runaway_external_backend_is_cut_off(tmp_path, monkeypatch):
    if " " in sys.executable:
        pytest.skip("cmd.exe 下带空格解释器路径需要额外引号，本机不覆盖")
    monkeypatch.setattr(mod, "EXTERNAL_TRANSCRIBE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setenv(
        "SUBFIX_MIMO_ASR_CMD",
        f'{sys.executable} -c "import time; time.sleep(8)" {{audio}} {{output}} {{model}} {{language}}',
    )
    progress_path = tmp_path / "progress.json"
    with pytest.raises(RuntimeError) as excinfo:
        mod.transcribe_external_backend(tmp_path / "window.wav", "model", "zh-CN", "mimo_asr")
    message = str(excinfo.value)
    assert "mimo_asr" in message and "仍未返回" in message and "缩短选区" in message
    # 走既有错误通道：main 的 except 分支把它写成 failed 阶段，Lua 才有中文可显示
    mod.write_progress(progress_path, "failed", message)
    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    assert payload["stage"] == "failed" and payload["message"] == message


def test_every_subprocess_run_is_bounded():
    text = (ROOT / "subfix_asr_transcribe.py").read_text(encoding="utf-8")
    calls = re.findall(r"subprocess\.run\((?:[^()]|\([^()]*\))*\)", text, flags=re.S)
    assert len(calls) == 3, f"subprocess.run 站点数量变了：{len(calls)}"
    for call in calls:
        assert "timeout=" in call, call


def test_emoji_on_a_gbk_console_does_not_kill_the_run(tmp_path):
    """GGUF 引擎进度里的 📊 撞 GBK 控制台会抛 UnicodeEncodeError，auto 把它当成
    "GGUF 失败"，静默退回慢约 30 倍的 torch 批 1。用真 GBK stdout 起子进程复现。
    """
    script = tmp_path / "print_emoji.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "sys.stdout.reconfigure(encoding='gbk')\n"
        "import subfix_asr_transcribe\n"
        "print('\\U0001F4CA 批大小')\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, encoding="gbk", errors="replace")
    assert result.returncode == 0, result.stderr[-400:]
    assert "批大小" in result.stdout
