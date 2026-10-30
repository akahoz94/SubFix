"""可疑字幕集中复查（v3.3.6）：只读判据 + 复查窗口回调 + 陈旧行 fail-closed。

沿用仓库做法：把 SubFix.lua 里的目标函数原文切出来，在 LuaJIT 里用桩宿主跑一遍
（SubFix.lua 本身要 Resolve 宿主才能整体执行，所以绝不去 require 它），
再加源码文本断言钉住接线。判据被改回「按列表顺序比较 / 0.5 秒改成硬编码帧数 /
陈旧行也照样打点」，这里就该红。

跑起来会看到若干 "Windows fatal exception: code 0xe24c4a02" —— 那是 LuaJIT 抛（并被我
pcall 掉）的错在 Windows 上触发 faulthandler 转储，pytest 自带的老毛病，
test_track_scope.py 一样有，不是失败。
"""
import shutil
import tempfile
from pathlib import Path

import pytest
from lupa.luajit21 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "SubFix.lua"
SOURCE = SRC.read_text(encoding="utf-8")


def segment(start_marker, end_marker):
    start = SOURCE.index(start_marker)
    return SOURCE[start:SOURCE.index(end_marker, start)]


COLLECTOR = segment("function collect_pre_delivery_final_check_issues(",
                    "function subtitle_row_middle_frame(")
WINDOW = segment("function close_pre_delivery_review_window(",
                 "function timeline_has_pre_delivery_marker(")
CLEAR = segment("function clear_pre_delivery_marker_by_custom_data(",
                "function pre_delivery_marker_frame(")
RUNNER = segment("function run_pre_delivery_final_check(",
                 "function win.On.BtnStep4.Clicked")
MARKER = segment("function add_pre_delivery_check_marker(",
                 "function run_pre_delivery_final_check(")

SETUP = '''
    collect_visible_timeline_cut_frames = function() return {} end
    collect_timeline_transition_alignment_cuts = function() return {} end
    count_effective_subtitle_chars = function(text) return #text end
    trim_text = function(text) return text:match("^%s*(.-)%s*$") end
    subtitle_has_trailing_space = function() return false end
    function find_issue(issues, kind)
        for _, issue in ipairs(issues) do
            if issue.kind == kind then return issue end
        end
    end
'''


@pytest.fixture(scope="module")
def lua():
    return LuaRuntime()


def run_lua(lua, code):
    return lua.execute(code)


@pytest.mark.parametrize("fps", [24, 29.97, 60])
def test_short_duration_and_overlap_boundaries(lua, fps):
    run_lua(lua, f"fps = {fps}")
    run_lua(lua, SETUP + COLLECTOR + '''
        for _, duration in ipairs({0, math.ceil(fps * 0.5) - 1, math.ceil(fps * 0.5), fps}) do
            local row = {text = "ok", start_frame = 100, end_frame = 100 + duration}
            local issues = collect_pre_delivery_final_check_issues({row}, nil, fps)
            assert((find_issue(issues, "字幕显示过短") ~= nil) == (duration / fps < 0.5))
            assert(row.start_frame == 100 and row.end_frame == 100 + duration)
        end
        for _, gap in ipairs({-5, -1, 0, 1}) do
            local rows = {{text = "one", start_frame = 100, end_frame = 200},
                {text = "two", start_frame = 200 + gap, end_frame = 300}}
            local issues = collect_pre_delivery_final_check_issues(rows, nil, fps)
            assert((find_issue(issues, "字幕重叠") ~= nil) == (gap < 0))
            assert(rows[1].end_frame == 200 and rows[2].start_frame == 200 + gap)
        end
        assert(not find_issue(collect_pre_delivery_final_check_issues({
            {text = "", start_frame = 0, end_frame = 0}}, nil, fps), "字幕显示过短"))
    ''')


