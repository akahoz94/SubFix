# coding=utf-8
"""Qwen3-ASR GGUF 混合推理后端（ONNX-DML encoder + llama.cpp Vulkan decoder）。

模型与运行时来自 HaujetZhao/Qwen3-ASR-GGUF（包已厂商化为本目录下的
qwen_asr_gguf/，llama.cpp 二进制必须用 b7798 —— 更新的构建会崩 hparams，
见该仓库 issue #19；b7798 的 zip 里没有 ggml-cpu.dll，只有按架构命名的
变体，ggml_backend_load_all 认不到，必须把 ggml-cpu-haswell.dll 复制为
ggml-cpu.dll，否则 RTF 从 0.047 掉到 0.83）。

实测（3060 Ti，2026-10-24）：180s 音频 8.5s（RTF 0.047），引擎初始化 3.4s，
双模型驻留 4.5GB；文本与 torch 路径逐字一致，字级对齐 712 项。
torch 路径同素材 63.5-81.9s（RTF 0.35-0.45）、初始化 22-80s。

引擎不可用时所有入口抛 RuntimeError，调用方（auto 回退链）秒回退 torch。
"""
from __future__ import annotations

import os

# torch（显存读数会 import）与 ggml 的 libomp.dll 各带一份 OpenMP 运行时，
# 同进程共存会让后加载的一方直接 abort（实测 CLI 全流程 rc=3）。
# KMP_DUPLICATE_LIB_OK 是官方文档承认的 workaround：容忍两份运行时共存。
# ponytail: 天花板是两份 OpenMP 并行调度可能互相不感知，极端情况性能略降；
# 根治要换无 OpenMP 的 ggml-cpu 构建，那条路留到有需要再走。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import sys
import threading
from pathlib import Path
from typing import Any

BACKEND_NAME = "qwen3_gguf"
GGUF_MODEL_DIR_ENV = "SUBFIX_QWEN3_GGUF_MODEL_DIR"
GGUF_DISABLE_ENV = "SUBFIX_DISABLE_QWEN3_GGUF"
MODEL_DIR_NAME = "qwen3-asr-gguf"

_ENGINE_LOCK = threading.Lock()
_ENGINE: Any = None
_ENGINE_ERROR: str | None = None

# 打包内的量化文件名（encoder 全 int4、decoder q4_k；改精度需重新导出模型）
ASR_ENCODER_FRONTEND = "qwen3_asr_encoder_frontend.int4.onnx"
ASR_ENCODER_BACKEND = "qwen3_asr_encoder_backend.int4.onnx"
ASR_LLM = "qwen3_asr_llm.q4_k.gguf"
ALIGNER_LLM = "qwen3_aligner_llm.q4_k.gguf"
MODEL_FILES = (ASR_ENCODER_FRONTEND, ASR_ENCODER_BACKEND, ASR_LLM)


