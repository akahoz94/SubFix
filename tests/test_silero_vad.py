"""Silero VAD 挂点：滞回状态机语义、模型链路 smoke、v5 接管与回退。"""

from array import array
import importlib.util
import math
from pathlib import Path
import wave

import pytest

from test_v5_onset_protection import write_audio

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 16000
WINDOW = 512


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vad = _load("silero_vad_tests", "subfix_silero_vad.py")
v5 = _load("v5_silero_tests", "subfix_generate_v5.py")


def probs_for(spans, total_seconds, high=0.92, low=0.01):
    """把 (起始秒, 结束秒, 概率) 片段铺到逐窗概率序列上。"""
    count = int(total_seconds * SAMPLE_RATE // WINDOW)
    probs = [low] * count
    for start_sec, end_sec, level in spans:
        first = int(start_sec * SAMPLE_RATE // WINDOW)
        last = min(count, int(math.ceil(end_sec * SAMPLE_RATE / WINDOW)))
        for index in range(first, last):
            probs[index] = level
    return probs


def test_short_dip_inside_speech_does_not_split_utterance():
    probs = probs_for([(1.0, 4.0, 0.92), (4.0, 4.3, 0.4), (4.3, 6.0, 0.92)], 7.0)
    intervals = vad.intervals_from_probs(probs, SAMPLE_RATE)
    assert len(intervals) == 1
    start, end = intervals[0]
    assert start == pytest.approx(0.96, abs=2 * WINDOW / SAMPLE_RATE)
    assert end == pytest.approx(6.04, abs=2 * WINDOW / SAMPLE_RATE)


def test_long_silence_splits_into_two_intervals():
    probs = probs_for([(1.0, 4.0, 0.92), (6.5, 8.5, 0.92)], 9.5)
    intervals = vad.intervals_from_probs(probs, SAMPLE_RATE)
    assert len(intervals) == 2
    assert intervals[0][0] == pytest.approx(0.96, abs=2 * WINDOW / SAMPLE_RATE)
    assert intervals[0][1] == pytest.approx(4.04, abs=2 * WINDOW / SAMPLE_RATE)
    assert intervals[1][0] == pytest.approx(6.46, abs=2 * WINDOW / SAMPLE_RATE)
    assert intervals[1][1] == pytest.approx(8.54, abs=2 * WINDOW / SAMPLE_RATE)


def test_blip_shorter_than_min_speech_is_dropped():
    probs = probs_for([(1.0, 1.1, 0.92), (3.0, 4.0, 0.92)], 5.0)
    intervals = vad.intervals_from_probs(probs, SAMPLE_RATE)
    assert len(intervals) == 1
    assert intervals[0][0] == pytest.approx(2.96, abs=2 * WINDOW / SAMPLE_RATE)


def test_prob_between_thresholds_keeps_speech_open():
    # 0.35 < 概率 < 0.5 属于滞回带，不算静音，不应提前闭合语音区。
    probs = probs_for([(1.0, 3.0, 0.92), (3.0, 3.2, 0.42), (3.2, 5.0, 0.92)], 6.0)
    intervals = vad.intervals_from_probs(probs, SAMPLE_RATE)
    assert len(intervals) == 1
    assert intervals[0][1] == pytest.approx(5.04, abs=2 * WINDOW / SAMPLE_RATE)


def test_speech_running_to_the_end_is_closed_at_total_length():
    # 偏离官方实现：官方把尾部 <500ms 静音并进区间（闭到流末尾），
    # 这里闭在 VAD 判定的语音结束点（temp_end）+ padding，对字幕吸附更紧。
    probs = probs_for([(0.5, 3.9, 0.92)], 4.0)
    intervals = vad.intervals_from_probs(probs, SAMPLE_RATE, total_samples=4 * SAMPLE_RATE)
    assert intervals[0][1] == pytest.approx(3.944, abs=WINDOW / SAMPLE_RATE)


def test_padding_merges_touching_intervals():
    probs = probs_for([(0.5, 1.0, 0.92), (1.0, 1.5, 0.92)], 2.0)
    intervals = vad.intervals_from_probs(probs, SAMPLE_RATE)
    assert len(intervals) == 1


def test_resample_keeps_48k_audio_on_the_same_timeline():
    # 1 秒 48k 正弦 → 16k 后长度与时间轴对应关系必须正确。
    source = array("h", [int(6000 * math.sin(i * 0.6)) for i in range(48000)])
    resampled, ratio = vad._resample_to_16k(vad._to_f32(source), 48000)
    assert len(resampled) == SAMPLE_RATE
    assert ratio == pytest.approx(1.0)


def test_resample_handles_odd_rates_like_44100():
    source = array("h", [int(6000 * math.sin(i * 0.6)) for i in range(44100)])
    resampled, _ = vad._resample_to_16k(vad._to_f32(source), 44100)
    assert abs(len(resampled) - SAMPLE_RATE) <= 2


@pytest.mark.skipif(not vad.available(), reason="onnxruntime 或 silero_vad.onnx 未就绪")
def test_session_smoke_on_silence_returns_no_intervals():
    silence = array("h", [0] * SAMPLE_RATE * 2)
    assert vad.detect_speech_intervals(silence, SAMPLE_RATE) == []


def test_v5_prefers_silero_and_reports_detector(tmp_path, monkeypatch):
    monkeypatch.delenv("SUBFIX_DISABLE_SILERO", raising=False)
    audio = tmp_path / "speech.wav"
    write_audio(audio, [(1.0, 6000), (1.0, 0)])
    monkeypatch.setattr(v5.silero_vad, "detect_speech_intervals", lambda samples, rate: [(0.4, 0.8)])
    rows = [{"text": "一句", "start_frame": 18, "end_frame": 21}]
    output, diagnostic = v5.refine_subtitle_boundaries(rows, audio, 0, 30.0)
    assert diagnostic["speech_detector"] == "silero"
    assert output[0]["start_frame"] == 12
    assert output[0]["end_frame"] == 24


def test_v5_empty_silero_regions_expand_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SUBFIX_DISABLE_SILERO", raising=False)
    audio = tmp_path / "tone.wav"
    write_audio(audio, [(2.0, 6000)])
    monkeypatch.setattr(v5.silero_vad, "detect_speech_intervals", lambda samples, rate: [])
    rows = [{"text": "一句", "start_frame": 18, "end_frame": 21}]
    output, diagnostic = v5.refine_subtitle_boundaries(rows, audio, 0, 30.0)
    assert diagnostic["speech_detector"] == "silero"
    assert (output[0]["start_frame"], output[0]["end_frame"]) == (18, 21)
    assert output[0]["timing_decision"] == "forced_alignment_kept"


def test_v5_falls_back_to_rms_when_vad_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("SUBFIX_DISABLE_SILERO", raising=False)
    audio = tmp_path / "quiet-tail.wav"
    write_audio(audio, [(0.5, 0), (0.6, 5000), (0.9, 300), (0.5, 0)])
    def broken(samples, rate):
        raise RuntimeError("推理环境损坏")
    monkeypatch.setattr(v5.silero_vad, "detect_speech_intervals", broken)
    rows = [{"text": "完整的句尾", "start_frame": 15, "end_frame": 60}]
    output, diagnostic = v5.refine_subtitle_boundaries(rows, audio, 0, 30.0)
    assert diagnostic["speech_detector"] == "silero_failed"
    assert output[0]["start_frame"] <= 15
    assert output[0]["end_frame"] >= 60


def test_v5_silero_disabled_env_switches_back_to_rms(tmp_path, monkeypatch):
    audio = tmp_path / "tone.wav"
    write_audio(audio, [(1.0, 6000), (1.0, 0)])
    monkeypatch.setenv("SUBFIX_DISABLE_SILERO", "1")
    def must_not_run(samples, rate):
        raise AssertionError("禁用后不应触发 Silero 推理")
    monkeypatch.setattr(v5.silero_vad, "detect_speech_intervals", must_not_run)
    rows = [{"text": "一句", "start_frame": 18, "end_frame": 21}]
    output, diagnostic = v5.refine_subtitle_boundaries(rows, audio, 0, 30.0)
    assert diagnostic["speech_detector"] == "rms"
