import pytest

from src.utils import (
    clean_notes, fmt_size, is_security_release, load_dotenv,
    needs_translation, resolve_env, sha256sum,
)


def test_fmt_size():
    assert fmt_size(1024 * 1024) == "1.0 MB"
    assert fmt_size(2 * 1024 * 1024 * 1024) == "2.0 GB"


def test_sha256sum(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello")
    assert sha256sum(p) == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


@pytest.mark.parametrize("text,expected", [
    ("", False),
    ("这是一个中文说明", False),
    ("Some English release notes", True),
    ("日本語のリリースノート", True),
    ("한국어 릴리즈 노트", True),
    ("中英混合 update 说明", False),
])
def test_needs_translation(text, expected):
    assert needs_translation(text) is expected


def test_clean_notes():
    raw = "<!-- comment -->\n\n## Changelog\n\n![img](http://x)\n\n\n\n[ref]: http://x\n\nBody"
    out = clean_notes(raw)
    assert "comment" not in out
    assert "![img]" not in out
    assert "\n\n\n" not in out
    assert out.startswith("## Changelog")
    assert "Body" in out


def test_is_security_release():
    assert is_security_release("v1.0", "Fixes CVE-2024-1234", "")
    assert is_security_release("v2.0", "", "Security Advisory Release")
    assert not is_security_release("v1.0", "new features", "")


def test_resolve_env(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "abc")
    assert resolve_env("${GH_TOKEN}") == "abc"
    assert resolve_env({"a": "${GH_TOKEN}", "b": [1, "${NOPE}"]}) == {"a": "abc", "b": [1, "${NOPE}"]}


def test_load_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    (tmp_path / ".env").write_text("# comment\nMY_KEY=value123\nexport OTHER=abc\n")
    load_dotenv(tmp_path / ".env")
    import os
    assert os.environ["MY_KEY"] == "value123"
    assert os.environ["OTHER"] == "abc"
