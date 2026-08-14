"""配置加载 + 数据类"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .utils import resolve_env

VALID_RELEASE_TYPES = ("latest", "prerelease")


@dataclass
class DownloadConfig:
    max_concurrent: int = 3
    speed_threshold_kbps: int = 1024
    slow_timeout_sec: int = 10


@dataclass
class NotifyChannel:
    type: str
    token: str = ""
    key: str = ""
    bot_token: str = ""
    chat_id: str = ""
    topic: str = ""


@dataclass
class NotifyConfig:
    channels: List[NotifyChannel] = field(default_factory=list)
    min_interval_sec: int = 12
    translate_notes: bool = True


@dataclass
class TranslationConfig:
    app_id: str = ""
    secret_key: str = ""


@dataclass
class RepoConfig:
    repo: str
    files: List[dict] = field(default_factory=list)
    release_type: str = "latest"
    tag_regex: Optional[str] = None
    include_regex: Optional[str] = None
    exclude_regex: Optional[str] = None
    rename_with_repo: bool = False
    rename_apk1: bool = False
    keep_versions: int = 1


@dataclass
class AppConfig:
    github_token: str = ""
    download_path: Path = field(default_factory=lambda: Path.home() / "Downloads")
    apk_rename_output: Optional[Path] = None
    mirrors: List[str] = field(default_factory=list)
    dry_run: bool = False
    download: DownloadConfig = field(default_factory=DownloadConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    logging: dict = field(default_factory=lambda: {"level": "INFO", "keep_backup_days": 14})
    repos: List[RepoConfig] = field(default_factory=list)


def _parse_channel(raw: dict) -> NotifyChannel:
    return NotifyChannel(
        type=raw.get("type", ""),
        token=raw.get("token", ""),
        key=raw.get("key", ""),
        bot_token=raw.get("bot_token", ""),
        chat_id=raw.get("chat_id", ""),
        topic=raw.get("topic", ""),
    )


def load_config(path: Path) -> AppConfig:
    """加载并校验配置文件"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    raw = resolve_env(raw)

    dl_raw = raw.get("download", {})
    notif_raw = raw.get("notify", {})
    trans_raw = raw.get("baidu_translate", {})
    log_raw = raw.get("logging", {})
    if not log_raw:
        log_raw = {"level": "INFO", "keep_backup_days": 14}

    repos = []
    for r in raw.get("repos", []):
        release_type = r.get("release_type", "latest")
        if release_type not in VALID_RELEASE_TYPES:
            print(f"[配置] release_type '{release_type}' 无效，使用 latest")
            release_type = "latest"
        keep = r.get("keep_versions", 1)
        if type(keep) is not int or keep < 0:
            print(f"[配置] keep_versions '{keep}' 无效，使用 1")
            keep = 1
        repos.append(RepoConfig(
            repo=r.get("repo", ""),
            files=r.get("files", []),
            release_type=release_type,
            tag_regex=r.get("tag_regex"),
            include_regex=r.get("include_regex"),
            exclude_regex=r.get("exclude_regex"),
            rename_with_repo=r.get("rename_with_repo", False),
            rename_apk1=r.get("rename_apk1", False),
            keep_versions=keep,
        ))

    apk_output = Path(raw["apk_rename_output"]) if raw.get("apk_rename_output") else None
    download_path = Path(raw.get("download_path", str(Path.home() / "Downloads")))
    if apk_output:
        if apk_output.exists() and not apk_output.is_dir():
            print(f"[配置] apk_rename_output '{apk_output}' 是文件而非目录，已禁用 APK1 同步")
            apk_output = None
        elif apk_output.resolve() == download_path.resolve():
            print("[配置] apk_rename_output 与 download_path 相同，已禁用 APK1 同步")
            apk_output = None

    return AppConfig(
        github_token=raw.get("github_token", ""),
        download_path=download_path,
        apk_rename_output=apk_output,
        mirrors=raw.get("mirrors", []),
        dry_run=raw.get("dry_run", False),
        download=DownloadConfig(
            max_concurrent=dl_raw.get("max_concurrent", 3),
            speed_threshold_kbps=dl_raw.get("speed_threshold_kbps", 1024),
            slow_timeout_sec=dl_raw.get("slow_timeout_sec", 10),
        ),
        notify=NotifyConfig(
            channels=[_parse_channel(c) for c in notif_raw.get("channels", [])],
            min_interval_sec=notif_raw.get("min_interval_sec", 12),
            translate_notes=notif_raw.get("translate_notes", True),
        ),
        translation=TranslationConfig(
            app_id=trans_raw.get("appid", ""),
            secret_key=trans_raw.get("secret", ""),
        ),
        logging=log_raw,
        repos=repos,
    )
