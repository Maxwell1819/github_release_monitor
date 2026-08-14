from main import ReleaseMonitor
from src.config import AppConfig, DownloadConfig, NotifyConfig, RepoConfig, TranslationConfig


def _make_config(tmp_path, **repo_kwargs):
    dl = tmp_path / "dl"
    dl.mkdir()
    kwargs = {"repo": "a/b", "files": [{"keywords": ["app"], "extensions": [".apk"]}]}
    kwargs.update(repo_kwargs)
    return AppConfig(
        download_path=dl,
        download=DownloadConfig(),
        notify=NotifyConfig(),
        translation=TranslationConfig(),
        repos=[RepoConfig(**kwargs)],
    )


class FakeGitHub:
    def __init__(self, release):
        self.release = release
        self.calls = 0

    def fetch_latest_release(self, repo, release_type="latest", etag=None, tag_regex=None):
        self.calls += 1
        if self.release is None:
            return None, '"e"', True
        return self.release, '"e1"', False

    def validate_token(self):
        return None


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.enabled = True

    def send_download(self, *a, **kw):
        self.sent.append((a, kw))
        return True


class FakeTranslator:
    def translate(self, text):
        return "译文:" + text


def _release(version, assets, size=100):
    return {
        "tag_name": version,
        "name": "",
        "body": "release notes text",
        "published_at": "2026-08-01T00:00:00Z",
        "assets": [
            {"name": n, "size": size, "browser_download_url": f"https://m/{n}"}
            for n in assets
        ],
    }


class FakeSess:
    def __init__(self, body=b"x" * 100):
        self.body = body
        self.calls = []

    def get(self, url, headers=None, stream=False, timeout=None):
        self.calls.append(url)
        return _FakeStreamResp(200, {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(self.body)),
        }, self.body)


class FakeSessError:
    """模拟全部下载失败的会话（HTTP 500）"""

    def get(self, url, headers=None, stream=False, timeout=None):
        return _FakeStreamResp(500, {}, b"")

    def close(self):
        pass


class FakeSessSelective:
    """按 URL 区分：直连(无镜像前缀)失败(500)，镜像 URL 成功"""

    def __init__(self, body=b"x" * 100):
        self.body = body
        self.calls = []

    def get(self, url, headers=None, stream=False, timeout=None):
        self.calls.append(url)
        if "ghfast.top" in url:
            return _FakeStreamResp(200, {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(self.body)),
            }, self.body)
        return _FakeStreamResp(500, {}, b"")

    def close(self):
        pass


class _FakeStreamResp:
    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = headers
        self._body = body

    def iter_content(self, chunk_size=None):
        return iter([self._body])


class _FakeHttp:
    def __init__(self, body):
        self.session = FakeSess(body)

    def close(self):
        pass


def _monitor(tmp_path, config, release, monkeypatch):
    m = ReleaseMonitor(config, tmp_path, dry_run=False)
    m.github = FakeGitHub(release)
    m.notif_mgr = FakeNotifier()
    m.translator = None
    m.http = _FakeHttp(b"x" * 100)
    m.downloader.session = m.http.session
    return m


def test_new_version_downloads_and_notifies(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, rename_apk1=True)
    m = _monitor(tmp_path, cfg, _release("v1.0", ["app-release.apk"]), monkeypatch)
    m.run()  # 首次静默
    assert len(m.notif_mgr.sent) == 0
    m2 = _monitor(tmp_path, cfg, _release("v2.0", ["app-release.apk"], size=200), monkeypatch)
    m2.run()
    repo_dir = cfg.download_path / "a_b"
    assert (repo_dir / "app-release.apk").exists()
    assert m2.stats["ok"] + m2.stats["fail"] >= 1
    assert len(m2.notif_mgr.sent) == 1
    assert (repo_dir / "RELEASE_NOTES_b_v2.0.md").exists()
    # rename_with_repo=false：新旧版本 apk1 同名，清理后当前版本文件必须保留
    assert (repo_dir / "app-release.apk1").exists()


