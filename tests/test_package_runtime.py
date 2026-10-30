"""轻量发行包的运行时体积与外部运行时发现。

背景：qwen_asr_gguf/inference 下有 bin（CUDA，cublasLt 452MB + ggml-cuda 424MB + cublas 95MB）
和 bin-vulkan（61MB，大头 ggml-vulkan.dll 53MB）。实测 3060 Ti：CUDA RTF 0.108、Vulkan 0.155，
都远快于回退 torch（RTF≈1）。现在两个都不进轻量包，运行时与权重一样走外部目录，
所以这里要同时钉住"包里没有"和"外部目录真能被找到、找不到会报人话"。
"""
import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_zip_runtime_test", ROOT / "build_windows_zip.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

gguf_spec = importlib.util.spec_from_file_location("qwen3_gguf_runtime_test", ROOT / "subfix_qwen3_gguf.py")
gguf = importlib.util.module_from_spec(gguf_spec)
gguf_spec.loader.exec_module(gguf)


def test_light_package_ships_no_gpu_runtime():
    stage, _notes = build.build_stage(build.read_plugin_version(), bundle_runtime=False)
    try:
        inference = stage / ".subfix_support" / "qwen_asr_gguf" / "inference"
        assert (inference / "llama.py").is_file(), "引擎 python 部分没进包，加速后端会整个消失"
        assert not (inference / "bin").exists(), "CUDA 运行时又混进轻量包了"
        assert not (inference / "bin-vulkan").exists(), "Vulkan 运行时不该随轻量包发（61MB）"
        total = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file())
        assert total < 12 * 1024 * 1024, f"轻量包未压缩体积 {total / 1e6:.0f} MB"
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def test_external_runtime_dir_is_honoured(monkeypatch, tmp_path):
    external = tmp_path / gguf.RUNTIME_DIR_NAME
    (external / "bin-vulkan").mkdir(parents=True)
    monkeypatch.setenv(gguf.RUNTIME_BIN_ENV, str(external))
    assert gguf.discover_runtime_dir() == external


def test_missing_runtime_reason_names_both_fixes(monkeypatch):
    monkeypatch.setattr(gguf, "discover_runtime_dir", lambda: None)
    reason = gguf.unavailable_reason()
    assert gguf.RUNTIME_DIR_NAME in reason and gguf.RUNTIME_BIN_ENV in reason
    assert "运行时或模型不齐全" not in reason, "运行目录缺不该套用那句含糊话"
