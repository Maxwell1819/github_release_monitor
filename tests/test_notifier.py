from src.downloader import DownloadResult
from src.notifier import (
    NotificationManager, PushPlusNotifier, ServerChanNotifier,
    TelegramNotifier, build_download_message,
)


class FakePushResp:
    def __init__(self, code, msg=""):
        self._code = code
        self._msg = msg

    def json(self):
        return {"code": self._code, "msg": self._msg}


class FakeSCResp:
    def json(self):
        return {"code": 0}


class FakeTGResp:
    def json(self):
        return {"ok": True}


class FakePoster:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return self.responses.pop(0)


def _results():
    return [
        DownloadResult(asset_name="app.apk", success=True, size_mb=12.3, checksum_verified=True, target_name="projectname-app-1.0.apk"),
        DownloadResult(asset_name="bad.apk", success=False, error="HTTP 500", target_name="bad.apk"),
    ]


def test_build_message_contains_parts():
    title, html = build_download_message(
        "a/b", "v1.0", _results(), "release notes", "2026-08-01T00:00:00Z",
        apk1_sync={"expected": ["projectname-app-1.0.apk1"], "copied": ["projectname-app-1.0.apk1"]}, notes_translated=True,
    )
    assert title == "[Github] b v1.0"
    assert "1/2" in html  # 下载状态
    assert "✅" in html and "❌" in html
    assert "✓SHA256" in html
    assert "→ TV版 ✓" in html
    assert "发布说明（译文）" in html
    assert "https://github.com/a/b/releases/tag/v1.0" in html


def test_pushplus_success(monkeypatch):
    poster = FakePoster([FakePushResp(200)])
    n = PushPlusNotifier("token", "topic")
    monkeypatch.setattr("src.notifier.requests.post", lambda url, json, timeout: poster.post(url, json, timeout))
    assert n.send("t", "c") is True
    assert poster.calls[0][1]["topic"] == "topic"
    assert poster.calls[0][1]["template"] == "html"


def test_pushplus_business_error_no_retry(monkeypatch):
    poster = FakePoster([FakePushResp(1000, "bad token")])
    monkeypatch.setattr("src.notifier.requests.post", lambda url, json, timeout: poster.post(url, json, timeout))
    n = PushPlusNotifier("token")
    assert n.send("t", "c") is False
    assert len(poster.calls) == 1  # 业务错误不重试


def test_serverchan_success(monkeypatch):
    poster = FakePoster([FakeSCResp()])
    monkeypatch.setattr("src.notifier.requests.post", lambda url, json, timeout: poster.post(url, json, timeout))
    n = ServerChanNotifier("key")
    assert n.send("t", "c") is True
    assert "sctapi.ftqq.com/key.send" in poster.calls[0][0]


def test_telegram_success(monkeypatch):
    poster = FakePoster([FakeTGResp()])
    monkeypatch.setattr("src.notifier.requests.post", lambda url, json, timeout: poster.post(url, json, timeout))
    n = TelegramNotifier("bot123", "chat1")
    assert n.send("t", "c") is True
    assert "api.telegram.org/botbot123/sendMessage" in poster.calls[0][0]


def test_manager_any_channel_success(monkeypatch):
    poster = FakePoster([FakePushResp(200)])
    monkeypatch.setattr("src.notifier.requests.post", lambda url, json, timeout: poster.post(url, json, timeout))
    mgr = NotificationManager([PushPlusNotifier("t")])
    ok = mgr.send_download("a/b", "v1", _results(), "notes", "")
    assert ok is True


def test_manager_all_failed(monkeypatch):
    poster = FakePoster([FakePushResp(1000)])
    monkeypatch.setattr("src.notifier.requests.post", lambda url, json, timeout: poster.post(url, json, timeout))
    mgr = NotificationManager([PushPlusNotifier("bad")])
    assert mgr.send_download("a/b", "v1", _results(), "notes", "") is False


def test_send_test_returns_channel_results(monkeypatch):
    poster = FakePoster([FakePushResp(200)])
    monkeypatch.setattr("src.notifier.requests.post", lambda url, json, timeout: poster.post(url, json, timeout))
    mgr = NotificationManager([PushPlusNotifier("t")])
    result = mgr.send_test()
    assert result["ok"] == 1
    assert result["fail"] == 0
