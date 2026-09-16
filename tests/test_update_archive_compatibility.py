import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_update_archives.py"
VERSION = "3.4.0"
LEGACY_FILES = {
    "SubFix/SubFix.lua", "SubFix/生成选区字幕.lua",
    ".subfix_support/subfix_generate_selection_core.lua",
    ".subfix_support/subfix_update.py", ".subfix_support/subfix_asr_transcribe.py",
    ".subfix_support/subfix_generate_v4.py", ".subfix_support/subfix_generate_v5.py",
    ".subfix_support/subfix_generate_textnorm.py", ".subfix_support/subfix_qwen_local_manager.py",
    ".subfix_support/setup_asr_env.sh", ".subfix_support/segmentation_profile.json",
    ".subfix_support/segmentation_profile_v3.json", ".subfix_support/segmentation_profile_v4.json",
}
FULL_ONLY_FILES = {
    ".subfix_support/subfix_process_group.py",
    ".subfix_support/bin/ffmpeg", ".subfix_support/licenses/FFmpeg-LGPL-2.1.txt",
}


@pytest.fixture
def payload(tmp_path):
    root = tmp_path / "payload"
    for name in LEGACY_FILES | FULL_ONLY_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'SUBFIX_VERSION = "{VERSION}"\n' if name == "SubFix/SubFix.lua" else name)
    return root


def build(payload, output):
    assert BUILDER.is_file(), "missing legacy-compatible update archive builder"
    return subprocess.run([sys.executable, str(BUILDER), str(payload), VERSION, str(output)],
                          capture_output=True, text=True)


def test_builder_writes_legacy_and_full_archives_with_portable_checksums(payload, tmp_path):
    output = tmp_path / "output"
    result = build(payload, output)
    assert result.returncode == 0, result.stderr
    for suffix, expected in (("", LEGACY_FILES), ("-full", LEGACY_FILES | FULL_ONLY_FILES)):
        path = output / f"SubFix-update-v{VERSION}{suffix}.zip"
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("subfix-update-manifest.json"))
            assert set(manifest["files"]) == expected
            assert manifest["version"] == VERSION
            assert set(archive.namelist()) == expected | {"subfix-update-manifest.json"}
        assert path.with_suffix(".sha256").read_text().strip() == (
            hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name
        )


@pytest.mark.parametrize("bad_path", [".DS_Store", ".subfix_support/unplanned_helper.py",
                                      ".subfix_support/doubao_credentials.json"])
def test_builder_rejects_unplanned_files_before_writing_archives(payload, tmp_path, bad_path):
    (payload / bad_path).write_text("must not ship")
    output = tmp_path / "output"
    result = build(payload, output)
    assert result.returncode != 0
    assert bad_path in result.stderr
    assert not list(output.glob("*.zip"))


def test_builder_rejects_symlink_payload(payload, tmp_path):
    path = payload / "SubFix/SubFix.lua"
    path.unlink()
    path.symlink_to(ROOT / "SubFix.lua")
    result = build(payload, tmp_path / "output")
    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()


def test_builder_rejects_version_mismatch(payload, tmp_path):
    (payload / "SubFix/SubFix.lua").write_text('SUBFIX_VERSION = "0.0.0"\n')
    result = build(payload, tmp_path / "output")
    assert result.returncode != 0
    assert "version" in result.stderr.lower()


@pytest.mark.parametrize("tag", ["v3.1.0", "v3.1.4", "v3.1.5", "v3.2.0", "v3.2.1", "v3.3.1"])
def test_historical_updater_installs_legacy_archive(payload, tmp_path, tag):
    output = tmp_path / "output"
    result = build(payload, output)
    assert result.returncode == 0, result.stderr
    historical = subprocess.run(["git", "show", f"{tag}:.subfix_support/subfix_update.py"],
                                cwd=ROOT, capture_output=True, text=True)
    if historical.returncode:
        pytest.skip(f"historical tag {tag} unavailable; frozen legacy allowlist is still tested")
    namespace = {"__name__": "historical_updater"}
    exec(compile(historical.stdout, f"{tag}/subfix_update.py", "exec"), namespace)
    archive = output / f"SubFix-update-v{VERSION}.zip"
    utility = tmp_path / "Utility"
    credential = utility / ".subfix_support/doubao_credentials.json"
    credential.parent.mkdir(parents=True)
    credential.write_text("unchanged user configuration")
    namespace["install_archive"](archive, hashlib.sha256(archive.read_bytes()).hexdigest(), VERSION, utility)
    assert credential.read_text() == "unchanged user configuration"
    assert (utility / "SubFix/SubFix.lua").read_bytes() == (payload / "SubFix/SubFix.lua").read_bytes()
    assert not (utility / ".subfix_support/subfix_process_group.py").exists()
