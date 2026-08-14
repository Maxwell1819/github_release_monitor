from src.downloader import Downloader, MirrorManager, verify_sha256


class FakeResp:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.text = b"".join(self._chunks).decode("utf-8", "replace")

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, stream=False, timeout=None):
        self.calls.append((url, headers, timeout))
        return self.responses.pop(0)


def make_part(tmp_path, size=1024 * 1024 + 10):
    p = tmp_path / "app.part"
    p.write_bytes(b"a" * size)
    return p


def test_mirror_manager_prefix_style():
    mgr = MirrorManager(["https://ghproxy.cxkpro.top"])
    assert mgr.translate("https://github.com/a/b/releases/download/v1/f.apk") == \
        "https://ghproxy.cxkpro.top/https://github.com/a/b/releases/download/v1/f.apk"


def test_mirror_manager_ghfast_prefix_style():
    """ghfast.top 属于 ghproxy 前缀类镜像，必须加 /https://github.com 前缀"""
    mgr = MirrorManager(["https://ghfast.top"])
    assert mgr.translate("https://github.com/a/b/releases/download/v1/f.apk") == \
        "https://ghfast.top/https://github.com/a/b/releases/download/v1/f.apk"


def test_mirror_manager_replace_style():
    mgr = MirrorManager(["https://mirror.example.com"])
    assert mgr.translate("https://github.com/a/b/releases/download/v1/f.apk") == \
        "https://mirror.example.com/a/b/releases/download/v1/f.apk"


def test_mirror_manager_switch():
    mgr = MirrorManager(["https://a.top", "https://b.top"])
    assert mgr.current() == "https://a.top"
    mgr.switch()
    assert mgr.current() == "https://b.top"


def test_download_fresh(tmp_path):
    body = b"x" * 100
    dl = Downloader(FakeSession([FakeResp(200, {
        "Content-Type": "application/octet-stream", "Content-Length": "100",
    }, chunks=(body,))]))
    result = dl.download("https://m/app.apk", tmp_path / "app.apk", "app.apk")
    assert result.success
    assert (tmp_path / "app.apk").read_bytes() == body
    assert result.size_mb > 0


def test_download_resume_206(tmp_path):
    make_part(tmp_path)
    body = b"y" * 50
    dl = Downloader(FakeSession([FakeResp(206, {
        "Content-Type": "application/octet-stream",
        "Content-Range": f"bytes={1024 * 1024 + 10}-{(1024 * 1024 + 10) + 50 - 1}/{1024 * 1024 + 60}",
    }, chunks=(body,))]))
    result = dl.download("https://m/app.apk", tmp_path / "app.apk", "app.apk")
    assert result.success
    assert (tmp_path / "app.apk").read_bytes() == b"a" * (1024 * 1024 + 10) + body
    assert result.error is None


def test_download_html_error_page(tmp_path):
    dl = Downloader(FakeSession([FakeResp(200, {
        "Content-Type": "text/html; charset=utf-8",
    }, chunks=(b"<html>404</html>",))]))
    result = dl.download("https://m/app.apk", tmp_path / "app.apk", "app.apk")
    assert not result.success
    assert "HTML" in result.error
    # 文案不应误导为镜像问题（直连/镜像通用）
    assert "镜像" not in result.error


def test_download_http_error_without_range(tmp_path):
    body = b"x" * 100
    dl = Downloader(FakeSession([FakeResp(500, {
        "Content-Type": "text/plain", "Content-Length": "100",
    }, chunks=(body,))]))
    result = dl.download("https://m/app.apk", tmp_path / "app.apk", "app.apk")
    assert not result.success
    assert result.error == "HTTP 500"
    assert not (tmp_path / "app.apk").exists()


def test_download_416_restarts(tmp_path):
    make_part(tmp_path)
    body = b"x" * 100
    dl = Downloader(FakeSession([
        FakeResp(416, {"Content-Type": "application/octet-stream"}),
        FakeResp(200, {"Content-Type": "application/octet-stream", "Content-Length": "100"}, chunks=(body,)),
    ]))
    result = dl.download("https://m/app.apk", tmp_path / "app.apk", "app.apk")
    assert result.success