def test_second_run_no_redownload(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    m = _monitor(tmp_path, cfg, _release("v1.0", ["app-release.apk"]), monkeypatch)
    m.run()
    m2 = _monitor(tmp_path, cfg, _release("v1.0", ["app-release.apk"]), monkeypatch)
    m2.run()
    assert len(m2.http.session.calls) == 0  # 无下载、无 checksum 请求
    assert len(m2.notif_mgr.sent) == 0


def test_dry_run_writes_state_no_download(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    cfg.dry_run = True
    m = _monitor(tmp_path, cfg, _release("v1.0", ["app-release.apk"]), monkeypatch)
    m.run()
    assert not (cfg.download_path / "a_b" / "app-release.apk").exists()
    assert len(m.notif_mgr.sent) == 0


def test_dry_run_then_real_first_download_notifies(tmp_path, monkeypatch):
    # 推荐流程 dry-run 先验证、再真实运行：真实首次下载即使 same-version
    # 补充下载（new_version=False）也应通知，因为该版本此前从未成功下载过
    (tmp_path / "dry").mkdir()
    cfg_dry = _make_config(tmp_path / "dry")
    mdry = _monitor(tmp_path, cfg_dry, _release("v1.0", ["app-release.apk"]), monkeypatch)
    mdry.config.dry_run = True
    mdry.run()
    assert len(mdry.notif_mgr.sent) == 0
    (tmp_path / "real").mkdir()
    cfg = _make_config(tmp_path / "real")
    m2 = _monitor(tmp_path, cfg, _release("v1.0", ["app-release.apk"]), monkeypatch)
    m2.run()
    assert (cfg.download_path / "a_b" / "app-release.apk").exists()
    assert len(m2.notif_mgr.sent) == 1


def test_failed_download_keeps_old_version_files(tmp_path, monkeypatch):
    # 下载全部失败时不得清理旧版本文件（下载成功后才删）
    cfg = _make_config(tmp_path, keep_versions=1, rename_with_repo=True)
    m = _monitor(tmp_path, cfg, _release("v1.0", ["app-release.apk"]), monkeypatch)
    m.run()
    repo_dir = cfg.download_path / "a_b"
    old = repo_dir / "b-app-release-1.0.apk"
    assert old.exists()
    m2 = _monitor(tmp_path, cfg, _release("v2.0", ["app-release.apk"]), monkeypatch)
    m2.http.session = FakeSessError()
    m2.downloader.session = m2.http.session
    m2.run()
    assert old.exists()  # 本次下载失败，唯一副本不能被清理
    assert m2.stats["fail"] == 1


def test_first_seen_silent_then_notify(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    m = _monitor(tmp_path, cfg, _release("v1.0", ["app-release.apk"]), monkeypatch)
    m.run()
    assert len(m.notif_mgr.sent) == 0  # 首次静默
    m2 = _monitor(tmp_path, cfg, _release("v2.0", ["app-release.apk"]), monkeypatch)
    m2.run()
    assert len(m2.notif_mgr.sent) == 1


def test_keep_versions_subdir(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, keep_versions=2)
    m = _monitor(tmp_path, cfg, _release("v1.0", ["app-release.apk"]), monkeypatch)
    m.run()
    m2 = _monitor(tmp_path, cfg, _release("v2.0", ["app-release.apk"]), monkeypatch)
    m2.run()
    assert (cfg.download_path / "a_b" / "v1.0" / "app-release.apk").exists()
    assert (cfg.download_path / "a_b" / "v2.0" / "app-release.apk").exists()
    m3 = _monitor(tmp_path, cfg, _release("v3.0", ["app-release.apk"]), monkeypatch)
    m3.run()
    assert not (cfg.download_path / "a_b" / "v1.0").exists()
    assert (cfg.download_path / "a_b" / "v3.0" / "app-release.apk").exists()


def test_apk1_custom_output(tmp_path, monkeypatch):
    tv = tmp_path / "tv"
    tv.mkdir()
    cfg = _make_config(tmp_path, rename_apk1=True)
    cfg.apk_rename_output = tv
    m = _monitor(tmp_path, cfg, _release("v1.0", ["app-release.apk"]), monkeypatch)
    m.run()
    assert (tv / "app-release.apk1").exists()


def test_direct_fail_memory_skips_direct_next_file(tmp_path, monkeypatch):
    """P0：直连失败后置位记忆；两个文件并发下载时直连不会无限重复尝试"""
    cfg = _make_config(tmp_path)
    cfg.mirrors = ["https://ghfast.top"]
    m = _monitor(tmp_path, cfg, _release("v1.0", ["app-a.apk", "app-b.apk"]), monkeypatch)
    m.http = _FakeHttp(b"x" * 100)
    m.http.session = FakeSessSelective(b"x" * 100)
    m.downloader.session = m.http.session
    m.run()
    # 两个文件都成功下载
    assert m.stats["ok"] == 2
    assert m.stats["fail"] == 0
    # 直连(https://m/...) 尝试次数受限于并发数（2个文件并发，最多各试1次），
    # 且第二轮镜像调用存在（后续文件直接走镜像）
    direct_calls = [u for u in m.http.session.calls if u.startswith("https://m/")]
    assert 1 <= len(direct_calls) <= 2
    mirror_calls = [u for u in m.http.session.calls if "ghfast.top" in u]
    assert len(mirror_calls) >= 2


def test_direct_fail_memory_applies_across_repos(tmp_path, monkeypatch):
    """P0：仓库A直连失败后，仓库B的文件直接走镜像"""
    cfg = _make_config(tmp_path)
    cfg.mirrors = ["https://ghfast.top"]
    cfg.repos = [
        RepoConfig(repo="a/b", files=[{"keywords": ["app"], "extensions": [".apk"]}]),
        RepoConfig(repo="c/d", files=[{"keywords": ["app"], "extensions": [".apk"]}]),
    ]
    m = _monitor(tmp_path, cfg, _release("v1.0", ["app-a.apk", "app-b.apk"]), monkeypatch)
    m.http = _FakeHttp(b"x" * 100)
    m.http.session = FakeSessSelective(b"x" * 100)
    m.downloader.session = m.http.session
    # 第一个仓库模拟全部直连失败（FakeSessSelective 直连 500），
    # 处理后 _direct_failed=True，第二个仓库应跳过直连
    m.run()
    assert m.stats["ok"] >= 2
    direct_calls = [u for u in m.http.session.calls if u.startswith("https://m/")]
    assert len(direct_calls) <= 2  # 并发上限，不会随仓库数量累积重复直连


def test_mirror_cycle_retries_first_mirror(tmp_path, monkeypatch):
    """P2：所有镜像一轮失败后，从第一个镜像再循环一轮（而非直接回直连）"""
    cfg = _make_config(tmp_path)
    cfg.mirrors = ["https://ghfast.top", "https://gh-proxy.com"]

    class FakeSessMirrorCycle:
        def __init__(self):
            self.calls = []
            self.ghfast_fails = 1  # ghfast 前 1 次失败，第 2 次成功

        def get(self, url, headers=None, stream=False, timeout=None):
            self.calls.append(url)
            if "ghfast.top" in url:
                if self.ghfast_fails > 0:
                    self.ghfast_fails -= 1
                    return _FakeStreamResp(500, {}, b"")
                return _FakeStreamResp(200, {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": "100",
                }, b"x" * 100)
            return _FakeStreamResp(500, {}, b"")

    m = _monitor(tmp_path, cfg, _release("v1.0", ["app-release.apk"]), monkeypatch)
    sess = FakeSessMirrorCycle()
    m.http.session = sess
    m.downloader.session = sess
    m.run()
    # 直连失败 → 镜像1失败 → 镜像2失败 → 循环镜像1成功
    assert m.stats["ok"] == 1
    ghfast_calls = [u for u in sess.calls if "ghfast.top" in u]
    assert len(ghfast_calls) == 2  # 第一轮失败 + 循环后成功
    # 两轮镜像均失败前不应直接回直连
    direct_calls = [u for u in sess.calls if u.startswith("https://m/")]
    assert len(direct_calls) == 1  # 仅最初的直连尝试