def gguf_disabled() -> bool:
    return os.getenv(GGUF_DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def discover_model_dir() -> Path:
    """模型目录：env 优先，其次 .subfix_support/models/qwen3-asr-gguf。"""
    override = os.getenv(GGUF_MODEL_DIR_ENV, "").strip()
    if override:
        path = Path(override)
        if not path.is_dir():
            raise RuntimeError(f"{GGUF_MODEL_DIR_ENV} 指向的目录不存在: {path}")
        return path
    try:
        import subfix_asr_transcribe as _core
        roots = _core.qwen3_support_roots()
    except Exception:
        roots = [Path(__file__).resolve().parent / ".subfix_support"]
    for root in roots:
        candidate = root / "models" / MODEL_DIR_NAME
        if candidate.is_dir():
            return candidate
    raise RuntimeError(
        f"未找到 {MODEL_DIR_NAME} 模型目录（可设 {GGUF_MODEL_DIR_ENV} 指向模型文件夹）"
    )


RUNTIME_DIR_NAME = "qwen3-asr-gguf-runtime"
RUNTIME_BIN_ENV = "SUBFIX_QWEN3_GGUF_BIN"
RUNTIME_SUBDIRS = ("bin", "bin-vulkan")


def discover_runtime_dir() -> Path | None:
    """llama.cpp 运行目录的外部候选（轻量包只发 python 部分，DLL 一律不打包）。

    顺序：env 指定 → 模型目录同级 qwen3-asr-gguf-runtime → 引擎包内（开发树/Max 包）。
    返回含 bin 或 bin-vulkan 的那个"父目录"，缺则 None。
    """
    candidates: list[Path] = []
    override = os.getenv(RUNTIME_BIN_ENV, "").strip()
    if override:
        candidates.append(Path(override))
    try:
        candidates.append(discover_model_dir().parent / RUNTIME_DIR_NAME)
    except Exception:
        pass
    candidates.append(Path(__file__).resolve().parent / "qwen_asr_gguf" / "inference")
    for base in candidates:
        if any((base / sub).is_dir() for sub in RUNTIME_SUBDIRS):
            return base
    return None


def unavailable_reason() -> str:
    """为什么用不了 GGUF，一句话到位（空串=可用）。调用点别再自己编原因。"""
    if gguf_disabled():
        return f"{GGUF_DISABLE_ENV}=1，GGUF 后端已禁用"
    try:
        model_dir = discover_model_dir()
    except RuntimeError as exc:
        return str(exc)
    missing = [name for name in MODEL_FILES if not (model_dir / name).exists()]
    if missing:
        return f"GGUF 模型不齐全，缺 {', '.join(missing)}（目录 {model_dir}，可设 {GGUF_MODEL_DIR_ENV}）"
    if discover_runtime_dir() is None:
        return _runtime_missing_message()
    return ""


def _engine_available() -> bool:
    if gguf_disabled():
        return False
    try:
        model_dir = discover_model_dir()
    except RuntimeError:
        return False
    if not all((model_dir / name).exists() for name in MODEL_FILES):
        return False
    return discover_runtime_dir() is not None


def _runtime_missing_message() -> str:
    return (
        "缺 llama.cpp 运行时（bin 或 bin-vulkan）：把它放到 "
        f"{RUNTIME_DIR_NAME}\\ 里（与模型同级的 models 目录下），"
        f"或设 {RUNTIME_BIN_ENV} 指向含 bin/bin-vulkan 的目录；"
        "Vulkan 实测 RTF 0.155、CUDA 0.108，都比回退 torch 快 6 倍以上"
    )


def get_engine() -> Any:
    """懒加载并缓存 QwenASREngine（含 ASR + 字级对齐器，进程内常驻）。"""
    global _ENGINE, _ENGINE_ERROR
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if _ENGINE_ERROR is not None:
            raise RuntimeError(_ENGINE_ERROR)
        runtime_dir = discover_runtime_dir()
        if runtime_dir is None:
            _ENGINE_ERROR = _runtime_missing_message()
            raise RuntimeError(_ENGINE_ERROR)
        # 让厂商化的 llama.py 从同一个目录挑 bin / bin-vulkan，别再按包内固定路径找
        os.environ.setdefault(RUNTIME_BIN_ENV, str(runtime_dir))
        try:
            from qwen_asr_gguf.inference import ASREngineConfig, AlignerConfig, QwenASREngine
        except Exception as exc:
            _ENGINE_ERROR = f"qwen_asr_gguf 运行时不可用: {exc}"
            raise RuntimeError(_ENGINE_ERROR) from exc
        try:
            model_dir = discover_model_dir()
            config = ASREngineConfig(
                model_dir=str(model_dir),
                encoder_frontend_fn=ASR_ENCODER_FRONTEND,
                encoder_backend_fn=ASR_ENCODER_BACKEND,
                llm_fn=ASR_LLM,
                onnx_provider="DML",
                llm_use_gpu=True,
                enable_aligner=True,
                align_config=AlignerConfig(
                    model_dir=str(model_dir),
                    llm_fn=ALIGNER_LLM,
                    onnx_provider="DML",
                    llm_use_gpu=True,
                ),
            )
            _ENGINE = QwenASREngine(config=config)
        except Exception as exc:
            _ENGINE_ERROR = f"Qwen3 GGUF 引擎初始化失败: {exc}"
            _ENGINE = None
            raise RuntimeError(_ENGINE_ERROR) from exc
        return _ENGINE


def shutdown_engine() -> None:
    global _ENGINE, _ENGINE_ERROR
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            try:
                _ENGINE.shutdown()
            except Exception:
                pass
        _ENGINE = None
        _ENGINE_ERROR = None


def transcribe_payload(
    audio_path: str | Path,
    language: str | None,
    context: str | None = None,
) -> dict[str, Any]:
    """转录一个音频文件，返回与 qwen3_asr payload 同构的 dict。"""
    engine = get_engine()
    from qwen_asr_gguf.inference import load_audio

    audio = load_audio(str(audio_path))
    used_context = str(context) if context else ""
    try:
        result = engine.asr(
            audio=audio,
            context=used_context,
            language=_gguf_language(language),
            temperature=0.0,
        )
        context_status = "used" if used_context else "not_requested"
    except Exception:
        if not used_context:
            raise
        # 热词上下文在这套引擎里失败就丢上下文重跑：宁可没热词，不整跑回退
        result = engine.asr(
            audio=audio,
            context="",
            language=_gguf_language(language),
            temperature=0.0,
        )
        context_status = "unsupported"
    text = str(getattr(result, "text", "") or "").strip()
    words = _alignment_words(result)
    segments = _segments_from_words(text, words, audio_path)
    return {
        "backend": BACKEND_NAME,
        "model": "qwen3-asr-1.7b-gguf",
        "language": "Chinese",
        "text": text,
        "segments": segments,
        "diagnostic": {
            "forced_aligner": "qwen3-aligner-0.6b-gguf",
            "device_map": "onnx=DML,llm=vulkan",
            "uses_word_timing": bool(words),
            "hotword_context_status": context_status,
            "alignment_item_count": len(words),
        },
    }


def align_items(
    audio_path: str | Path,
    text: str,
    language: str | None,
) -> list[dict[str, Any]]:
    """强制对齐：返回与 qwen3_force_align_items 同构的 [{text,start,end}]。"""
    engine = get_engine()
    from qwen_asr_gguf.inference import load_audio

    audio = load_audio(str(audio_path))
    result = engine.asr(
        audio=audio,
        context="",
        language=_gguf_language(language),
        temperature=0.0,
    )
    return _alignment_words(result)


def _gguf_language(language: str | None) -> str:
    value = str(language or "").strip().lower()
    if not value or value in {"auto", "zh", "zh-cn", "chinese"}:
        return "Chinese"
    return value


def _alignment_words(result: Any) -> list[dict[str, Any]]:
    alignment = getattr(result, "alignment", None)
    items = getattr(alignment, "items", None) or []
    words: list[dict[str, Any]] = []
    for item in items:
        text = str(getattr(item, "text", "") or "")
        start = getattr(item, "start_time", None)
        end = getattr(item, "end_time", None)
        if text and start is not None and end is not None:
            words.append({"text": text, "start": float(start), "end": float(end)})
    return words


def _segments_from_words(
    text: str, words: list[dict[str, Any]], audio_path: str | Path
) -> list[dict[str, Any]]:
    """words → segments，shape 与 qwen3_result_to_payload 一致。"""
    if text and words:
        return [
            {
                "start": words[0]["start"],
                "end": words[-1]["end"],
                "text": text,
                "words": words,
            }
        ]
    if not text:
        return []
    duration = 0.0
    try:
        import wave

        with wave.open(str(audio_path), "rb") as handle:
            duration = handle.getnframes() / float(handle.getframerate())
    except Exception:
        pass
    return [{"start": 0.0, "end": max(0.01, duration), "text": text}]
