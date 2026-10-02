"""显存档位必须看"别人占了多少"，不是看启动那一刻的空闲。"""
import importlib.util, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("asr_batch_sizing", ROOT / "subfix_asr_transcribe.py")
m = importlib.util.module_from_spec(spec); sys.modules["asr_batch_sizing"] = m
spec.loader.exec_module(m)

def test_batch_drops_when_others_occupy_vram():
    assert m.pick_inference_batch_size((0.0, 8.0)) == 2      # 8GB 干净桌面
    assert m.pick_inference_batch_size((1.5, 8.0)) == 2      # 达芬奇本身约占 1.5GB
    assert m.pick_inference_batch_size((2.5, 8.0)) == 1      # 再多个浏览器就降保命档
    assert m.pick_inference_batch_size((0.1, 24.0)) == 4     # 大卡才给批 4
    assert m.pick_inference_batch_size(None) == 1            # 读不到就保守

def test_oom_lesson_cannot_block_live_downgrade():
    m._AUTO_BATCH_STATE.clear()
    m._AUTO_BATCH_STATE["value"] = 2
    class Fake: max_inference_batch_size = 9
    fake = Fake()
    m._gpu_occupied_by_others_gb = lambda: (2.5, 8.0)
    m._apply_auto_batch_size(fake)
    assert fake.max_inference_batch_size == 1
