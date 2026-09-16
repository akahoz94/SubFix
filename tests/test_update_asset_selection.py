import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_full_update_requires_matching_zip_and_checksum():
    spec = importlib.util.spec_from_file_location("release_updater", ROOT / ".subfix_support/subfix_update.py")
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    prefix = "SubFix-update-v3.3.2"
    def asset(suffix):
        return {"name": prefix + suffix, "browser_download_url": "https://example.invalid/" + prefix + suffix}
    release = {"tag_name": "v3.3.2", "assets": [asset(".zip"), asset(".sha256")]}
    assert updater.release_to_update_info(release, "3.3.1")["zip_url"].endswith(prefix + ".zip")
    release["assets"].append(asset("-full.zip"))
    assert updater.release_to_update_info(release, "3.3.1")["zip_url"].endswith(prefix + ".zip")
    release["assets"].append(asset("-full.sha256"))
    info = updater.release_to_update_info(release, "3.3.1")
    assert info["zip_url"].endswith(prefix + "-full.zip")
    assert info["sha256_url"].endswith(prefix + "-full.sha256")
    assert not updater.release_to_update_info(release, "3.3.2")["ok"]
