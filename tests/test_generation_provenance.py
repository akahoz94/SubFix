"""生成记录（provenance）：只增元数据，绝不改动识别文本/时序/批处理。

对齐上游 v3.3.6 的规则，但按 windows 分叉适配：
- 主入口只暴露 --generate-engine v5，所以只移植 v5 相关的 main 用例；
- 分叉没有 v5→v4 profile 回退，故不移植 test_profile_trace_tracks_v5_to_v4_fallback；
- 本机默认编码是 GBK，读取 helper 写出的 UTF-8 文件必须显式 encoding="utf-8"。
"""
import argparse
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location("subfix_trace_test", ROOT / "subfix_asr_transcribe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_args(tmp_path):
    return argparse.Namespace(
        model="large-v3-turbo", backend="doubao_asr", generate_engine="v5",
        mode="generate_subtitles_batch", subtitle_mode="narration", language="zh",
        fps=29.97, max_chars=25, segmentation_profile=None, hotwords_json=None,
        diagnostic_output=str(tmp_path / "last_generate_diagnostic.json"),
        output=str(tmp_path / "result.json"), fixture_json=None,
    )


def test_history_is_unique_matches_writeback_and_has_no_content_or_secrets(helper, tmp_path, monkeypatch):
    monkeypatch.setenv("SUBFIX_DOUBAO_API_KEY", "secret-never-record")
    rows = [{"start_frame": 100, "end_frame": 150, "text": "私密字幕全文"}]
    traces = []
    for _ in range(2):
        args = make_args(tmp_path)
        args.generation_trace = helper.start_generation_trace(args)
        helper.record_generation_asr(args, {
            "backend": "doubao_asr", "model": args.model,
            "audio": "/private/client.wav", "text": "私密字幕全文",
            "diagnostic": {"resource_id": "volc.bigasr.auc_turbo", "token": "secret-never-record"},
        })
        payload = {"ok": True, "subtitle_rows": rows, "diagnostic": {"generate_engine": "v4"}}
        helper.finish_generation_trace(args, payload)
        trace = payload["generation_trace"]
        uuid.UUID(trace["run_id"])
        stored = json.loads((tmp_path / "generation_history" / (trace["run_id"] + ".json")).read_text(encoding="utf-8"))
        assert stored == trace
        assert helper.build_generate_writeback_payload(payload)["generation_trace"] == trace
        assert helper.sanitize_generate_diagnostic_payload(payload)["generation_trace"] == trace
        assert trace["subtitle_count"] == 1
        assert trace["result_frame_range"] == {"start_frame": 100, "end_frame": 150}
        assert trace["generate_engine"] == "v4"
        assert trace["parameters"]["requested_generate_engine"] == "v5"
        assert trace["asr_sources"][0]["model"] == "bigmodel"
        assert trace["asr_sources"][0]["model_revision"] is None
        assert trace["asr_sources"][0]["resource_id"] == "volc.bigasr.auc_turbo"
        serialized = json.dumps(stored, ensure_ascii=False)
        for private in ("secret-never-record", "私密字幕全文", "/private/client.wav"):
            assert private not in serialized
        traces.append(trace)
    assert traces[0]["run_id"] != traces[1]["run_id"]
    assert traces[0]["subtitle_result_sha256"] == traces[1]["subtitle_result_sha256"]
    assert len(list((tmp_path / "generation_history").glob("*.json"))) == 2


def test_history_is_capped_and_keeps_newest(helper, tmp_path):
    helper.GENERATION_HISTORY_MAX_FILES = 3
    run_ids = []
    for _ in range(6):
        args = make_args(tmp_path)
        args.generation_trace = helper.start_generation_trace(args)
        helper.finish_generation_trace(args, {"ok": True, "subtitle_rows": []})
        run_ids.append(args.generation_trace["run_id"])
    surviving = {item.stem for item in (tmp_path / "generation_history").glob("*.json")}
    assert len(surviving) == 3
    assert surviving == set(run_ids[-3:])


def test_fingerprint_uses_explicit_running_plugin_and_changes_with_code(helper, tmp_path):
    plugin = tmp_path / "SubFix.lua"
    core = tmp_path / "core.lua"
    profile = tmp_path / "profile.json"
    plugin.write_text('SUBFIX_VERSION = "3.8.1"\n', encoding="utf-8")
    core.write_text("return {}", encoding="utf-8")
    profile.write_text('{"schema_version":"test"}', encoding="utf-8")
    args = make_args(tmp_path)
    args.plugin_source, args.generation_core_source = str(plugin), str(core)
    args.segmentation_profile = str(profile)
    before = helper.start_generation_trace(args)
    assert before["plugin_version"] == "3.8.1"
    assert before["code_fingerprints"]["generation_core"]["status"] == "available"
    assert before["profile_fingerprint"]["status"] == "available"
    plugin.write_text('SUBFIX_VERSION = "3.8.1"\n-- local fix', encoding="utf-8")
    after = helper.start_generation_trace(args)
    assert before["code_fingerprints"]["plugin"] != after["code_fingerprints"]["plugin"]
    assert str(tmp_path) not in json.dumps(after)


def test_unreadable_fingerprint_is_nonfatal(helper, tmp_path, monkeypatch):
    def denied(self, *a, **k):
        raise PermissionError("private location")

    monkeypatch.setattr(Path, "read_bytes", denied)
    trace = helper.start_generation_trace(make_args(tmp_path))
    assert trace["code_fingerprints"]["helper"] == {"status": "unavailable", "error_type": "PermissionError"}
    assert "private location" not in json.dumps(trace)


def test_history_failure_does_not_destroy_result(helper, tmp_path):
    (tmp_path / "generation_history").write_text("occupied by a file", encoding="utf-8")
    args = make_args(tmp_path)
    args.generation_trace = helper.start_generation_trace(args)
    payload = {"ok": True, "subtitle_rows": [{"start_frame": 1, "end_frame": 20, "text": "正常字幕"}]}
    helper.finish_generation_trace(args, payload)
    result = helper.build_generate_writeback_payload(payload)
    assert result["ok"] is True
    assert result["subtitle_rows"] == payload["subtitle_rows"]
    assert result["generation_trace"]["history_status"] == "failed"
    assert result["generation_trace"]["history_error_type"] == "FileExistsError"


def test_multiple_actual_models_and_retry_roles_are_preserved(helper, tmp_path):
    args = make_args(tmp_path)
    args.generation_trace = helper.start_generation_trace(args)
    primary = {"backend": "doubao_asr_v2", "model": "wrong-requested-model"}
    helper.record_generation_asr(args, primary)
    helper.record_generation_asr(args, primary)
    helper.record_generation_asr(args, {"backend": "qwen3_asr", "model": "/models/Qwen3-ASR-1.7B"}, "retry_candidate")
    sources = args.generation_trace["asr_sources"]
    assert len(sources) == 2
    assert sources[1] == {"backend": "qwen3_asr", "model": "Qwen3-ASR-1.7B", "role": "retry_candidate", "model_identity_source": "backend_result"}


def test_doubao_plural_resource_ids_still_fingerprints(helper, tmp_path):
    # 聚合诊断把 resource_id 去重成 doubao_resource_ids 列表，指纹不能因此变空。
    args = make_args(tmp_path)
    args.generation_trace = helper.start_generation_trace(args)
    helper.record_generation_asr(args, {"backend": "doubao_asr", "diagnostic": {"doubao_resource_ids": ["volc.bigasr.auc_turbo"]}})
    assert args.generation_trace["asr_sources"][0]["resource_id"] == "volc.bigasr.auc_turbo"


@pytest.mark.parametrize("fail", [False, True])
def test_main_generates_trace_for_success_and_failure(helper, tmp_path, monkeypatch, fail):
    monkeypatch.setattr(helper, "load_generate_subtitles_batch_plan", lambda _: [])

    def generate(batches, args, progress_path, fixture_payload):
        helper.record_generation_asr(args, {"backend": "qwen3_asr", "model": "Qwen3-ASR-1.7B"})
        if fail:
            raise RuntimeError("simulated failure")
        return {"ok": True, "subtitle_rows": [{"start_frame": 0, "end_frame": 30, "text": "测试字幕"}], "diagnostic": {"generate_engine": "v5"}}

    monkeypatch.setattr(helper, "run_generate_subtitles_batch_plan", generate)
    output = tmp_path / "output.json"
    report = tmp_path / "diagnostic.json"
    status = helper.main(["--mode", "generate_subtitles_batch", "--generate-engine", "v5", "--output", str(output), "--diagnostic-output", str(report)])
    assert status == int(fail)
    payload = json.loads(output.read_text(encoding="utf-8"))
    trace = payload["generation_trace"]
    assert trace["ok"] is not fail
    assert json.loads(report.read_text(encoding="utf-8"))["generation_trace"] == trace
    assert trace["asr_sources"][0]["model"] == "Qwen3-ASR-1.7B"
    assert len(list((tmp_path / "generation_history").glob("*.json"))) == 1


def test_generation_core_passes_actual_sources_and_displays_history_location():
    source = (ROOT / ".subfix_support" / "subfix_generate_selection_core.lua").read_text(encoding="utf-8")
    assert '"--plugin-source", shell_quote(paths.plugin_source)' in source
    assert '"--generation-core-source", shell_quote(paths.generation_core_source)' in source
    assert "file_exists(paths.plugin_source)" in source
    # 装机布局是 Utility\SubFix\SubFix.lua，root 回退成 Utility 时必须走这条兜底
    assert 'file_exists(root .. "/SubFix/SubFix.lua")' in source
    assert 'return rows, nil, payload.generation_trace' in source
    assert 'generation_trace.history_status == "saved"' in source
    assert 'resolve_asr_paths().generation_history .. "/" .. run_id .. ".json"' in source
    assert 'generation_history = user_support_dir .. "/generation_history"' in source


def test_history_and_diagnostic_permission_errors_preserve_success(helper, tmp_path, monkeypatch):
    original_write = helper.write_payload

    def guarded_write(path, payload):
        if path.name == "diagnostic.json" or path.parent.name == "generation_history":
            raise PermissionError("private path must not appear in metadata")
        return original_write(path, payload)

    monkeypatch.setattr(helper, "write_payload", guarded_write)
    monkeypatch.setattr(helper, "load_generate_subtitles_batch_plan", lambda _: [])
    monkeypatch.setattr(helper, "run_generate_subtitles_batch_plan", lambda *a, **k: {
        "ok": True, "subtitle_rows": [{"start_frame": 0, "end_frame": 30, "text": "保留结果"}],
        "diagnostic": {"generate_engine": "v5"},
    })
    output = tmp_path / "output.json"
    status = helper.main(["--mode", "generate_subtitles_batch", "--generate-engine", "v5", "--output", str(output), "--diagnostic-output", str(tmp_path / "diagnostic.json")])
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert status == 0 and payload["ok"] is True
    assert payload["subtitle_rows"][0]["text"] == "保留结果"
    assert payload["generation_trace"]["history_error_type"] == "PermissionError"
    assert payload["generation_trace"]["diagnostic_error_type"] == "PermissionError"
    assert "private path" not in json.dumps(payload["generation_trace"])


def test_single_generation_v5_keeps_trace(helper, tmp_path, monkeypatch):
    def generate(batches, args, progress_path, fixture_payload):
        assert len(batches) == 1 and batches[0]["audio"] == "synthetic.wav"
        helper.record_generation_asr(args, {"backend": "qwen3_asr", "model": "test-model"})
        return {"ok": True, "subtitle_rows": [{"start_frame": 0, "end_frame": 30, "text": "字幕测试"}],
                "diagnostic": {"generate_engine": "v5"}}

    monkeypatch.setattr(helper, "run_generate_subtitles_batch_plan", generate)
    output = tmp_path / "output.json"
    status = helper.main(["--mode", "generate_subtitles", "--generate-engine", "v5",
                          "--audio", "synthetic.wav", "--output", str(output)])
    assert status == 0
    trace = json.loads(output.read_text(encoding="utf-8"))["generation_trace"]
    assert trace["parameters"]["requested_generate_engine"] == "v5"
    assert trace["generate_engine"] == "v5"
    assert trace["asr_sources"][0]["backend"] == "qwen3_asr"
    assert trace["history_status"] == "saved"


@pytest.mark.parametrize("source", ["environment", "default"])
def test_profile_trace_records_actual_implicit_file(helper, tmp_path, monkeypatch, source):
    monkeypatch.delenv(helper.SEGMENTATION_PROFILE_ENV, raising=False)
    profile = tmp_path / "segmentation_profile.json"
    profile.write_text(json.dumps({"schema_version": helper.SEGMENTATION_PROFILE_SCHEMA, "modes": {}}), encoding="utf-8")
    monkeypatch.setattr(helper, "__file__", str(tmp_path / "subfix_asr_transcribe.py"))
    if source == "environment":
        monkeypatch.setenv(helper.SEGMENTATION_PROFILE_ENV, str(profile))
    args = make_args(tmp_path)
    args.generate_engine = "v3"
    args.generation_trace = helper.start_generation_trace(args)
    loaded, status = helper.load_generation_profile(args, "v3")
    assert loaded is not None and status == "loaded"
    payload = {"ok": True, "subtitle_rows": []}
    helper.finish_generation_trace(args, payload)
    trace = payload["generation_trace"]
    assert trace["profile_load_status"] == "loaded"
    assert trace["profile_loads"][0]["source"] == source
    assert trace["profile_fingerprint"] == helper.generation_file_fingerprint(profile)
    assert str(tmp_path) not in json.dumps(trace)
