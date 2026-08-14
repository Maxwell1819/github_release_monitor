from src.storage import (
    VersionTracker, cleanup_apk1, cleanup_flat_dir, cleanup_version_dirs, target_name,
)


def _verdir(tmp_path, ver):
    d = tmp_path / ver
    d.mkdir(parents=True)
    (d / f"{ver}.apk").write_bytes(b"x")
    (d / "RELEASE_NOTES.md").write_text("notes")
    return d


def test_target_name():
    assert target_name("app-release.apk", "v1.2.19", "projectname", True) == "projectname-app-release-1.2.19.apk"
    assert target_name("app-release.apk", "1.2.19", "projectname", True) == "projectname-app-release-1.2.19.apk"
    assert target_name("app-release.apk", "v1.2", "projectname", False) == "app-release.apk"


def test_tracker_roundtrip(tmp_path):
    p = tmp_path / "vr.json"
    t = VersionTracker(p)
    t.set("a_b", "v1", etag='"e"')
    t.update_file("a_b", "x.apk", True)
    t.mark_seen("a_b")
    t.save()

    t2 = VersionTracker(p)
    assert t2.get("a_b")["version"] == "v1"
    assert t2.is_complete("a_b", ["x.apk"])
    assert not t2.is_first_seen("a_b")
    assert t2.get_etag("a_b") == '"e"'


def test_tracker_apk1_by_version(tmp_path):
    p = tmp_path / "vr.json"
    t = VersionTracker(p)
    t.set("a_b", "v2")
    t.set_apk1("a_b", "v2", ["app-v2.apk1"])
    t.set_apk1("a_b", "v1", ["app-v1.apk1"])
    assert t.get_apk1("a_b") == {"v1": ["app-v1.apk1"], "v2": ["app-v2.apk1"]}


def test_cleanup_flat_dir(tmp_path):
    repo_dir = tmp_path / "a_b"
    repo_dir.mkdir()
    (repo_dir / "keep.apk").write_bytes(b"x")
    (repo_dir / "old.apk").write_bytes(b"x")
    (repo_dir / "RELEASE_NOTES_a_b_v2.md").write_text("n")
    (repo_dir / "RELEASE_NOTES_a_b_v1.md").write_text("n")
    cleanup_flat_dir(repo_dir, ["keep.apk", "RELEASE_NOTES_a_b_v2.md"])
    assert (repo_dir / "keep.apk").exists()
    assert not (repo_dir / "old.apk").exists()
    assert not (repo_dir / "RELEASE_NOTES_a_b_v1.md").exists()


def test_cleanup_flat_dir_non_apk_extensions(tmp_path):
    """keep_versions=1 平铺模式：非 .apk 后缀（.exe/.zip/.msixbundle 等）旧版本同样清理"""
    repo_dir = tmp_path / "a_b"
    repo_dir.mkdir()
    (repo_dir / "Sunshine.v2026.0806.WindowsInstaller.exe").write_bytes(b"old")
    (repo_dir / "Sunshine.v2026.0807.WindowsInstaller.exe").write_bytes(b"new")
    (repo_dir / "old-version.zip").write_bytes(b"old")
    (repo_dir / "RELEASE_NOTES_a_b_v2.md").write_text("n")
    cleanup_flat_dir(repo_dir, ["Sunshine.v2026.0807.WindowsInstaller.exe", "RELEASE_NOTES_a_b_v2.md"])
    assert (repo_dir / "Sunshine.v2026.0807.WindowsInstaller.exe").exists()
    assert not (repo_dir / "Sunshine.v2026.0806.WindowsInstaller.exe").exists()
    assert not (repo_dir / "old-version.zip").exists()
    assert (repo_dir / "RELEASE_NOTES_a_b_v2.md").exists()


def test_cleanup_flat_dir_keeps_apk1(tmp_path):
    """.apk1 由 cleanup_apk1 独立管理，平铺清理不得误删"""
    repo_dir = tmp_path / "a_b"
    repo_dir.mkdir()
    (repo_dir / "keep.apk").write_bytes(b"x")
    (repo_dir / "keep.apk1").write_bytes(b"x")
    (repo_dir / "old.apk1").write_bytes(b"x")
    cleanup_flat_dir(repo_dir, ["keep.apk"])
    assert (repo_dir / "keep.apk").exists()
    assert (repo_dir / "keep.apk1").exists()
    assert (repo_dir / "old.apk1").exists()


def test_cleanup_version_dirs(tmp_path):
    for v in ["v3", "v2", "v1"]:
        _verdir(tmp_path, v)
    cleanup_version_dirs(tmp_path, keep_versions=2)
    assert (tmp_path / "v3").exists()
    assert (tmp_path / "v2").exists()
    assert not (tmp_path / "v1").exists()


