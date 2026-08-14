"""通用工具函数"""

import hashlib
import os
import re
from pathlib import Path
from typing import Any


def fmt_size(b: int) -> str:
    """格式化文件大小"""
    mb = b / (1024 * 1024)
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def sha256sum(path: Path) -> str:
    """计算文件 SHA256"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def needs_translation(text: str) -> bool:
    """判断发布说明是否需要翻译成中文：
    含日文假名/韩文谚文（明确非中文特征）或纯拉丁文本 → 需要翻译；
    含汉字且无假名/谚文 → 视为中文，跳过"""
    if not text:
        return False
    if re.search(r"[\u3040-\u30ff\uac00-\ud7af]", text):
        return True
    if re.search(r"[\u4e00-\u9fff]", text):
        return False
    return True


def resolve_env(obj: Any) -> Any:
    """递归解析所有字符串中的 ${VAR} 占位符"""
    if isinstance(obj, str):
        return re.sub(
            r"\$\{([^}]+)\}",
            lambda m: os.environ.get(m.group(1), m.group(0)),
            obj,
        )
    if isinstance(obj, dict):
        return {k: resolve_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_env(v) for v in obj]
    return obj


def clean_notes(text: str) -> str:
    """清理 release notes 噪音：HTML 注释/图片引用/折叠块/多余空行"""
    if not text:
        return ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"^\[.+?\]:\s+\S+.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"<details>.*?</details>", "", text, flags=re.DOTALL)
    text = re.sub(r"<summary>.*?</summary>", "", text, flags=re.DOTALL)
    text = re.sub(r"!\[.*?\]\[.*?\]", "", text)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# 安全版本检测模式
_SECURITY_PATTERNS = [
    r"\bCVE-\d{4}-\d{4,}\b",
    r"\bGHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}\b",
    r"\bsecurity\b",
    r"\bsecurity\s+(?:advisory|fix|patch|release|update)\b",
    r"\bvulnerabilit(?:y|ies)\b",
]


def is_security_release(tag_name: str, body: str = "", name: str = "") -> bool:
    """检测是否为安全更新版本"""
    text = f"{tag_name}\n{name}\n{body}"
    return any(re.search(p, text, re.IGNORECASE) for p in _SECURITY_PATTERNS)


def load_dotenv(path: Path) -> None:
    """加载 .env 文件到环境变量（支持 export 前缀与引号）"""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            if key:
                os.environ[key] = val