def test_alignment_warning_reaches_final_check(lua):
    run_lua(lua, SETUP + COLLECTOR + '''
        local rows = {{text = "ok", start_frame = 100, end_frame = 200,
            audio_alignment_warning = "未取得可靠的语音起止时间，请复听后检查音频来源"}}
        local issues = collect_pre_delivery_final_check_issues(rows, nil, 24)
        local issue = find_issue(issues, "音频对齐待复查")
        assert(issue ~= nil and issue.reason == rows[1].audio_alignment_warning)
        assert(issue.row_index == 1)
        rows[1].audio_alignment_warning = nil
        assert(not find_issue(collect_pre_delivery_final_check_issues(rows, nil, 24), "音频对齐待复查"))
        assert(#rows == 1 and rows[1].start_frame == 100 and rows[1].end_frame == 200)
    ''')


@pytest.mark.parametrize("left,right,evidence,gap,expected", [
    ("micro", "phone", "microphone", 0, True),
    ("micro", "phone", "Microphone", 2, True),
    ("micro", "phone", "microphone", 3, False),
    ("micro", "phone", "microphone", -1, False),
    ("micro", "phone", "different", 0, False),
    ("micro ", "phone", "microphone", 0, False),
    ("micro", " phone", "microphone", 0, False),
    ("you", "are", "youare", 0, False),
    ("the", "speaker", "speaker", 0, False),
    ("中文句子", "就是另一个句子", "中文句子就是另一个句子", 0, False),
    ("中文micro", "phone中文", "microphone", 0, True),
])
def test_cross_row_word_requires_corroboration(lua, left, right, evidence, gap, expected):
    import json

    run_lua(lua, f"left={json.dumps(left, ensure_ascii=False)}; right={json.dumps(right, ensure_ascii=False)}; evidence={json.dumps(evidence, ensure_ascii=False)}; gap={gap}")
    run_lua(lua, SETUP + COLLECTOR + '''
        local rows = {{text = evidence, start_frame = 0, end_frame = 48},
            {text = left, start_frame = 100, end_frame = 148},
            {text = right, start_frame = 148 + gap, end_frame = 196}}
        local issues = collect_pre_delivery_final_check_issues(rows, nil, 24)
        split_issue = find_issue(issues, "疑似跨条断词")
        if split_issue then assert(split_issue.row_index == 3 and split_issue.start_frame == 148 + gap) end
    ''')
    run_lua(lua, f"assert((split_issue ~= nil) == {str(expected).lower()})")


def test_pure_cjk_rows_never_report_broken_words(lua):
    run_lua(lua, SETUP + COLLECTOR + '''
        local rows = {{text = "音频对齐", start_frame = 0, end_frame = 48},
            {text = "待复查", start_frame = 48, end_frame = 96}}
        local issues = collect_pre_delivery_final_check_issues(rows, nil, 24)
        assert(not find_issue(issues, "疑似跨条断词"))
    ''')


def test_unsorted_and_nested_overlaps_preserve_input(lua):
    run_lua(lua, SETUP + COLLECTOR + '''
        local rows = {{text = "third", start_frame = 30, end_frame = 40},
            {text = "long", start_frame = 0, end_frame = 100},
            {text = "second", start_frame = 10, end_frame = 20}}
        local issues = collect_pre_delivery_final_check_issues(rows, nil, 24)
        local overlaps = {}
        for _, issue in ipairs(issues) do
            if issue.kind == "字幕重叠" then overlaps[#overlaps + 1] = issue end
        end
        assert(#overlaps == 2)
        assert(overlaps[1].row_index == 3 and overlaps[2].row_index == 1)
        assert(overlaps[2].reason:find("第 2 条", 1, true))
        assert(rows[1].start_frame == 30 and rows[2].start_frame == 0 and rows[3].start_frame == 10)
    ''')


