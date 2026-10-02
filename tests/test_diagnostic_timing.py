"""阶段计时必须留在诊断里：曾被白名单静默过滤，导致误判"没有计时"。"""
import importlib.util, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("asr_diag_timing", ROOT / "subfix_asr_transcribe.py")
m = importlib.util.module_from_spec(spec); sys.modules["asr_diag_timing"] = m
spec.loader.exec_module(m)

def test_stage_timing_survives_sanitize():
    out = m.sanitize_generate_diagnostic_payload({
        "diagnostic": {
            "stage_elapsed_seconds": {"asr": 48.6, "total": 52.6},
            "progress_stage_elapsed_seconds": {"validate_subtitle_batch": 44.4},
            "unlisted_noise": {"x": 1},
        },
        "backend": "qwen3",
    })["diagnostic"]
    assert out["stage_elapsed_seconds"]["asr"] == 48.6
    assert out["progress_stage_elapsed_seconds"]["validate_subtitle_batch"] == 44.4
    assert "unlisted_noise" not in out  # 白名单确实在过滤，上面的断言不是空转
