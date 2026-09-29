#!/usr/bin/env python3
"""Manage the optional local Qwen ASR extension shipped beside SubFix."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable

import msvcrt
CREATE_NO_WINDOW = 0x08000000  # 隐藏子进程控制台窗口，杜绝黑框


QWEN_ASR_MODEL_ID = "Qwen/Qwen3-ASR-1.7B"
QWEN_PYPI_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
# 清华镜像可能对高频请求返回 403（pip 会把它当"无可用版本"），按序回退
PYPI_INDEX_FALLBACKS = (
    QWEN_PYPI_INDEX_URL,
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://pypi.org/simple",
)
MODEL_DOWNLOAD_SOURCES = (("modelscope", "魔搭国内源"), ("huggingface", "Hugging Face 备用源"))
# PyTorch 官方 CUDA 索引：PyPI 与国内镜像的 torch 是 CPU 构建（+cpu），
# RTX 等 NVIDIA 显卡上推理慢 10~30 倍；有 GPU 时 torch 必须从这里装。
TORCH_CUDA_INDEX_URL = "https://download.pytorch.org/whl/cu129"
QWEN_ASR_REQUIRED_MODEL_FILES = (
    "config.json",
    "model.safetensors.index.json",
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
)
MODEL_DOWNLOAD_ETA_MIN_ELAPSED_SECONDS = 10
MODEL_DOWNLOAD_ETA_MIN_DOWNLOADED_BYTES = 8 * 1024 * 1024
MODEL_DOWNLOAD_MIN_VISIBLE_FRACTION = 0.005
COMMAND_HEARTBEAT_INTERVAL_SECONDS = 1
INSTALL_IN_PROGRESS_MESSAGE = "本地 Qwen 正在安装或下载模型，请勿重复启动"
ProgressReporter = Callable[..., None]


@dataclass(frozen=True)
class SubFixQwenPaths:
    root: Path
    data_root: Path

    @property
    def base_python(self) -> Path:
        if os.name == "nt":
            bundled = self.root / "runtime" / "python" / "python.exe"
            # Windows 轻量包不内置运行时，回退到当前解释器（安装器本身在可用的 Python 里运行）
            return bundled if bundled.is_file() else Path(sys.executable)
        return self.root / "runtime" / "python" / "bin" / "python3"

    @property
    def env_dir(self) -> Path:
        # Resolve scans its script tree on the UI thread when pip creates files.
        return self.data_root / "envs" / "qwen-local"

    @property
    def legacy_env_dir(self) -> Path:
        return self.root / "envs" / "qwen-local"

    @property
    def env_python(self) -> Path:
        if os.name == "nt":
            return self.env_dir / "Scripts" / "python.exe"
        return self.env_dir / "bin" / "python"

    @property
    def model_dir(self) -> Path:
        return self.data_root / "models" / "qwen3-asr-1.7b"

    @property
    def legacy_model_dir(self) -> Path:
        return self.root / "models" / "qwen3-asr-1.7b"

    @property
    def ready_marker(self) -> Path:
        return self.data_root / ".subfix-qwen-local-ready.json"

    @property
    def legacy_plugin_ready_marker(self) -> Path:
        return self.root / ".subfix-qwen-local-ready.json"

    @property
    def legacy_ready_marker(self) -> Path:
        return self.legacy_model_dir / ".subfix-ready.json"


    @property
    def install_log(self) -> Path:
        return self.data_root / "logs" / "qwen-local-install.log"

    @property
    def install_lock(self) -> Path:
        return self.data_root / "locks" / "qwen-local-install.lock"


@contextmanager
def exclusive_install_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError(INSTALL_IN_PROGRESS_MESSAGE) from exc
        acquired = True
        yield
    finally:
        if acquired:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        lock_file.close()


def python_can_import_qwen_asr(python: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python), "-c", "import qwen_asr, torch"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # A fresh venv can spend over a minute loading native libraries.
            timeout=180,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def model_directory_is_complete(model_dir: Path) -> bool:
    if not model_dir.is_dir():
        return False
    if not model_dir.joinpath("config.json").is_file():
        return False
    # 官方分片形态：index + 两个分片（download_model 落盘的标准文件集）
    if all(model_dir.joinpath(name).is_file() for name in QWEN_ASR_REQUIRED_MODEL_FILES[1:]):
        return True
    # 单文件 safetensors 形态：与 Lua 侧 local_model_dir_complete 的判定标准一致，
    # 避免 UI 探测通过（提示"检测到本机模型"）而 install 仍判定不完整并重复下载。
    # index.json 并存时 transformers 按索引加载分片，单文件不会被使用——该畸形目录必须拒绝。
    if model_dir.joinpath("model.safetensors.index.json").is_file():
        return False
    return model_dir.joinpath("model.safetensors").is_file()


def huggingface_cache_roots() -> list[Path]:
    roots: list[Path] = []
    explicit_cache = os.getenv("HF_HUB_CACHE") or os.getenv("HUGGINGFACE_HUB_CACHE")
    if explicit_cache:
        roots.append(Path(explicit_cache).expanduser())
    hf_home = os.getenv("HF_HOME")
    if hf_home:
        roots.append(Path(hf_home).expanduser() / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return list(dict.fromkeys(roots))


def force_download_requested(paths: "SubFixQwenPaths") -> bool:
    """用户在 UI 明确选择"重新下载模型"后由 Lua 写入的标记；存在时忽略一切本机模型候选。"""
    return (paths.data_root / ".subfix-force-download").is_file()


def env_model_dir() -> Path | None:
    """SUBFIX_QWEN3_ASR_MODEL 指向的本机模型目录（接入本地模型.bat 或 setx 写入）。

    本机模型优先级最高：用户明确登记的目录，命中即免下载。"""
    value = str(os.getenv("SUBFIX_QWEN3_ASR_MODEL") or "").strip()
    if not value:
        return None
    candidate = Path(value).expanduser()
    if model_directory_is_complete(candidate):
        return candidate
    return None


def existing_model_dir(paths: SubFixQwenPaths) -> Path | None:
    if force_download_requested(paths):
        # 重新下载模式：env/junction/HF 缓存一律忽略，只认数据目录里的完整模型
        return paths.model_dir if model_directory_is_complete(paths.model_dir) else None
    env_dir = env_model_dir()
    if env_dir is not None:
        return env_dir
    if model_directory_is_complete(paths.model_dir):
        return paths.model_dir
    if model_directory_is_complete(paths.legacy_model_dir):
        return paths.legacy_model_dir
    for cache_root in huggingface_cache_roots():
        snapshots_dir = cache_root / "models--Qwen--Qwen3-ASR-1.7B" / "snapshots"
        if not snapshots_dir.is_dir():
            continue
        candidates = sorted(
            (path for path in snapshots_dir.iterdir() if model_directory_is_complete(path)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def has_model_artifacts(paths: SubFixQwenPaths) -> bool:
    if force_download_requested(paths):
        return model_directory_is_complete(paths.model_dir)
    if env_model_dir() is not None:
        return True
    if paths.model_dir.is_dir() or paths.legacy_model_dir.is_dir():
        return True
    return any((root / "models--Qwen--Qwen3-ASR-1.7B" / "snapshots").is_dir() for root in huggingface_cache_roots())


def ready_environment_python(paths: SubFixQwenPaths) -> Path | None:
    if paths.env_python.is_file() and paths.ready_marker.is_file():
        return paths.env_python
    legacy_python = paths.legacy_env_dir / (Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python")
    # A legacy marker must never certify a newly created, incomplete environment.
    if legacy_python.is_file() and (paths.legacy_plugin_ready_marker.is_file() or paths.legacy_ready_marker.is_file()):
        return legacy_python
    return None


def inspect_install(paths: SubFixQwenPaths) -> dict[str, object]:
    model_dir = existing_model_dir(paths)
    legacy_python_rel = Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python"
    environment_exists = paths.env_python.is_file() or (paths.legacy_env_dir / legacy_python_rel).is_file()
    if not environment_exists or not has_model_artifacts(paths):
        return {"state": "missing", "ready": False}
    # The marker is written only after the installer verifies the dependency;
    # do not re-import qwen_asr/torch on Resolve's synchronous status path.
    python = ready_environment_python(paths)
    if model_dir is None or python is None:
        return {"state": "repair_required", "ready": False}
    return {
        "state": "installed",
        "ready": True,
        "python": str(python),
        "model": str(model_dir),
    }


def ensure_base_python(python: Path) -> None:
    if not python.is_file():
        raise RuntimeError(f"未找到 SubFix 内置 Python：{python}")


def ensure_model_dir_writable(model_dir: Path) -> None:
    if model_dir.is_symlink() or model_dir.parent.is_symlink():
        raise RuntimeError(f"本地 Qwen 模型目录不能是软链接：{model_dir}")
    target_dir = model_dir if model_dir.exists() else model_dir.parent
    probe_path = target_dir / f".subfix-write-test-{os.getpid()}-{time.time_ns()}"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        probe_path.write_bytes(b"")
        probe_path.unlink()
    except OSError as exc:
        raise RuntimeError(f"本地 Qwen 模型目录不可写：{target_dir}（{exc}）") from exc


def append_command_log(log_path: Path | None, command: list[str], output: str) -> None:
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n[{timestamp}] $ {' '.join(command)}\n")
            log_file.write(output)
            if output and not output.endswith("\n"):
                log_file.write("\n")
    except OSError:
        # Logging must never hide the original install error.
        pass


def command_error_message(error_prefix: str, output: str, log_path: Path | None) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if re.search(r"^ERROR:|[\w.]+(?:Error|Exception):", line)]
    meaningful = [line for line in lines if not line.startswith(("[notice]", "Traceback", "File ", "^"))]
    cause = (errors or meaningful or ["命令未返回可用错误信息"])[-1]
    lower = cause.lower()
    if "no space left" in lower or "disk full" in lower:
        detail = "磁盘空间不足，请释放空间后点击“重试”。"
    elif "permission denied" in lower or "operation not permitted" in lower:
        detail = "安装目录没有写入权限，请检查目录权限后重试。"
    elif "certificate_verify_failed" in lower or "certificate verify failed" in lower:
        detail = "下载连接的证书校验失败，请检查系统时间和网络代理后重试。"
    elif "timed out" in lower or "timeout" in lower:
        detail = "依赖下载超时，请确认网络连接稳定后点击“重试”；已完成的下载缓存会复用。"
    elif any(marker in lower for marker in ("connection broken", "connection reset", "incompleteread", "protocolerror")):
        detail = "依赖下载连接中断，请确认网络连接稳定后点击“重试”；已完成的下载缓存会复用。"
    else:
        detail = cause[:240] + "\n请查看完整日志确认原因后重试。"
    message = f"{error_prefix}：{detail}"
    if log_path is not None:
        message += f"\n完整日志：{log_path}"
    return message


def start_command_heartbeat(
    report: ProgressReporter | None,
    heartbeat: tuple[str, str] | None,
) -> tuple[threading.Event | None, threading.Thread | None]:
    if report is None or heartbeat is None:
        return None, None
    stop_event = threading.Event()
    stage, message = heartbeat
    started_at = time.monotonic()

    def report_heartbeat() -> None:
        while not stop_event.is_set():
            elapsed_seconds = max(0, int(time.monotonic() - started_at))
            try:
                report(stage, f"{message}（已用时 {elapsed_seconds}s）")
            except Exception:
                return
            stop_event.wait(COMMAND_HEARTBEAT_INTERVAL_SECONDS)

    monitor = threading.Thread(target=report_heartbeat, daemon=True)
    monitor.start()
    return stop_event, monitor


def run_checked(
    command: list[str],
    *,
    error_prefix: str,
    log_path: Path | None = None,
    report: ProgressReporter | None = None,
    heartbeat: tuple[str, str] | None = None,
) -> None:
    stop_event, monitor = start_command_heartbeat(report, heartbeat)
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        output = str(exc)
        append_command_log(log_path, command, output)
        raise RuntimeError(command_error_message(error_prefix, output, log_path)) from exc
    finally:
        if stop_event is not None:
            stop_event.set()
        if monitor is not None:
            monitor.join(timeout=2)
    output = result.stdout or ""
    append_command_log(log_path, command, output)
    if result.returncode != 0:
        raise RuntimeError(command_error_message(error_prefix, output, log_path))


def create_or_reuse_venv(base_python: Path, env_dir: Path) -> None:
    env_python_rel = Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python"
    if not (env_dir / env_python_rel).is_file():
        run_checked([str(base_python), "-m", "venv", str(env_dir)], error_prefix="创建本地 Qwen 环境失败")


def pip_install_with_index_fallback(env_python: Path, packages: list[str], error_prefix: str,
                                    report: ProgressReporter | None, log_path: Path | None,
                                    heartbeat: tuple[str, str] | None = None,
                                    extra_args: list[str] | None = None) -> None:
    """pip 安装，镜像按 清华→阿里云→官方 逐个回退。

    清华 403 时 pip 报 "No matching distribution found"——按序换源重试，
    全部失败才抛错（附上各镜像的错误摘要）。"""
    errors: list[str] = []
    for index_url in PYPI_INDEX_FALLBACKS:
        command = [str(env_python), "-m", "pip", "install", "--upgrade",
                   "--index-url", index_url,
                   "--timeout", "60", "--retries", "5", "--disable-pip-version-check",
                   "--no-input", "--progress-bar", "off", *(extra_args or []), *packages]
        try:
            run_checked(command, error_prefix=error_prefix, log_path=log_path,
                        report=report, heartbeat=heartbeat)
            return
        except RuntimeError as exc:
            message = str(exc)
            errors.append(f"{index_url}: {message.splitlines()[0] if message else '失败'}")
    raise RuntimeError(
        f"{error_prefix}：所有镜像源均失败（可能是网络受限或镜像限流）。已尝试："
        + "；".join(errors))


def _has_nvidia_gpu() -> bool:
    """检测是否存在可用的 NVIDIA GPU（nvidia-smi 可执行且能列出设备）。"""
    if os.name != "nt":
        return False
    candidates = [shutil.which("nvidia-smi"), r"C:\Windows\System32\nvidia-smi.exe"]
    for nv in candidates:
        if nv and os.path.isfile(nv):
            try:
                probe = subprocess.run([nv, "-L"], capture_output=True, timeout=20, check=False,
                       creationflags=CREATE_NO_WINDOW)
                return probe.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False
    return False


def install_torch_with_cuda(env_python: Path, report: ProgressReporter, log_path: Path) -> None:
    """从 PyTorch 官方 CUDA 索引安装支持 GPU 的 torch。

    PyPI 与国内镜像的 torch 是 CPU 构建（+cpu），在 NVIDIA 显卡上推理慢 10~30 倍；
    有 GPU 时优先走官方 cu 索引。失败回退镜像 CPU 版（功能可用，只是慢）。"""
    # 注意：不能带 --extra-index-url（会引入 PyPI 的 +cpu 版干扰 pip 选版本），
    # 也不能 --no-deps（torch 的 nvidia-* CUDA 运行库也要从 cu129 索引装）。
    # 其余公共依赖（sympy 等）在调用本函数前已由镜像源装好，这里 pip 会判定满足。
    command = [str(env_python), "-m", "pip", "install",
               "--index-url", TORCH_CUDA_INDEX_URL,
               "--timeout", "60", "--retries", "5", "--disable-pip-version-check",
               "--no-input", "--progress-bar", "off",
               "--no-cache-dir", "--force-reinstall", "torch"]
    try:
        run_checked(command, error_prefix="安装 GPU 版 PyTorch 失败",
                    log_path=log_path, report=report,
                    heartbeat=("安装依赖", "正在安装 GPU 版 PyTorch（CUDA），识别将大幅加速"))
        return
    except RuntimeError:
        pass
    # 兜底：torch 依赖已装（qwen-asr 要求），直接镜像装 CPU 版保持功能可用
    pip_install_with_index_fallback(
        env_python, ["torch"],
        error_prefix="安装 PyTorch 失败（GPU 版不可用，已回退 CPU 版）",
        report=report, log_path=log_path,
        heartbeat=("安装依赖", "正在安装 PyTorch（CPU 版兜底，未检测到可用 GPU 源）"),
        extra_args=["--resume-retries", "5"],
    )


def install_qwen_dependencies(env_python: Path, report: ProgressReporter, log_path: Path) -> None:
    # venv seeds pip 25.0.1; upgrading in the dependency command leaves that
    # same old process handling large downloads without resume support.
    pip_install_with_index_fallback(
        env_python, ["pip>=25.2"],
        error_prefix="升级本地 Qwen 下载工具失败",
        report=report,
        log_path=log_path,
        heartbeat=("准备下载工具", "正在升级 pip，启用下载中断恢复"),
    )
    has_gpu = _has_nvidia_gpu()
    if has_gpu:
        # 先装公共依赖（qwen-asr 会带上 torch 所需的 sympy/jinja2 等），
        # 再单独从 cu129 索引装 GPU 版 torch——避免 --extra-index-url 引入 PyPI +cpu 版干扰。
        pip_install_with_index_fallback(
            env_python, ["qwen-asr", "huggingface_hub", "modelscope", "onnxruntime", "pypinyin"],
            error_prefix="安装本地 Qwen 依赖失败",
            report=report,
            log_path=log_path,
            heartbeat=("安装依赖", "正在安装识别引擎依赖（qwen-asr 等，非模型文件），下载中断会自动重试，请保持网络连接"),
            extra_args=["--resume-retries", "5"],
        )
        install_torch_with_cuda(env_python, report, log_path)
        return
    pip_install_with_index_fallback(
        env_python, ["qwen-asr", "torch", "huggingface_hub", "modelscope", "onnxruntime", "pypinyin"],
        error_prefix="安装本地 Qwen 依赖失败",
        report=report,
        log_path=log_path,
        heartbeat=("安装依赖", "正在安装识别引擎依赖（qwen-asr 与 PyTorch，非模型文件），下载中断会自动重试，请保持网络连接"),
        extra_args=["--resume-retries", "5"],
    )


def directory_size_bytes(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            # The downloader may replace a temporary file between iteration and stat.
            continue
    return total


def ensure_modelscope_downloader(env_python: Path, report: ProgressReporter, log_path: Path) -> None:
    try:
        probe = subprocess.run(
            [str(env_python), "-c", "from modelscope.hub.snapshot_download import snapshot_download"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        if probe.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Older installations can already run Qwen but lack the domestic downloader.
    pip_install_with_index_fallback(
        env_python, ["modelscope"],
        error_prefix="安装魔搭下载工具失败", log_path=log_path, report=report,
        heartbeat=("准备下载工具", "正在通过国内镜像安装魔搭下载工具"),
    )


def fetch_model_total_bytes(env_python: Path, source: str = "modelscope") -> int | None:
    if source == "modelscope":
        script = (
            "from modelscope.hub.api import HubApi\n"
            "import sys\n"
            "files = HubApi().get_model_files(sys.argv[1], recursive=True)\n"
            "print(sum(int(item.get('Size', 0) or 0) for item in files if item.get('Type') != 'tree'))\n"
        )
    elif source == "huggingface":
        script = (
            "from huggingface_hub import HfApi\n"
            "import sys\n"
            "info = HfApi().model_info(sys.argv[1], files_metadata=True)\n"
            "print(sum(int(getattr(item, 'size', 0) or 0) for item in info.siblings))\n"
        )
    else:
        raise ValueError(f"未知模型下载源：{source}")
    try:
        result = subprocess.run(
            [str(env_python), "-c", script, QWEN_ASR_MODEL_ID],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        total = int(result.stdout.strip().rsplit("\n", 1)[-1])
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return None
    return total if total > 0 else None


def model_download_eta_seconds(
    *,
    total_bytes: int,
    initial_bytes: int,
    current_bytes: int,
    started_at: float,
    current_at: float,
) -> int | None:
    elapsed = max(0.0, current_at - started_at)
    downloaded = max(0, current_bytes - initial_bytes)
    if (
        elapsed < MODEL_DOWNLOAD_ETA_MIN_ELAPSED_SECONDS
        or downloaded < MODEL_DOWNLOAD_ETA_MIN_DOWNLOADED_BYTES
        or current_bytes >= total_bytes
    ):
        return None
    average_speed = downloaded / elapsed
    if average_speed <= 0:
        return None
    return max(1, round((total_bytes - current_bytes) / average_speed))


def report_model_download_progress(
    model_dir: Path,
    total_bytes: int | None,
    report: ProgressReporter,
    stop_event: threading.Event,
) -> None:
    initial_bytes = directory_size_bytes(model_dir)
    started_at = time.monotonic()
    while not stop_event.is_set():
        current_at = time.monotonic()
        current_bytes = directory_size_bytes(model_dir)
        details: dict[str, object] = {}
        # Resolve rounds the displayed percentage, so wait until this can render
        # as at least 1% instead of switching from the marquee to a visible 0%.
        if total_bytes is not None and current_bytes / total_bytes >= MODEL_DOWNLOAD_MIN_VISIBLE_FRACTION:
            details["progress_index"] = min(current_bytes, total_bytes)
            details["progress_total"] = total_bytes
            eta_seconds = model_download_eta_seconds(
                total_bytes=total_bytes,
                initial_bytes=initial_bytes,
                current_bytes=current_bytes,
                started_at=started_at,
                current_at=current_at,
            )
            if eta_seconds is not None:
                details["eta_seconds"] = eta_seconds
        if details:
            report("下载模型", "正在下载 Qwen3-ASR-1.7B 模型（约 1.7GB）", **details)
        else:
            report("连接模型仓库", "正在连接 Qwen3-ASR-1.7B")
        stop_event.wait(1)


def download_model_from_source(
    env_python: Path, model_dir: Path, report: ProgressReporter, log_path: Path, source: str,
) -> None:
    if source == "modelscope":
        script = (
            # Desktop installs should not probe cloud metadata for intranet acceleration.
            "import os, sys\n"
            "os.environ['MODELSCOPE_DOWNLOAD_INTRA_CLOUD'] = 'false'\n"
            "os.environ['INTRA_CLOUD_ACCELERATION'] = 'false'\n"
            "from modelscope.hub.snapshot_download import snapshot_download\n"
            "snapshot_download(model_id=sys.argv[1], local_dir=sys.argv[2])\n"
        )
    elif source == "huggingface":
        script = (
            "from huggingface_hub import snapshot_download\n"
            "import sys\n"
            "snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2])\n"
        )
    else:
        raise ValueError(f"未知模型下载源：{source}")
    stop_event = threading.Event()
    monitor = threading.Thread(
        target=report_model_download_progress,
        args=(model_dir, fetch_model_total_bytes(env_python, source), report, stop_event),
        daemon=True,
    )
    monitor.start()
    try:
        run_checked(
            [str(env_python), "-c", script, QWEN_ASR_MODEL_ID, str(model_dir)],
            error_prefix="下载 Qwen3-ASR 模型失败",
            log_path=log_path,
        )
        if not model_directory_is_complete(model_dir):
            raise RuntimeError("模型下载未返回完整文件")
    finally:
        stop_event.set()
        monitor.join(timeout=2)


def download_model(env_python: Path, model_dir: Path, report: ProgressReporter, log_path: Path) -> None:
    errors = []
    for source, label in MODEL_DOWNLOAD_SOURCES:
        def source_report(stage: str, message: str, source_label: str = label, **details: object) -> None:
            report(stage, f"{source_label}：{message}", **details)

        source_report("连接模型仓库", "正在准备下载 Qwen3-ASR-1.7B")
        try:
            if source == "modelscope":
                ensure_modelscope_downloader(env_python, source_report, log_path)
            download_model_from_source(env_python, model_dir, source_report, log_path, source)
            return
        except RuntimeError as exc:
            errors.append(f"{label}：{exc}")
            if source == "modelscope":
                report("切换下载源", "魔搭下载失败，正在尝试 Hugging Face 备用源；保留已下载文件")
    raise RuntimeError("模型下载源均失败，请检查网络后重试。\n" + "\n".join(errors))


def write_ready_marker(paths: SubFixQwenPaths, model_dir: Path) -> None:
    paths.ready_marker.write_text(
        json.dumps(
            {"model_id": QWEN_ASR_MODEL_ID, "python": str(paths.env_python), "model": str(model_dir)},
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def install(paths: SubFixQwenPaths, report: ProgressReporter) -> dict[str, object]:
    existing_status = inspect_install(paths)
    if existing_status.get("ready"):
        report("完成", "本地 Qwen 已安装，正在启用")
        return existing_status
    ensure_base_python(paths.base_python)
    paths.ready_marker.unlink(missing_ok=True)
    report("创建环境", "正在准备本地 Qwen 运行环境")
    create_or_reuse_venv(paths.base_python, paths.env_dir)
    if not python_can_import_qwen_asr(paths.env_python):
        report("安装依赖", "正在安装识别引擎依赖（qwen-asr 与 PyTorch，非模型文件）")
        install_qwen_dependencies(paths.env_python, report, paths.install_log)
    else:
        report("检查依赖", "本地 Qwen 运行环境已就绪")
    model_dir = existing_model_dir(paths)
    if model_dir is not None:
        report("复用模型", "正在复用已下载的 Qwen3-ASR-1.7B")
    else:
        try:
            ensure_model_dir_writable(paths.model_dir)
            report("下载模型", "正在下载 Qwen3-ASR-1.7B 模型（约 1.7GB）")
            download_model(paths.env_python, paths.model_dir, report, paths.install_log)
            model_dir = paths.model_dir
        except Exception as exc:
            paths.ready_marker.unlink(missing_ok=True)
            raise RuntimeError(f"下载模型失败：{exc}") from exc
    report("校验模型", "正在验证模型文件与运行环境")
    (paths.data_root / ".subfix-force-download").unlink(missing_ok=True)
    write_ready_marker(paths, model_dir)
    status = inspect_install(paths)
    if not status.get("ready"):
        paths.ready_marker.unlink(missing_ok=True)
        raise RuntimeError("本地 Qwen 安装校验未通过")
    report("完成", "本地 Qwen 已安装，可用于生成字幕")
    return status


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def make_progress_reporter(progress_path: Path | None) -> ProgressReporter:
    def report(stage: str, message: str, **details: object) -> None:
        if progress_path is not None:
            payload: dict[str, object] = {"stage": stage, "message": message, **details}
            if "progress_index" not in payload or "progress_total" not in payload:
                payload["indeterminate"] = True
            write_json(progress_path, payload)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SubFix 本地 Qwen 管理器")
    parser.add_argument("--action", choices=("status", "install"), required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--progress-json", type=Path)
    args = parser.parse_args(argv)
    paths = SubFixQwenPaths(
        args.root.resolve(),
        Path.home() / "AppData" / "Roaming" / "SubFix" if os.name == "nt" else Path.home() / "Library" / "Application Support" / "SubFix",
    )
    try:
        if args.action == "install":
            with exclusive_install_lock(paths.install_lock):
                payload = install(paths, make_progress_reporter(args.progress_json))
        else:
            payload = inspect_install(paths)
    except Exception as exc:
        payload = {"state": "error", "ready": False, "error": str(exc)}
        if args.progress_json is not None:
            write_json(args.progress_json, {"stage": "失败", "message": str(exc), "indeterminate": True})
        if args.output is not None:
            write_json(args.output, payload)
        else:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    if args.output is not None:
        write_json(args.output, payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