UI_SETUP = '''
    ui = setmetatable({}, {__index = function(_, name) return function(_, spec) return spec end end})
    SUBFIX_WINDOW_GEOMETRY = {centered_geometry = function(g) return g end}
    SUBFIX_SUBTITLE_FADES = {item_id = function(t) return t and t.id end}
    resolve_subtitle_navigation_timecode = function(row, timeline)
        assert(row.fps == 29.97)
        return "TC" .. tostring(row.start_frame)
    end
    set_tree_item_text = function(item, column, text) item.Text[column] = tostring(text or "") end
    get_tree_item_text = function(item, column) return item and item.Text[column] or "" end
    safe_refresh_tree_widget = function() end
    LogMsg = function() end
    get_tree_event_value = function(ev) return ev.item or ev.Item end
    get_selected_tree_node = function() return nil end
    timeline = {id = "original", GetName = function() return "Test timeline" end,
        SetCurrentTimecode = function(self, tc) self.last_tc = tc; self.jumps = (self.jumps or 0) + 1; return true end}
    active_timeline = timeline
    resolve = {GetProjectManager = function() return {GetCurrentProject = function()
        return {GetCurrentTimeline = function() return active_timeline end}
    end} end}
    dispatcher = {AddWindow = function(_, spec, layout)
        local tree = {ColumnWidth = {}, entries = {},
            SetHeaderLabels = function(self, labels) self.labels = labels end,
            Clear = function(self) self.entries = {} end,
            NewItem = function() return {Text = {}} end,
            IndexOfTopLevelItem = function(self, item)
                for i, entry in ipairs(self.entries) do
                    if entry.Text == item.Text then return i - 1 end
                end
                return -1
            end,
            AddTopLevelItem = function(self, item) self.entries[#self.entries + 1] = item end}
        local combo = {entries = {}, AddItem = function(self, text) self.entries[#self.entries + 1] = text end}
        local items = {ReviewIssues = tree, ReviewCategory = combo,
            ReviewStatus = {}, ClearReviewMarkers = {Text = "清空检查标记"}}
        local window = {On = setmetatable({}, {__index = function(t, key) local value = {}; t[key] = value; return value end}),
            GetItems = function() return items end,
            Show = function(self) self.visible = true end,
            Hide = function(self) self.visible = false end}
        return window
    end}
    issues = {
        {kind = "A", text = "first", reason = "first reason", start_frame = 100, row_index = 1},
        {kind = "B", text = "second", reason = "second reason", start_frame = 200, row_index = 2},
        {kind = "A", text = "third", reason = "third reason", start_frame = 300, row_index = 3}}
'''


def test_review_filters_navigation_and_close(lua):
    run_lua(lua, UI_SETUP + WINDOW + '''
        show_pre_delivery_review_window(issues, timeline, 29.97, 2)
        local window = pre_delivery_review_window
        local items = window:GetItems()
        local tree, combo = items.ReviewIssues, items.ReviewCategory
        assert(window.visible and #tree.entries == 3)
        assert(#combo.entries == 3 and combo.entries[2] == "A（2）")
        assert(tree.ColumnCount == 2 and tree.labels[1] == "问题" and tree.labels[2] == "字幕")
        assert(tree.entries[2].Text[0] == "B" and tree.entries[2].Text[1] == "second")
        assert(items.ReviewStatus.Visible == false)
        combo.CurrentIndex = 2
        window.On.ReviewCategory.CurrentIndexChanged({})
        assert(#tree.entries == 1 and tree.entries[1].Text[1] == "second")
        -- Native events can return a new proxy for the same Tree item.
        window.On.ReviewIssues.ItemDoubleClicked({item = {Text = tree.entries[1].Text}})
        assert(timeline.last_tc == "TC200" and timeline.jumps == 1)
        active_timeline = {id = "another"}
        window.On.ReviewIssues.ItemDoubleClicked({Item = tree.entries[1]})
        assert(timeline.jumps == 1 and items.ReviewStatus.Text:find("时间线已切换", 1, true))
        active_timeline = nil
        window.On.ReviewIssues.ItemDoubleClicked({item = tree.entries[1]})
        assert(timeline.jumps == 1)
        active_timeline = timeline
        timeline.SetCurrentTimecode = function() return false end
        window.On.ReviewIssues.ItemDoubleClicked({item = tree.entries[1]})
        assert(items.ReviewStatus.Text:find("定位失败", 1, true))
        window.On.CloseReview.Clicked({})
        assert(not window.visible and pre_delivery_review_window == nil)
        show_pre_delivery_review_window({}, timeline, 29.97, 0)
        window = pre_delivery_review_window
        assert(#window:GetItems().ReviewIssues.entries == 0)
        assert(window:GetItems().ReviewStatus.Visible == false)
        show_pre_delivery_review_window(issues, timeline, 29.97, 0)
        assert(not window.visible)
        pre_delivery_review_window.On.PreDeliveryReviewWindow.Close({})
        assert(pre_delivery_review_window == nil)
    ''')


