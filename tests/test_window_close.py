"""关窗＝退出：迷你版单独开着时红叉也必须生效（上游 v3.3.7 修的就是这个）。

上游那份把复查窗口的清理函数放在"打开完整版"时才初始化，所以只开迷你版时
force_quit_subfix() 里调了个 nil，红叉点了没反应。本仓库的清理函数是顶层定义、
加载即存在，所以那条 type(x) == "function" 守卫不用抄；这里改成把结论钉住：
两条路（只开迷你版 / 完整版已建）都要能藏窗、退出事件循环、取消在跑的批处理。
"""
from pathlib import Path

import pytest
from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "SubFix.lua").read_text(encoding="utf-8")


def segment(start, end):
    begin = SOURCE.index(start)
    return SOURCE[begin:SOURCE.index(end, begin)]


REVIEW_CLOSE = segment("function close_pre_delivery_review_window(",
                       "function show_pre_delivery_review_window(")
HANDLE = segment("handle_main_window_close = function()", "-- 强制退出")
FORCE = segment("function force_quit_subfix()", "function bind_full_window_close_events(")
MINI = segment("mini_win.On[WINDOW_META.mini_window_id].Close",
               "function mini_win.On.MiniTrackSpin.TextChanged")

STUBS = '''
    print = function() end
    trim_text = function(text) return text end
    subfix_kill_tree = function() end
    AI_CURL_PID_FILE = nil
    pending_report_window = nil
    workflow_log_window = nil
    NormalizeLengthConfigWin = nil
'''


def test_review_window_cleanup_is_defined_eagerly():
    """顶层定义才没有上游那个 nil；改成缩进/挪进 if 里就该红。"""
    assert "\nfunction close_pre_delivery_review_window()" in SOURCE


@pytest.fixture(scope="module")
def lua():
    return LuaRuntime()


@pytest.mark.parametrize("full_initialized", [False, True])
def test_close_hides_windows_and_exits(lua, full_initialized):
    body = (STUBS + REVIEW_CLOSE + HANDLE + FORCE + f'''
        -- 计数器留在 local（Hide 闭包与它同作用域）；其余必须全局，
        -- 因为切出来的函数在这几行之前就定义了，只认全局名。
        local mini_hidden, full_hidden, review_hidden, exits, cancels = 0, 0, 0, 0, 0
        WINDOW_META = {{mini_window_id = "mini"}}
        mini_win = {{On = {{mini = {{}}}}, Hide = function() mini_hidden = mini_hidden + 1 end}}
        win, pre_delivery_review_window = nil, nil
        function kill_normalize_background_process() cancels = cancels + 1 end
        dispatcher = {{ExitLoop = function() exits = exits + 1 end}}
        if {str(full_initialized).lower()} then
            win = {{Hide = function() full_hidden = full_hidden + 1 end}}
            pre_delivery_review_window = {{Hide = function() review_hidden = review_hidden + 1 end}}
        end
        {MINI}
        mini_win.On.mini.Close({{}})
        assert(mini_hidden == 1, "迷你窗口没关掉")
        assert(exits > 0, "事件循环没退出")
        assert(cancels == 1, "在跑的批处理没被取消")
        assert(AI_CANCEL_REQUESTED and NORMALIZE_CANCEL_REQUESTED)
        assert(full_hidden == ({str(full_initialized).lower()} and 1 or 0))
        assert(review_hidden == ({str(full_initialized).lower()} and 1 or 0))
        assert(pre_delivery_review_window == nil, "复查窗口句柄没清空，下次还会点到已销毁的窗口")
    ''')
    lua.execute(body)