def test_download_slow_aborts(tmp_path, monkeypatch):
    import time
    fake_now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now.__setitem__(0, fake_now[0] + 0.5) or fake_now[0])
    chunks = [b"x" * 65536] * 40
    dl = Downloader(FakeSession([FakeResp(200, {
        "Content-Type": "application/octet-stream", "Content-Length": "100000000",
    }, chunks=chunks)]), speed_threshold_kbps=1024, slow_timeout_sec=1)

    result = dl.download("https://m/app.apk", tmp_path / "app.apk", "app.apk")
    assert not result.success
    assert result.error == "下载速度过慢"


def test_check_speed_nearly_done_does_not_abort(monkeypatch):
    """P1：已下载 ≥90%（或剩余 <10MB）时慢速不触发切源"""
    import time
    fake_now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now.__setitem__(0, fake_now[0] + 10) or fake_now[0])
    dl = Downloader(None, speed_threshold_kbps=1024, slow_timeout_sec=1)
    # 慢速 + 已下载 95% → 连续两次都不切源（豁免生效）
    ok, st = dl._check_speed(500 * 1024, None, downloaded=95 * 1024 * 1024, total=100 * 1024 * 1024)
    assert ok
    ok, st = dl._check_speed(500 * 1024, st, downloaded=96 * 1024 * 1024, total=100 * 1024 * 1024)
    assert ok
    # 慢速 + 已下载 50% → 首次设置计时、二次触发切源
    ok, st2 = dl._check_speed(500 * 1024, None, downloaded=50 * 1024 * 1024, total=100 * 1024 * 1024)
    assert ok
    assert st2 is not None
    ok, _ = dl._check_speed(500 * 1024, st2, downloaded=50 * 1024 * 1024, total=100 * 1024 * 1024)
    assert not ok


def test_download_size_mismatch_retries_once(tmp_path):
    dl = Downloader(FakeSession([
        FakeResp(200, {"Content-Type": "application/octet-stream", "Content-Length": "100"},
                 chunks=(b"short",)),
        FakeResp(200, {"Content-Type": "application/octet-stream", "Content-Length": "100"},
                 chunks=(b"x" * 100,)),
    ]))
    result = dl.download("https://m/app.apk", tmp_path / "app.apk", "app.apk")
    assert result.success


def test_download_size_mismatch_fails_twice(tmp_path):
    dl = Downloader(FakeSession([
        FakeResp(200, {"Content-Type": "application/octet-stream", "Content-Length": "100"},
                 chunks=(b"short",)),
        FakeResp(200, {"Content-Type": "application/octet-stream", "Content-Length": "100"},
                 chunks=(b"also short",)),
    ]))
    result = dl.download("https://m/app.apk", tmp_path / "app.apk", "app.apk")
    assert not result.success
    assert result.error == "文件大小不匹配"


def test_verify_sha256_ok(tmp_path):
    f = tmp_path / "app.apk"
    f.write_bytes(b"hello")
    import hashlib
    digest = hashlib.sha256(b"hello").hexdigest()
    assets = [{"name": "app.sha256", "browser_download_url": "https://m/app.sha256"}]
    sess = FakeSession([FakeResp(200, {}, chunks=(f"{digest}  app.apk\n".encode(),))])
    ok, err = verify_sha256(f, assets, sess)
    assert ok
    assert err is None


def test_verify_sha256_mismatch_deletes(tmp_path):
    f = tmp_path / "app.apk"
    f.write_bytes(b"hello")
    digest = "0" * 64
    assets = [{"name": "app.sha256", "browser_download_url": "https://m/app.sha256"}]
    sess = FakeSession([FakeResp(200, {}, chunks=(f"{digest}  app.apk\n".encode(),))])
    ok, err = verify_sha256(f, assets, sess)
    assert not ok
    assert err == "SHA256 不匹配"
    assert not f.exists()