def test_all_advice_is_visible_by_default_and_categories_remain_navigable(lua):
    run_lua(lua, UI_SETUP + WINDOW + '''
        issues = {
            {kind = "字幕边界未贴剪辑点", text = "cut", reason = "1 frame", start_frame = 100},
            {kind = "字幕尾部空格", text = "space", reason = "trailing", start_frame = 200},
            {kind = "字幕重叠", text = "overlap", reason = "overlap reason", start_frame = 300}}
        show_pre_delivery_review_window(issues, timeline, 29.97, 3)
        local window = pre_delivery_review_window
        local items = window:GetItems()
        local tree, combo = items.ReviewIssues, items.ReviewCategory
        assert(combo.entries[1] == "全部提示（3）" and #combo.entries == 4)
        assert(combo.CurrentIndex == 0 and #tree.entries == 3)
        for _, label in ipairs(combo.entries) do assert(not label:find("优先复查", 1, true)) end
        assert(items.ReviewStatus.Visible == false)
        window.On.ReviewIssues.ItemDoubleClicked({item = tree.entries[1]})
        assert(timeline.last_tc == "TC100")
        for index, label in ipairs(combo.entries) do
            if label == "字幕尾部空格（1）" then combo.CurrentIndex = index - 1 end
        end
        window.On.ReviewCategory.CurrentIndexChanged({})
        assert(#tree.entries == 1 and tree.entries[1].Text[1] == "space")
        window.On.ReviewIssues.ItemDoubleClicked({item = tree.entries[1]})
        assert(timeline.last_tc == "TC200")
        combo.CurrentIndex = 0
        window.On.ReviewCategory.CurrentIndexChanged({})
        assert(#tree.entries == 3)
        show_pre_delivery_review_window({issues[1]}, timeline, 29.97, 1)
        items = pre_delivery_review_window:GetItems()
        assert(#items.ReviewIssues.entries == 1)
        assert(items.ReviewStatus.Visible == false)
    ''')


def test_review_timeline_identity_fails_closed(lua):
    run_lua(lua, UI_SETUP + WINDOW + '''
        timeline.id = nil
        show_pre_delivery_review_window(issues, timeline, 29.97, 0)
        local window = pre_delivery_review_window
        local item = window:GetItems().ReviewIssues.entries[1]
        active_timeline = {GetName = function() return "Test timeline" end}
        window.On.ReviewIssues.ItemDoubleClicked({item = item})
        assert(not timeline.jumps)
        active_timeline = timeline
        window.On.ReviewIssues.ItemDoubleClicked({item = item})
        assert(timeline.jumps == 1)
        resolve.GetProjectManager = function() error("project closed") end
        window.On.ReviewIssues.ItemDoubleClicked({item = item})
        assert(timeline.jumps == 1)
        assert(window:GetItems().ReviewStatus.Text:find("时间线已切换或无法确认", 1, true))
    ''')


def test_review_window_degrades_instead_of_erroring(lua):
    # AddWindow / 属性写入是 Resolve API：宿主不配合时调用方的 pcall 必须拿到错误码，
    # 由终检写成中文状态提示，而不是让脚本在 UI 事件里崩掉。
    run_lua(lua, UI_SETUP + WINDOW + '''
        assert(pcall(show_pre_delivery_review_window, issues, timeline, 29.97, 0))
        dispatcher.AddWindow = function() error("no such method") end
        assert(not pcall(show_pre_delivery_review_window, issues, timeline, 29.97, 0))
        assert(pre_delivery_review_window == nil, "打开失败不能留下半截窗口句柄")
    ''')


