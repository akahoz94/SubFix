"""规整字幕长度·起终联合计划 + 剪辑点吸附 + 二次复查采用的规则锚点。

只断言 SubFix.lua 源码文本（脚本要 Resolve 宿主才能跑），沿用 test_win_compat.py 的取段做法：
规则一旦被改回「重复叠提前量 / 贴点吃掉音节 / 文字满分就采纳」，这里就该红。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "SubFix.lua"
SOURCE = SRC.read_text(encoding="utf-8")


def segment(start_marker, end_marker):
    start = SOURCE.index(start_marker)
    end = SOURCE.index(end_marker, start)
    return SOURCE[start:end]


PLAN = segment("function SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(",
               "function SUBFIX_AUDIO_ALIGN.reject_neighbor_gap_outlier")
APPLY = segment("function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill(",
                "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill")
REVIEW_ADOPT = segment("function SUBFIX_AUDIO_ALIGN.apply_ctc_review_results(",
                       "function SUBFIX_AUDIO_ALIGN.format_frame_range")
SELECT_QWEN = segment("function SUBFIX_AUDIO_ALIGN.select_qwen_review_candidate(",
                      "function SUBFIX_AUDIO_ALIGN.copy_review_result_into_fast_result")
QWEN_ONSET = segment("function SUBFIX_AUDIO_ALIGN.ctc_candidate_start_frame(",
                     "function SUBFIX_AUDIO_ALIGN.is_ctc_review_candidate")
RUN_QWEN_BATCH = segment("function SUBFIX_AUDIO_ALIGN.run_qwen_forced_alignment_batches(",
                         "function SUBFIX_AUDIO_ALIGN.ctc_candidate_start_frame")
NORMALIZE = segment("function run_normalize_subtitle_length(", "function win.On.BtnStep3.Clicked")
CSV_FIELDS = segment('"align_engine", "qwen_item_count", "qwen_output_path",', "local csv_file")


def test_snap_tolerance_and_minimum_duration():
    assert "local tolerance = math.max(1, math.floor((tonumber(fps) or 24) * 0.1 + 0.5))" in PLAN
    assert "local minimum_duration = math.max(1, math.ceil((tonumber(fps) or 24) * 0.18))" in PLAN
    # 只在 tolerance 内找最近的剪辑点，找不到就原样返回 —— 不贴点绝不硬吸。
    assert "cut >= lower and cut <= upper and math.abs(cut - frame) <= tolerance" in PLAN


def test_plan_takes_cut_frames_and_source_is_visible_cuts():
    assert PLAN.startswith(
        "function SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan("
        "results, rows, row_position, fps, normalize_bias_frames, cut_frames)")
    assert "build_qwen_global_writeback_plan(results, rows, row_position, fps, normalize_bias_frames, cut_frames)" in APPLY
    # 剪辑点必须来自可见画面剪辑点（终检同一来源），且 Resolve 调用包在 pcall 里失败即空表。
    getter = segment("function SUBFIX_AUDIO_ALIGN.get_alignment_cut_frames(",
                     "function SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(")
    assert "collect_visible_timeline_cut_frames(timeline)" in getter
    assert getter.index("pcall(") < getter.index("collect_visible_timeline_cut_frames")
    assert "return ok and type(cuts) == \"table\" and cuts or {}" in getter


def test_neighbor_limits_use_planned_bounds():
    assert "local prev_limit = prev_plan and prev_plan.final_end or prev_end" in PLAN
    assert "local next_limit = next_candidate and next_candidate.new_start or next_start" in PLAN
    assert "final_end = should_apply and new_end or candidate.old_end" in PLAN
    assert "elseif next_limit and (new_start >= next_limit or new_end > next_limit) then" in PLAN
    assert "elseif candidate.audio_end and (new_start >= candidate.audio_end or new_end > candidate.audio_end) then" in PLAN
    assert "elseif new_end - new_start < minimum_duration then" in PLAN


def test_start_offset_is_not_applied_twice():
    assert "local qwen_display_lead_frames = 0" in PLAN
    assert "local new_start = math.floor(qwen_start + start_bias_frames)" in PLAN
    assert "start_bias_frames - qwen_display_lead_frames" not in SOURCE
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_start_bias_frames = 0" in SOURCE
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_qwen_display_lead_frames = 0" in SOURCE


def test_snap_can_not_trim_first_or_last_syllable():
    # 起点最多回到 qwen_start - tolerance，终点最多走到 speech_end + tolerance，绝不越过语音证据。
    assert "candidate.qwen_start - tolerance" in PLAN
    assert "math.min(candidate.qwen_start, (next_limit or math.huge) - minimum_duration)" in PLAN
    assert "snap(new_end, math.max(candidate.speech_end, new_start + minimum_duration)" in PLAN
    assert "candidate.speech_end + tolerance" in PLAN


def test_rejected_qwen_results_stay_inside_joint_plan():
    assert "result.alignment_mode == \"qwen3_forced_aligner\" and not plan_by_row[result.row]" in PLAN
    assert "for pass = 1, #rows do" in PLAN
    assert "plan.can_move, plan.should_apply = false, false" in PLAN
    assert "plan.start_cut_snapped, plan.end_cut_snapped, plan.speech_end_verified = false, false, false" in PLAN


def test_accepted_candidate_score_rejects_nan_and_inf():
    accepted = segment("function SUBFIX_AUDIO_ALIGN.qwen_global_candidate_is_accepted(",
                       "function SUBFIX_AUDIO_ALIGN.get_alignment_cut_frames(")
    assert "row_remap_score ~= row_remap_score or row_remap_score == math.huge" in accepted


def test_joint_plan_only_moves_timing():
    assert "row.start_frame = final_start" in APPLY
    assert re.search(r"row\.text\s*=", APPLY) is None


def test_review_candidate_start_follows_qwen_ctc_start():
    assert "if result.alignment_mode == \"qwen3_forced_aligner\" and result.ctc_start_frame ~= nil then" in QWEN_ONSET
    assert "local onset, distance = SUBFIX_AUDIO_ALIGN.qwen_review_audio_onset(result, candidate_start, fps)" in segment(
        "function SUBFIX_AUDIO_ALIGN.is_ctc_review_candidate(",
        "function SUBFIX_AUDIO_ALIGN.review_audio_source_for_window")
    assert "reasons[#reasons + 1] = \"audio_onset_mismatch\"" in segment(
        "function SUBFIX_AUDIO_ALIGN.is_ctc_review_candidate(",
        "function SUBFIX_AUDIO_ALIGN.review_audio_source_for_window")


def test_qwen_onset_evidence_is_available_so_gate_needs_no_fail_open():
    # 上游的音频起音门依赖 result.local_onset_frames；本仓库 Qwen 批量/serve 两条腿都填了这个字段，
    # 所以上面的门不需要 fail-open。这段接线一旦断掉，采纳会静默归零 —— 此处必须红。
    assert "local_onset_frames = SUBFIX_AUDIO_ALIGN.map_stable_ts_onsets_to_frames(batch_payload.speech_onsets, audio_source, fps)" in RUN_QWEN_BATCH
    assert "result.local_onset_frames = reference_info.local_onset_frames" in RUN_QWEN_BATCH
    assert "for _, value in ipairs(result.local_onset_frames or {}) do" in QWEN_ONSET


def test_qwen_review_adoption_needs_shared_audio_evidence():
    assert "return false, \"review_text_unreliable\"" in SELECT_QWEN
    assert "return false, \"audio_evidence_not_better\"" in SELECT_QWEN
    assert "return true, \"closer_to_shared_audio_onset\"" in SELECT_QWEN
    assert "math.abs(fast_onset - review_onset) > 1" in SELECT_QWEN
    assert "fast_distance - review_distance < math.max(2, math.ceil(rate * 0.05))" in SELECT_QWEN
    # 快速候选没有 row 时不能抛错（本仓库 result.row 可能为 nil，硬失败会打断用户操作）。
    assert "tonumber(fast_result.row and fast_result.row.start_frame)" in SELECT_QWEN
    assert "should_adopt, selection_reason = SUBFIX_AUDIO_ALIGN.select_qwen_review_candidate(" in REVIEW_ADOPT
    assert "fast_result.review_selection_reason = selection_reason" in REVIEW_ADOPT
    assert "if selection_reason then" in REVIEW_ADOPT
    # 旧的填充尾巴不再挡住 Qwen 复核采纳（联合计划已经校验过语音终点）。
    assert "and (result.alignment_mode == \"qwen3_forced_aligner\" or not prev_end or candidate_start >= prev_end)" in segment(
        "function SUBFIX_AUDIO_ALIGN.ctc_review_candidate_passes_guards(",
        "function SUBFIX_AUDIO_ALIGN.select_qwen_review_candidate")


def test_pending_review_marks_and_count():
    warn = "row.audio_alignment_warning = \"未取得可靠的语音起止时间，请复听后检查音频来源\""
    assert warn in APPLY
    assert APPLY.index(warn) < APPLY.index("find_primary_audio_track_batches(timeline, rows, fps)")
    assert "return nil, false, batch_plan_err or \"没有可用音频源\", {review_required_count = #rows}" in APPLY
    assert "return nil, false, \"没有 Qwen3 强制对齐结果\", {review_required_count = #rows}" in APPLY
    assert "row.audio_alignment_verified = can_move and qwen_global_plan.speech_end_verified == true" in APPLY
    assert "row.audio_alignment_end_at_cut = can_move and qwen_global_plan.end_cut_snapped == true" in APPLY
    assert "if row.audio_alignment_verified then row.audio_alignment_warning = nil end" in APPLY
    assert "decision_counts.review_required_count = 0" in APPLY
    assert APPLY.index("decision_counts.review_required_count = 0") < APPLY.index("write_gap_fill_alignment_diagnostics(")
    assert "if aligned_count == 0 and decision_counts.review_required_count < #rows then\n        return 0, true, nil, decision_counts" in APPLY
    assert "cut_snapped_count = 0," in APPLY
    assert "fallback_assigned_count = tonumber(batch_plan.fallback_assigned_count) or 0," in APPLY


def test_review_count_reaches_completion_message():
    assert "local review_count = tonumber(audio_alignment_stats and audio_alignment_stats.review_required_count) or 0" in NORMALIZE
    assert "条语音边界待确认，请打开集中复查。" in NORMALIZE
    assert NORMALIZE.index("条语音边界待确认") < NORMALIZE.index("update_shared_status(window, msg)")


def test_diagnostic_columns_expose_snap_and_selection():
    for column in ("start_cut_snapped", "end_cut_snapped", "review_selection_reason"):
        assert '"%s"' % column in CSV_FIELDS
        assert "result.%s" % column in SOURCE


def test_bias_mode_default_stays_auto():
    # 本仓库刻意保留 auto（自动算中位偏移），不跟上游改成 manual。
    dialog = segment("function show_normalize_length_config_dialog(", "function get_selected_backup_entry(")
    assert "bias_mode = \"auto\"" in dialog


def test_subfix_lua_compiles_under_luajit():
    import shutil
    import tempfile
    from lupa.luajit21 import LuaRuntime

    # LuaJIT 的文件 API 走 Windows ANSI，中文目录打不开，先拷到 ASCII 临时路径。
    target = Path(tempfile.mkdtemp(prefix="subfix_align_snap_")) / "SubFix.lua"
    shutil.copyfile(SRC, target)
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("local f, e = loadfile([[%s]]) SUBFIX_OK = (f ~= nil) SUBFIX_ERR = tostring(e)"
                % target.as_posix())
    assert bool(lua.eval("SUBFIX_OK")), lua.eval("SUBFIX_ERR")