def test_cleanup_version_dirs_zero_keeps_all(tmp_path):
    for v in ["v3", "v2", "v1"]:
        _verdir(tmp_path, v)
    cleanup_version_dirs(tmp_path, keep_versions=0)
    assert (tmp_path / "v3").exists() and (tmp_path / "v2").exists() and (tmp_path / "v1").exists()


def test_cleanup_apk1_keep_two(tmp_path):
    apk1 = {v: [f"{v}.apk1"] for v in ["v3", "v2", "v1"]}
    for names in apk1.values():
        (tmp_path / names[0]).write_bytes(b"x")
    cleanup_apk1(tmp_path, apk1, keep_versions=2)
    assert (tmp_path / "v3.apk1").exists()
    assert (tmp_path / "v2.apk1").exists()
    assert not (tmp_path / "v1.apk1").exists()


def test_cleanup_apk1_zero_keeps_all(tmp_path):
    apk1 = {v: [f"{v}.apk1"] for v in ["v3", "v2", "v1"]}
    for names in apk1.values():
        (tmp_path / names[0]).write_bytes(b"x")
    cleanup_apk1(tmp_path, apk1, keep_versions=0)
    assert (tmp_path / "v3.apk1").exists() and (tmp_path / "v1.apk1").exists()


def test_cleanup_apk1_shared_name_keeps_current(tmp_path):
    # rename_with_repo=false：新旧版本 apk1 文件名相同，清理不得误删
    # 当前版本刚复制的文件（excess 中的旧版本名 = 当前版本名时必须保留）
    apk1 = {"v1": ["app-release.apk1"], "v2": ["app-release.apk1"]}
    (tmp_path / "app-release.apk1").write_bytes(b"x")
    cleanup_apk1(tmp_path, apk1, keep_versions=1, current_names=["app-release.apk1"])
    assert (tmp_path / "app-release.apk1").exists()


def test_cleanup_apk1_rename_with_repo_removes_old(tmp_path):
    # rename_with_repo=true：各版本文件名不同，仅删除旧版本的文件
    apk1 = {"v1": ["b-app-release-1.0.apk1"], "v2": ["b-app-release-2.0.apk1"]}
    (tmp_path / "b-app-release-1.0.apk1").write_bytes(b"x")
    (tmp_path / "b-app-release-2.0.apk1").write_bytes(b"x")
    cleanup_apk1(tmp_path, apk1, keep_versions=1, current_names=["b-app-release-2.0.apk1"])
    assert not (tmp_path / "b-app-release-1.0.apk1").exists()
    assert (tmp_path / "b-app-release-2.0.apk1").exists()


def test_cleanup_version_dirs_natural_sort(tmp_path):
    # v1.10 比 v1.2.9 新：字典序排序会误删 v1.10，应按数值比较
    for v in ["v1.9", "v1.2.9", "v1.10"]:
        _verdir(tmp_path, v)
    cleanup_version_dirs(tmp_path, keep_versions=2)
    assert (tmp_path / "v1.10").exists()
    assert (tmp_path / "v1.9").exists()
    assert not (tmp_path / "v1.2.9").exists()


def test_cleanup_apk1_natural_sort(tmp_path):
    apk1 = {v: [f"{v}.apk1"] for v in ["v1.9", "v1.2.9", "v1.10"]}
    for names in apk1.values():
        (tmp_path / names[0]).write_bytes(b"x")
    cleanup_apk1(tmp_path, apk1, keep_versions=2, current_names=["v1.10.apk1", "v1.9.apk1"])
    assert (tmp_path / "v1.10.apk1").exists()
    assert (tmp_path / "v1.9.apk1").exists()
    assert not (tmp_path / "v1.2.9.apk1").exists()


def test_prune_apk1_records(tmp_path):
    """映射修剪：set_apk1 累积多版本后，prune 只留最近 keep_versions 条记录"""
    t = VersionTracker(tmp_path / "rec.json")
    for v in ["v1", "v2", "v3"]:
        t.set_apk1("a/b", v, [f"{v}.apk1"])
    t.prune_apk1("a/b", keep_versions=1)
    assert t.get_apk1("a/b") == {"v3": ["v3.apk1"]}


def test_prune_apk1_keep_two(tmp_path):
    t = VersionTracker(tmp_path / "rec.json")
    for v in ["v1", "v2", "v3"]:
        t.set_apk1("a/b", v, [f"{v}.apk1"])
    t.prune_apk1("a/b", keep_versions=2)
    assert t.get_apk1("a/b") == {"v2": ["v2.apk1"], "v3": ["v3.apk1"]}


def test_prune_apk1_zero_keeps_all(tmp_path):
    """keep_versions=0 不清理也不修剪"""
    t = VersionTracker(tmp_path / "rec.json")
    for v in ["v1", "v2", "v3"]:
        t.set_apk1("a/b", v, [f"{v}.apk1"])
    t.prune_apk1("a/b", keep_versions=0)
    assert len(t.get_apk1("a/b")) == 3