def test_clear_button_checks_timeline_and_reports_failures(lua):
    assert 'ID = "ReviewDetail"' not in WINDOW
    assert 'ItemClicked = true' not in WINDOW
    run_lua(lua, UI_SETUP + WINDOW + '''
        local clear_calls = 0
        clear_all_pre_delivery_markers = function(t)
            assert(t == timeline)
            clear_calls = clear_calls + 1
            return 3, 0
        end
        show_pre_delivery_review_window(issues, timeline, 29.97, 3)
        local window = pre_delivery_review_window
        local items = window:GetItems()
        active_timeline = {id = "other"}
        window.On.ClearReviewMarkers.Clicked({})
        assert(clear_calls == 0 and items.ReviewStatus.Visible)
        active_timeline = timeline
        window.On.ClearReviewMarkers.Clicked({})
        assert(clear_calls == 1 and not items.ReviewStatus.Visible)
        assert(items.ClearReviewMarkers.Text == "已清空 3 个检查标记")
        assert(#items.ReviewIssues.entries == 3, "clearing markers must retain review results")
        clear_all_pre_delivery_markers = function() return 0, 1 end
        window.On.ClearReviewMarkers.Clicked({})
        assert(items.ReviewStatus.Visible and items.ReviewStatus.Text:find("未能清除", 1, true))
        clear_all_pre_delivery_markers = function() error("unavailable") end
        window.On.ClearReviewMarkers.Clicked({})
        assert(items.ReviewStatus.Visible)
    ''')


def test_clear_markers_preserves_manual_and_other_plugin_markers(lua):
    run_lua(lua, '''
        trim_text = function(text) return tostring(text or ""):match("^%s*(.-)%s*$") end
        timeline_has_pre_delivery_marker = function(tl, custom_data)
            local ok, markers = pcall(function() return tl:GetMarkers() end)
            if not ok or type(markers) ~= "table" then return false end
            for _, marker in pairs(markers) do
                if tostring(marker.customData or "") == tostring(custom_data) then return true end
            end
            return false
        end
    ''' + CLEAR + '''
        local markers = {
            [1] = {name = "SubFix终检：重叠", customData = "subfix-pre-delivery:1"},
            [2] = {name = "SubFix终检：过长"},
            [3] = {name = "手工备注", customData = ""},
            [4] = {name = "SubFix终检：保留其他插件", customData = "another-plugin"}}
        local deleted_frames, deleted_custom = {}, {}
        local tl = {GetMarkers = function() return markers end,
            DeleteMarkerAtFrame = function(_, frame)
                assert(frame == 2, "按帧删除只能用于取不到 customData 的旧数据")
                deleted_frames[frame] = true; markers[frame] = nil; return true
            end,
            DeleteMarkerByCustomData = function(_, custom_data)
                assert(not deleted_custom[custom_data], "must not double-count deletion")
                deleted_custom[custom_data] = true
                for frame, marker in pairs(markers) do
                    if tostring(marker.customData or "") == custom_data then markers[frame] = nil end
                end
                return true
            end}
        local count, failed = clear_all_pre_delivery_markers(tl)
        assert(count == 2 and failed == 0, string.format("%s %s", tostring(count), tostring(failed)))
        assert(markers[3] and markers[4], "手工标记与其他插件的标记不能动")
        count, failed = clear_all_pre_delivery_markers(tl)
        assert(count == 0 and failed == 0)
        markers[1] = {customData = "subfix-pre-delivery:1"}
        tl.DeleteMarkerByCustomData = function() return false end
        count, failed = clear_all_pre_delivery_markers(tl)
        assert(count == 0 and failed == 1 and markers[1], "删除失败必须计入 failed，供复查窗口提示")
        tl.GetMarkers = function() error("unavailable") end
        count, failed = clear_all_pre_delivery_markers(tl)
        assert(count == 0 and failed == 1)
        count, failed = clear_all_pre_delivery_markers(nil)
        assert(count == 0 and failed == 1)
    ''')


