"""测试进程默认禁用 Silero，让既有 v5 用例继续锁定 RMS 回退路径的语义。

合成正弦音频会被 Silero 判为非语音，开启它会让期望“能量区扩张”的
用例失去探测结果。Silero 专属行为由 test_silero_vad.py 自行解禁并用
桩函数/真实模型 smoke 验证。
"""

import os

os.environ.setdefault("SUBFIX_DISABLE_SILERO", "1")
