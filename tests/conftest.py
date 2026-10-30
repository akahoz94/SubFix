"""测试进程默认禁用 Silero，让既有 v5 用例继续锁定 RMS 回退路径的语义。

合成正弦音频会被 Silero 判为非语音，开启它会让期望“能量区扩张”的
用例失去探测结果。Silero 专属行为由 test_silero_vad.py 自行解禁并用
桩函数/真实模型 smoke 验证。
"""

import os

os.environ.setdefault("SUBFIX_DISABLE_SILERO", "1")

# Windows 10.0.26300 的挂载点安全策略拒绝 pathlib.resolve() 遍历 pytest 为
# tmp_path 建的 "*current" junction（WinError 448），整个套件在清理阶段崩掉。
# 死链接清理是纯外观功能，直接停用。
try:
    _noop = lambda root: None
    import _pytest.pathlib as _pytest_pathlib

    _pytest_pathlib.cleanup_dead_symlinks = _noop
    import _pytest.tmpdir as _pytest_tmpdir

    _pytest_tmpdir.cleanup_dead_symlinks = _noop
except Exception:
    pass