def test_final_check_opens_review_and_keeps_marker_workflow():
    assert "clear_all_pre_delivery_markers(timeline)" in RUNNER
    assert "add_pre_delivery_check_marker(timeline, issue, tl_start_frame)" in RUNNER
    assert "pcall(show_pre_delivery_review_window, issues, timeline, fps, marker_count)" in RUNNER
    assert "close_pre_delivery_review_window()" in RUNNER[RUNNER.index("if not shown then"):]
    assert "复查窗口打开失败" in RUNNER
    shutdown = SOURCE[SOURCE.index("function force_quit_subfix("):]
    assert "close_pre_delivery_review_window()" in shutdown


def test_review_window_has_exactly_one_entry_point():
    # 只能从终检按钮（BtnStep4 -> run_pre_delivery_final_check）打开，不许多一个入口。
    assert SOURCE.count("show_pre_delivery_review_window") == 2
    assert "run_pre_delivery_final_check" in segment("function win.On.BtnStep4.Clicked",
                                                     "function win.On.BtnStep5.Clicked")


def test_check_rejects_stale_rows_before_touching_markers(lua):
    run_lua(lua, UI_SETUP + '''
        win = {}; current_rows = {{text = "cached"}}; current_fps = 29.97
        resolve_window = function(w) return w end
        update_shared_status = function(_, message) status_message = message end
        LogMsg = function() end
        timeline.GetStartFrame = function() return 0 end
        cleared, checked, shown = 0, 0, 0
        clear_all_pre_delivery_markers = function() cleared = cleared + 1 end
        collect_pre_delivery_final_check_issues = function() checked = checked + 1; return {} end
        show_pre_delivery_review_window = function() shown = shown + 1 end
    ''' + RUNNER + '''
        run_pre_delivery_final_check(win)
        assert(cleared == 0 and checked == 0 and shown == 0)
        pre_delivery_source_timeline = {id = "another", ref = {}}
        run_pre_delivery_final_check(win)
        assert(cleared == 0 and checked == 0 and shown == 0)
        assert(status_message:find("请先刷新字幕列表", 1, true))
        assert(status_message:find("未修改字幕或标记", 1, true))
        pre_delivery_source_timeline = {id = timeline.id, ref = timeline}
        run_pre_delivery_final_check(win)
        assert(cleared == 1 and checked == 1 and shown == 1)
    ''')
    refresh = segment("local function refresh_subtitles(",
                      "function load_generate_selection_core_for_subfix(")
    assert "pre_delivery_source_timeline = nil" in refresh
    assert "pre_delivery_source_timeline = {id = SUBFIX_SUBTITLE_FADES.item_id(timeline), ref = timeline}" in refresh
    assert refresh.index("current_rows = {}") < refresh.index("pre_delivery_source_timeline = nil")
    assert refresh.index("pre_delivery_source_timeline = {id") > refresh.index("rebuild_tree_from_rows(rows, window")


def test_new_kinds_are_yellow_and_summarised():
    assert "不含口播-文本一致性" in RUNNER
    for kind in ("字幕显示过短", "字幕重叠", "疑似跨条断词", "音频对齐待复查"):
        assert f'or issue.kind == "{kind}"' in MARKER
    summary = RUNNER[RUNNER.index("local status_text = string.format("):RUNNER.index("if marker_failed_count > 0 then")]
    assert "检查项：语速/时长/字数/空隙/切点对齐/相邻重复/过短/重叠/断词/音频对齐待复查" in summary
    # 空字幕仍是 Red：只有提示级的才染黄。
    assert 'and "Yellow" or "Red"' in MARKER


def test_full_lua_source_compiles(lua):
    import json

    # LuaJIT 的文件 API 走 Windows ANSI，中文目录打不开，先拷到 ASCII 临时路径。
    target = Path(tempfile.mkdtemp(prefix="subfix_review_")) / "SubFix.lua"
    shutil.copyfile(SRC, target)
    run_lua(lua, "assert(loadfile(" + json.dumps(target.as_posix()) + "))")


def test_review_pending_markers_are_capped():
    # 待复查是「没确认」不是「有错」：整片都可能挂上，超过上限就只在复查窗口列出
    assert "PRE_DELIVERY_REVIEW_MARKER_MAX = 30" in SOURCE
    assert "review_pending_count > PRE_DELIVERY_REVIEW_MARKER_MAX" in RUNNER
    assert "在复查窗口里看" in RUNNER
