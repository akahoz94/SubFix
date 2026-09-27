"""Silero VAD 语音区检测（ONNX 运行时），供 v5 边界修正替代能量法。

模型 silero_vad.onnx（MIT License，Copyright (c) 2020-present Silero Team）
取自 github.com/snakers4/silero-vad，随包分发于 .subfix_support/models/；
检测语义参照官方 get_speech_timestamps，边界策略参照
AutoSubs（MIT License，Copyright (c) 2023 Tom Moroney）vad_snap/vad.rs 的
参数取舍：min_silence 500ms（200ms 会切碎自然换气），单线程推理
（小图多线程在 Windows 上有死锁风险）。

运行时依赖 onnxruntime（自带 numpy）。模型或运行时缺失、推理失败时，
调用方（v5）回退既有 RMS 能量法，本模块的存在不改变失败行为。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

MODEL_FILENAME = "silero_vad.onnx"
TARGET_SAMPLE_RATE = 16000
# Silero 要求固定窗口（16k 下 512 样本）；尾部不足一窗的音频（<=32ms）不送推理。
WINDOW_SAMPLES = 512
# 官方 OnnxWrapper 每步喂 64 样本上文 + 512 窗共 576 样本；直接喂 512 会在
# 真实语音上整体塌到 ~0.002 的假概率（静音/纯音测不出，必须真人语音验证）。
CONTEXT_SAMPLES = 64

_session_cache: dict[str, Any] = {}


def _model_candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    override = os.environ.get("SUBFIX_SILERO_VAD_MODEL")
    candidates = [
        Path(override) if override else None,
        here / "models" / MODEL_FILENAME,
        here / ".subfix_support" / "models" / MODEL_FILENAME,
        here / MODEL_FILENAME,
    ]
    return [path for path in candidates if path is not None]


def resolve_model_path() -> Path | None:
    for path in _model_candidates():
        if path.is_file():
            return path
    return None


def available() -> bool:
    """运行时与模型文件都就绪才可用；任何缺失走调用方回退。"""
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False
    return resolve_model_path() is not None


def _load_session(model_path: Path):
    import onnxruntime as ort

    cached = _session_cache.get(str(model_path))
    if cached is not None:
        return cached
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(model_path), options, providers=["CPUExecutionProvider"]
    )
    _session_cache[str(model_path)] = session
    return session


def _resample_to_16k(samples_f32: "Any", sample_rate: int) -> tuple["Any", float]:
    """任意单声道采样率转 16k，返回 (f32 样本, 每输出样本对应的原样本步长)。

    ponytail: 盒式平均粗降采样 + 线性插值的朴素重采样，语音频段（<8kHz）
    精度够用；升级路径是让上游 ffmpeg 提取音频时直接 -ar 16000。
    """
    import numpy as np

    if sample_rate == TARGET_SAMPLE_RATE:
        return samples_f32, 1.0
    ratio = sample_rate / TARGET_SAMPLE_RATE
    if ratio > 2:
        block = int(ratio)
        trimmed = len(samples_f32) - len(samples_f32) % block
        if trimmed:
            samples_f32 = samples_f32[:trimmed].reshape(-1, block).mean(axis=1)
            ratio = sample_rate / block / TARGET_SAMPLE_RATE
    source_positions = np.arange(len(samples_f32), dtype=np.float64)
    out_count = max(1, int(round(len(samples_f32) / ratio)))
    target_positions = np.arange(out_count, dtype=np.float64) * ratio
    return np.interp(target_positions, source_positions, samples_f32).astype(np.float32), ratio


def _speech_probs(samples_f32: "Any", sample_rate: int) -> tuple[list[float], int]:
    """滑窗推理，返回 (每窗语音概率, 重采样后样本总数)。"""
    import numpy as np

    model_path = resolve_model_path()
    if model_path is None:
        raise RuntimeError("未找到 silero_vad.onnx 模型文件")
    session = _load_session(model_path)
    resampled, ratio = _resample_to_16k(samples_f32, sample_rate)
    total = len(resampled)
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr_feed = np.array(TARGET_SAMPLE_RATE, dtype=np.int64)
    context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
    probs: list[float] = []
    for start in range(0, total - WINDOW_SAMPLES + 1, WINDOW_SAMPLES):
        window = resampled[start:start + WINDOW_SAMPLES]
        feed = np.concatenate([context, window]).reshape(1, -1)
        output, state = session.run(
            None, {"input": feed, "state": state, "sr": sr_feed}
        )
        context = feed[0, -CONTEXT_SAMPLES:].copy()
        probs.append(float(output[0][0]))
    return probs, total


def intervals_from_probs(
    probs: list[float],
    sample_rate: int = TARGET_SAMPLE_RATE,
    *,
    window_samples: int = WINDOW_SAMPLES,
    threshold: float = 0.5,
    neg_threshold: float | None = None,
    min_speech_ms: float = 250.0,
    min_silence_ms: float = 500.0,
    speech_pad_ms: float = 40.0,
    total_samples: int | None = None,
) -> list[tuple[float, float]]:
    """官方 get_speech_timestamps 语义：滞回判决 + 最短语音/静音 + 边界 padding。

    输出按秒计的合并语音区间。纯函数，便于脚本化概率序列的单元测试。
    """
    if neg_threshold is None:
        neg_threshold = threshold - 0.15
    min_silence = sample_rate * min_silence_ms / 1000.0
    min_speech = sample_rate * min_speech_ms / 1000.0
    if total_samples is None:
        total_samples = max(int(sample_rate * min_speech_ms / 1000.0), len(probs) * window_samples)

    raw: list[tuple[int, int]] = []
    speech = False
    start = 0
    temp_end: int | None = None
    for index, prob in enumerate(probs):
        if not speech and prob >= threshold:
            start = index * window_samples
            speech = True
            temp_end = None
        elif speech and prob < neg_threshold:
            if temp_end is None:
                temp_end = index * window_samples
            if (index + 1) * window_samples - temp_end >= min_silence:
                if temp_end - start >= min_speech:
                    raw.append((start, temp_end))
                speech = False
        elif speech and temp_end is not None and prob >= neg_threshold:
            temp_end = None
    if speech:
        end = total_samples if temp_end is None else temp_end
        if end - start >= min_speech:
            raw.append((start, end))

    pad = speech_pad_ms / 1000.0
    merged: list[tuple[float, float]] = []
    for begin, finish in raw:
        padded_start = max(0.0, begin / sample_rate - pad)
        padded_end = min(total_samples / sample_rate, finish / sample_rate + pad)
        if merged and padded_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], padded_end))
        else:
            merged.append((padded_start, padded_end))
    return [(start, end) for start, end in merged if end > start]


def _to_f32(samples: Any) -> "Any":
    import numpy as np

    return np.frombuffer(bytes(samples), dtype=np.int16).astype(np.float32) / 32768.0


def detect_speech_intervals(samples: Any, sample_rate: int) -> list[tuple[float, float]]:
    """输入单声道 int16 PCM 样本，输出语音区间（秒，按原音频时间轴）。

    任何异常都直接抛出，由调用方决定回退策略。
    """
    buffer = _to_f32(samples)
    if len(buffer) < WINDOW_SAMPLES:
        return []
    probs, total = _speech_probs(buffer, sample_rate)
    return intervals_from_probs(probs, TARGET_SAMPLE_RATE, total_samples=total)
