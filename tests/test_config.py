import json
from pathlib import Path

from src.config import load_config


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_basic_parse(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_test")
    p = _write(tmp_path, {
        "github_token": "${GH_TOKEN}",
        "download_path": str(tmp_path / "dl"),
        "mirrors": ["https://ghfast.top"],
        "repos": [{"repo": "a/b", "files": [{"keywords": ["app"], "extensions": [".apk"]}]}],
    })
    cfg = load_config(p)
    assert cfg.github_token == "ghp_test"
    assert cfg.download_path == tmp_path / "dl"
    assert cfg.repos[0].repo == "a/b"
    assert cfg.repos[0].release_type == "latest"
    assert cfg.repos[0].keep_versions == 1
    assert cfg.download.speed_threshold_kbps == 1024


def test_defaults(tmp_path):
    p = _write(tmp_path, {"repos": [{"repo": "a/b"}]})
    cfg = load_config(p)
    assert cfg.dry_run is False
    assert cfg.download.max_concurrent == 3
    assert cfg.logging == {"level": "INFO", "keep_backup_days": 14}
    assert cfg.notify.min_interval_sec == 12
    assert cfg.notify.translate_notes is True


def test_invalid_release_type_fallback(tmp_path):
    p = _write(tmp_path, {"repos": [{"repo": "a/b", "release_type": "bogus"}]})
    cfg = load_config(p)
    assert cfg.repos[0].release_type == "latest"


def test_negative_keep_versions_fallback(tmp_path):
    p = _write(tmp_path, {"repos": [{"repo": "a/b", "keep_versions": -3}]})
    cfg = load_config(p)
    assert cfg.repos[0].keep_versions == 1


def test_tag_regex_parse(tmp_path):
    p = _write(tmp_path, {"repos": [
        {"repo": "a/b", "tag_regex": "desktop"},
        {"repo": "c/d"},
    ]})
    cfg = load_config(p)
    assert cfg.repos[0].tag_regex == "desktop"
    assert cfg.repos[1].tag_regex is None


def test_channels_parse(tmp_path):
    p = _write(tmp_path, {"notify": {"channels": [
        {"type": "pushplus", "token": "t", "topic": "grp"},
        {"type": "serverchan", "key": "k"},
        {"type": "telegram", "bot_token": "b", "chat_id": "c"},
    ]}})
    cfg = load_config(p)
    assert [c.type for c in cfg.notify.channels] == ["pushplus", "serverchan", "telegram"]
    assert cfg.notify.channels[0].topic == "grp"


def test_apk_output_disabled_when_file(tmp_path):
    f = tmp_path / "notadir"
    f.write_text("x")
    p = _write(tmp_path, {"apk_rename_output": str(f), "repos": []})
    assert load_config(p).apk_rename_output is None


def test_apk_output_disabled_when_same_as_download(tmp_path):
    p = _write(tmp_path, {
        "download_path": str(tmp_path),
        "apk_rename_output": str(tmp_path),
        "repos": [],
    })
    assert load_config(p).apk_rename_output is None


def test_apk_output_ok(tmp_path):
    p = _write(tmp_path, {
        "download_path": str(tmp_path / "dl"),
        "apk_rename_output": str(tmp_path / "tv"),
        "repos": [],
    })
    assert load_config(p).apk_rename_output == tmp_path / "tv"
