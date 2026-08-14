"""版本追踪持久化 + 版本清理（keep_versions）"""

import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class VersionTracker:
    def __init__(self, path: Path):
        self.path = path
        self._data: Dict[str, dict] = {}
        self._lock = threading.Lock()
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.path)

    def get(self, key: str) -> Optional[dict]:
        return self._data.get(key)

    def set(
        self, key: str, version: str, *,
        etag: Optional[str] = None, clear_files: bool = False,
    ) -> None:
        with self._lock:
            if key not in self._data:
                self._data[key] = {"version": version, "files": {}, "first_seen": False}
            else:
                self._data[key]["version"] = version
                if clear_files:
                    self._data[key]["files"] = {}
            if etag is not None:
                self._data[key]["etag"] = etag
            self._data[key]["checked_at"] = datetime.now().isoformat()

    def update_file(self, key: str, name: str, ok: bool) -> None:
        with self._lock:
            if key in self._data:
                self._data[key].setdefault("files", {})[name] = ok

    def is_complete(self, key: str, names: List[str]) -> bool:
        rec = self._data.get(key)
        if not rec:
            return False
        return all(rec.get("files", {}).get(n, False) for n in names)

    def is_first_seen(self, key: str) -> bool:
        """检查是否为首次发现（未标记过 first_seen=True）"""
        rec = self._data.get(key)
        if not rec:
            return True
        return not rec.get("first_seen", False)

    def mark_seen(self, key: str) -> None:
        """标记为已发现（首次发现后调用）"""
        with self._lock:
            if key in self._data:
                self._data[key]["first_seen"] = True

    def get_etag(self, key: str) -> Optional[str]:
        rec = self._data.get(key)
        return rec.get("etag") if rec else None

    def set_apk1(self, key: str, version: str, names: List[str]) -> None:
        """按版本记录某仓库已复制改名为 .apk1 的文件名列表"""
        with self._lock:
            if key not in self._data:
                self._data[key] = {"version": "", "files": {}, "first_seen": False}
            rec = self._data[key]
            rec.setdefault("renamed_apk1", {})[version] = names

    def get_apk1(self, key: str) -> Dict[str, List[str]]:
        """获取某仓库按版本记录的 .apk1 文件名映射"""
        rec = self._data.get(key)
        return rec.get("renamed_apk1", {}) if rec else {}

    def prune_apk1(self, key: str, keep_versions: int) -> None:
        """修剪映射：只保留最近 keep_versions 个版本的记录（0=不修剪）"""
        if keep_versions <= 0:
            return
        with self._lock:
            rec = self._data.get(key)
            if not rec or "renamed_apk1" not in rec:
                return
            mapping = rec["renamed_apk1"]
            ordered = sorted(mapping.keys(), key=_version_key)
            excess = ordered[:max(0, len(ordered) - keep_versions)]
            for v in excess:
                del mapping[v]


def target_name(asset_name: str, version: str, repo_name: str, rename_with_repo: bool) -> str:
    """文件名规范化为 {项目名}-{原文件名}-{版本号}：
    app-release.apk + v1.2.19 → projectname-app-release-1.2.19.apk
    防御：asset_name 只取纯文件名（basename），杜绝路径分隔符/.. 穿越下载目录"""
    safe_name = Path(asset_name).name
    if safe_name in ("", ".", ".."):
        safe_name = f"asset-{version}" if version else "asset"
    if not rename_with_repo or not version:
        return safe_name
    p = Path(safe_name)
    ver = version[1:] if version[:1].lower() == "v" else version
    return f"{repo_name}-{p.stem}-{ver}{p.suffix}"


def _version_key(name: str):
    """自然排序键：数字段按数值比较，保证 v1.10 排在 v1.2.9 之后"""
    parts = []
    for piece in re.split(r"(\d+)", name):
        if piece.isdigit():
            parts.append((0, int(piece)))
        else:
            parts.append((1, piece))
    return tuple(parts)


def cleanup_flat_dir(repo_dir: Path, keep_names: List[str]) -> None:
    """keep_versions=1 平铺模式：删除目录内不属于本次 target_names 的文件
    （.apk1 除外，由 cleanup_apk1 独立管理）"""
    if not repo_dir.exists():
        return
    keep = set(keep_names)
    for f in repo_dir.iterdir():
        if not f.is_file():
            continue
        if f.name in keep or f.name.endswith(".apk1"):
            continue
        f.unlink(missing_ok=True)


def cleanup_version_dirs(repo_dir: Path, keep_versions: int) -> None:
    """keep_versions≥2 子目录模式：保留最近 N 个版本目录，删除更旧目录。
    keep_versions=0 表示不清理"""
    if keep_versions == 0 or not repo_dir.exists():
        return
    dirs = [d for d in repo_dir.iterdir() if d.is_dir()]
    if len(dirs) <= keep_versions:
        return
    dirs.sort(key=lambda d: _version_key(d.name))
    for d in dirs[:len(dirs) - keep_versions]:
        shutil.rmtree(d, ignore_errors=True)


def cleanup_apk1(
    apk_output: Path, apk1_by_version: Dict[str, List[str]], keep_versions: int,
    current_names: Optional[List[str]] = None,
) -> None:
    """自定义 apk_rename_output 路径：apk1 平铺留存数量与 keep_versions 一致。
    1=只留最新版本，N=保留最近 N 个版本，0=不清理。
    current_names 为本次刚复制成功的文件名（未开启 rename 时新旧版本同名），
    属于本次版本的文件绝不能被当作旧版本清理删除"""
    if keep_versions == 0 or not apk1_by_version:
        return
    current = set(current_names or [])
    ordered = sorted(apk1_by_version.keys(), key=_version_key)
    excess = ordered[:len(ordered) - keep_versions]
    for ver in excess:
        for name in apk1_by_version.get(ver, []):
            if name in current:
                continue
            (apk_output / name).unlink(missing_ok=True)