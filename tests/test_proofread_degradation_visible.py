"""文稿校对的同音纠错不许静默失效：缺 pypinyin 必须进度可见 + 诊断留痕。

Item 1 的两条硬要求：
  · 依赖缺失时用户看得见原因，但管线照跑（标点修正仍生效、时间轴不动）
  · 埋点键必须活过 sanitize_generate_diagnostic_payload 白名单，否则等于没埋
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MISSING_MESSAGE = "同音字校对未启用：缺少 pypinyin（识别环境里 pip 安装后恢复）"
# 界面用的阶段机器码沿用既有的英文蛇形码；改名会打断 Lua 的 200ms 轮询翻译
PROOFREAD_DEGRADE_STAGE = "proofread_homophone_missing"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _BlockPypinyin:
    """让 from pypinyin import lazy_pinyin 真的抛 ImportError，走模块自己的降级分支。"""

    def find_spec(self, fullname, path=None, target=None):  # noqa: D102
        if fullname == "pypinyin" or fullname.startswith("pypinyin."):
            raise ImportError("pypinyin 被测试屏蔽")
        return None


def _proofread_without_pypinyin(monkeypatch, name: str):
    monkeypatch.delitem(sys.modules, "pypinyin", raising=False)
    blocker = _BlockPypinyin()
    sys.meta_path.insert(0, blocker)
    try:
        module = _load(name, "subfix_script_proofread.py")
    finally:
        sys.meta_path.remove(blocker)
    assert module.lazy_pinyin is None, "屏蔽失败：pypinyin 仍然导入了"
    return module


def _as_proofread_proxy(module):
    return types.SimpleNamespace(
        proofread_rows=module.proofread_rows,
        pinyin_missing_message=module.pinyin_missing_message,
    )


def test_blocked_pypinyin_announces_in_progress_and_diagnostic(tmp_path, monkeypatch):
    helper = _load("asr_proofread_degrade", "subfix_asr_transcribe.py")
    blocked = _proofread_without_pypinyin(monkeypatch, "proofread_blocked")
    monkeypatch.setattr(helper, "script_proofread", _as_proofread_proxy(blocked))
    progress_path = tmp_path / "progress.json"

    rows = [
        {"text": "这件事在说一遍", "start_frame": 0, "end_frame": 30},
        {"text": "你好,世界", "start_frame": 30, "end_frame": 60},
    ]
    diagnostic: dict = {}
    # 挂点真实语句：先校对、再并诊断、最后播报（见 run_generate_subtitles_batch_plan_v4）
    output, proofread_diagnostic = helper.script_proofread.proofread_rows(
        rows, "这件事再说一遍，你好，世界"
    )
    diagnostic.update(proofread_diagnostic)
    helper.report_proofread_degradation(progress_path, diagnostic)

    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    assert payload["stage"] == PROOFREAD_DEGRADE_STAGE
    assert payload["message"] == MISSING_MESSAGE
    assert diagnostic["proofread_pinyin_available"] is False
    assert diagnostic["proofread_unavailable_reason"] == MISSING_MESSAGE

    # 跑成了：不崩、不跳步；标点修正仍生效，同音错字保守保留，时间轴一字未动
    assert diagnostic["proofread_status"] == "applied_degraded"
    assert output[0]["text"] == "这件事在说一遍"
    assert output[1]["text"] == "你好，世界"
    assert [(row["start_frame"], row["end_frame"]) for row in output] == [(0, 30), (30, 60)]

    # 白名单必须放行，否则"运行日志里查不到"又被当成"没埋点"
    diagnostic["unlisted_noise"] = {"x": 1}
    kept = helper.sanitize_generate_diagnostic_payload(
        {"ok": True, "backend": "qwen3_asr", "diagnostic": diagnostic, "subtitle_rows": output}
    )["diagnostic"]
    for key in (
        "proofread_pinyin_available",
        "proofread_unavailable_reason",
        "proofread_status",
        "proofread_homophone_replace_count",
        "proofread_punctuation_replace_count",
    ):
        assert key in kept, key
    assert kept["proofread_unavailable_reason"] == MISSING_MESSAGE
    # 缺依赖的现场：同音 0 处、标点照旧 1 处——用户能从日志区分"没跑"和"跑了一半"
    assert kept["proofread_homophone_replace_count"] == 0
    assert kept["proofread_punctuation_replace_count"] == 1
    assert "unlisted_noise" not in kept  # 白名单确实在过滤，上面的断言不是空转


def test_blocked_pypinyin_never_breaks_the_proofread_run(tmp_path, monkeypatch):
    """缺依赖只该少一项能力，不许把整条转录管线带崩。"""
    helper = _load("asr_proofread_crash", "subfix_asr_transcribe.py")
    blocked = _proofread_without_pypinyin(monkeypatch, "proofread_blocked_crash")
    monkeypatch.setattr(helper, "script_proofread", _as_proofread_proxy(blocked))
    rows = [{"text": "第一句", "start_frame": 0, "end_frame": 30}]
    output, diagnostic = blocked.proofread_rows(rows, "第一句")
    assert output[0]["text"] == "第一句"
    helper.report_proofread_degradation(None, diagnostic)  # 没传 --progress-json 也不许抛
    assert diagnostic["proofread_pinyin_available"] is False


def test_present_pypinyin_stays_silent_and_keeps_homophone_fix(tmp_path, monkeypatch):
    pytest.importorskip("pypinyin")
    helper = _load("asr_proofread_present", "subfix_asr_transcribe.py")
    live = _load("proofread_present", "subfix_script_proofread.py")
    assert live.pinyin_available() is True and live.pinyin_missing_message() == ""
    monkeypatch.setattr(helper, "script_proofread", _as_proofread_proxy(live))
    progress_path = tmp_path / "progress.json"

    diagnostic: dict = {}
    helper.report_proofread_degradation(progress_path, diagnostic)
    assert not progress_path.exists(), "依赖在位却播了降级消息"
    assert diagnostic == {}

    rows = [{"text": "这件事在说一遍", "start_frame": 0, "end_frame": 30}]
    output, proofread_diagnostic = live.proofread_rows(rows, "这件事再说一遍")
    assert output[0]["text"] == "这件事再说一遍", "在位的同音纠错被改坏了"
    assert proofread_diagnostic["proofread_status"] == "applied"


def test_degradation_report_sits_at_the_proofread_hook():
    source = (ROOT / "subfix_asr_transcribe.py").read_text(encoding="utf-8")
    hook = source.index("script_proofread.proofread_rows(")
    report = source.index("report_proofread_degradation(progress_path, diagnostic)")
    assert source.index("protect_word_boundaries(") < hook < report
    assert report < source.index("enforce_hard_char_limit(")
    assert MISSING_MESSAGE in (ROOT / "subfix_script_proofread.py").read_text(encoding="utf-8")


def test_existing_stage_codes_and_phase_labels_untouched():
    """既有机器码一个不许改名或删除（Lua 侧按码翻译文案、每 200ms 轮询）。"""
    helper = _load("asr_proofread_stages", "subfix_asr_transcribe.py")
    source = (ROOT / "subfix_asr_transcribe.py").read_text(encoding="utf-8")
    for stage in (
        "prepare_subtitle_batch",
        "generate_subtitle_batch",
        "validate_subtitle_batch",
        "select_subtitle_sources",
        "segment_subtitles",
        "write_output",
        "done",
        "failed",
        "align_subtitle_batch",
        "cut_audio",
        "detect_speech",
    ):
        assert f'"{stage}"' in source, stage
    assert helper.QWEN_LOCAL_PHASE_LABELS == {
        "load_qwen_model": "正在加载语音模型",
        "run_qwen_asr": "正在识别语音",
        "load_qwen_aligner": "正在加载字幕对齐模型",
        "qwen_align": "正在对齐字幕时间",
    }
    assert PROOFREAD_DEGRADE_STAGE not in helper.QWEN_LOCAL_PHASE_LABELS
    assert "inference_batch_size" not in helper.QWEN_LOCAL_PHASE_LABELS


def test_asr_env_installer_declares_pypinyin():
    """识别环境依赖必须真的被装上：本机的 pypinyin 是撞运气装上的不算数。"""
    installer = (ROOT / ".subfix_support" / "setup_asr_env.cmd").read_bytes().decode("gbk")
    for line in installer.splitlines():
        if "-m pip install -U qwen-asr" in line:
            assert " pypinyin" in line, line
