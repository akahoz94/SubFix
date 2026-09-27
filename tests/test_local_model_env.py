"""本机模型环境变量支持：SUBFIX_QWEN3_ASR_MODEL 指向的目录优先于下载目录。

对应"使用本机模型"选项——用户登记本机模型后，status/install 不得再要求联网下载。

运行：pytest tests/test_local_model_env.py
"""
import importlib.util
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_manager():
    spec = importlib.util.spec_from_file_location(
        "subfix_qwen_local_manager", ROOT / ".subfix_support" / "subfix_qwen_local_manager.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass 装饰器需要模块已注册
    spec.loader.exec_module(mod)
    return mod


def make_complete_model(tmp_path: Path) -> Path:
    model_dir = tmp_path / "Qwen3-ASR-1.7B"
    model_dir.mkdir()
    for name in ("config.json", "model.safetensors.index.json",
                 "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"):
        (model_dir / name).write_bytes(b"x")
    return model_dir


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.delenv("SUBFIX_QWEN3_ASR_MODEL", raising=False)
    return load_manager()


def test_env_model_dir_is_preferred(manager, tmp_path, monkeypatch):
    model_dir = make_complete_model(tmp_path)
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MODEL", str(model_dir))
    paths = manager.SubFixQwenPaths(tmp_path / "root", tmp_path / "data")
    assert manager.existing_model_dir(paths) == model_dir
    assert manager.has_model_artifacts(paths) is True


def test_env_model_dir_invalid_falls_through(manager, tmp_path, monkeypatch):
    empty = tmp_path / "not-a-model"
    empty.mkdir()
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MODEL", str(empty))
    paths = manager.SubFixQwenPaths(tmp_path / "root", tmp_path / "data")
    assert manager.existing_model_dir(paths) is None
    assert manager.has_model_artifacts(paths) is False


def test_env_unset_uses_data_root(manager, tmp_path):
    paths = manager.SubFixQwenPaths(tmp_path / "root", tmp_path / "data")
    model_dir = paths.model_dir
    model_dir.mkdir(parents=True)
    for name in ("config.json", "model.safetensors.index.json",
                 "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"):
        (model_dir / name).write_bytes(b"x")
    assert manager.existing_model_dir(paths) == model_dir


def test_install_reuses_env_model_without_download(manager, tmp_path, monkeypatch):
    """install() 在环境变量模型就位时：建 venv、装依赖，但绝不触发模型下载。"""
    model_dir = make_complete_model(tmp_path)
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MODEL", str(model_dir))

    paths = manager.SubFixQwenPaths(tmp_path / "root", tmp_path / "data")
    paths.data_root.mkdir(parents=True, exist_ok=True)
    (paths.root / "runtime" / "python").mkdir(parents=True)
    fake_python = paths.root / "runtime" / "python" / ("python.exe" if os.name == "nt" else "bin/python3")
    if os.name != "nt":
        fake_python.parent.mkdir(parents=True)
    fake_python.write_bytes(b"x")

    events = []
    manager.report = None  # 占位避免误用

    calls = {"venv": 0, "deps": 0, "download": 0}

    def fake_venv(base_python, env_dir):
        calls["venv"] += 1
        env_python = env_dir / (Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python")
        env_python.parent.mkdir(parents=True, exist_ok=True)
        env_python.write_bytes(b"x")

    def fake_deps(env_python, report, log_path):
        calls["deps"] += 1

    def fail_download(*args, **kwargs):
        calls["download"] += 1
        raise AssertionError("本机模型就位时不得触发模型下载")

    monkeypatch.setattr(manager, "create_or_reuse_venv", fake_venv)
    monkeypatch.setattr(manager, "python_can_import_qwen_asr", lambda p: False)
    monkeypatch.setattr(manager, "install_qwen_dependencies", fake_deps)
    monkeypatch.setattr(manager, "download_model", fail_download)

    def report(stage, message, **kwargs):
        events.append((stage, message))

    status = manager.install(paths, report)
    assert calls["venv"] == 1
    assert calls["deps"] == 1
    assert calls["download"] == 0
    assert status.get("ready") is True
    assert status.get("model") == str(model_dir)
    assert any("复用" in message for _, message in events)


def test_force_download_ignores_all_local_candidates(manager, tmp_path, monkeypatch):
    model_dir = make_complete_model(tmp_path)
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MODEL", str(model_dir))
    paths = manager.SubFixQwenPaths(tmp_path / "root", tmp_path / "data")
    paths.data_root.mkdir(parents=True, exist_ok=True)
    (paths.data_root / ".subfix-force-download").write_text("1")
    # 即使 env 模型完好，force 标记下也一律视为"无本机模型"
    assert manager.env_model_dir() is not None  # env 本身不受影响
    assert manager.existing_model_dir(paths) is None
    assert manager.has_model_artifacts(paths) is False


def test_force_download_cleared_after_install(manager, tmp_path, monkeypatch):
    model_dir = make_complete_model(tmp_path)
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MODEL", str(model_dir))
    paths = manager.SubFixQwenPaths(tmp_path / "root", tmp_path / "data")
    paths.data_root.mkdir(parents=True, exist_ok=True)
    (paths.root / "runtime" / "python").mkdir(parents=True)
    fake_python = paths.root / "runtime" / "python" / ("python.exe" if os.name == "nt" else "bin/python3")
    if os.name != "nt":
        fake_python.parent.mkdir(parents=True)
    fake_python.write_bytes(b"x")
    (paths.data_root / ".subfix-force-download").write_text("1")

    def fake_venv(base_python, env_dir):
        env_python = env_dir / (Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python")
        env_python.parent.mkdir(parents=True, exist_ok=True)
        env_python.write_bytes(b"x")

    monkeypatch.setattr(manager, "create_or_reuse_venv", fake_venv)
    monkeypatch.setattr(manager, "python_can_import_qwen_asr", lambda p: True)
    monkeypatch.setattr(manager, "download_model", lambda *a, **k: None)

    # force 下 env 模型被忽略 → 走下载分支（这里 download 被打桩）→ 数据目录模型缺失 →
    # 真实场景由 download_model 落盘；此处直接放置完整模型模拟下载结果
    import shutil
    shutil.copytree(model_dir, paths.model_dir)

    status = manager.install(paths, lambda stage, message, **kw: None)
    assert not (paths.data_root / ".subfix-force-download").exists()
    assert status.get("ready") is True
