from src.github_api import (
    GitHubClient, HttpClient, match_asset, match_name_regex,
)


class FakeResp:
    def __init__(self, status=200, headers=None, json_data=None):
        self.status_code = status
        self.headers = headers or {}
        self._json = json_data

    def json(self):
        return self._json


class FakeHttp:
    """记录请求参数并返回预设响应队列"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, stream=False, timeout=None, max_attempts=6):
        self.calls.append((url, headers, stream, timeout, max_attempts))
        r = self.responses.pop(0)
        if callable(r):
            r = r(self.calls[-1])
        return r


def _release(tag, published, prerelease=False, draft=False, assets=None):
    return {
        "tag_name": tag, "published_at": published, "prerelease": prerelease,
        "draft": draft, "assets": assets or [],
    }


def test_fetch_latest_stable_filters_prerelease():
    http = FakeHttp([FakeResp(200, {"etag": '"e1"'}, [
        _release("v1.1", "2026-01-02T00:00:00Z", prerelease=True),
        _release("v1.0", "2026-01-01T00:00:00Z"),
    ])])
    gh = GitHubClient(http, "")
    release, etag, not_modified = gh.fetch_latest_release("a/b", release_type="latest")
    assert release["tag_name"] == "v1.0"
    assert etag == '"e1"'
    assert not_modified is False


def test_fetch_latest_prerelease_included():
    http = FakeHttp([FakeResp(200, {"etag": '"e2"'}, [
        _release("v2.0-rc1", "2026-02-01T00:00:00Z", prerelease=True),
        _release("v1.9", "2026-01-01T00:00:00Z"),
    ])])
    gh = GitHubClient(http, "")
    release, _, _ = gh.fetch_latest_release("a/b", release_type="prerelease")
    assert release["tag_name"] == "v2.0-rc1"


def test_fetch_draft_skipped_and_sorted():
    http = FakeHttp([FakeResp(200, {}, [
        _release("v1.0", "2026-01-01T00:00:00Z"),
        _release("v0.9", "2026-02-01T00:00:00Z"),  # 更早发布但列表在后
        _release("v9.9", "2026-03-01T00:00:00Z", draft=True),
    ])])
    gh = GitHubClient(http, "")
    release, _, _ = gh.fetch_latest_release("a/b")
    assert release["tag_name"] == "v0.9"


def test_fetch_tag_regex_filters_desktop_line():
    """tag_regex 只匹配指定 tag 前缀的 release（多 release 线场景）"""
    http = FakeHttp([FakeResp(200, {}, [
        _release("v1.21.3", "2026-08-08T00:00:00Z"),           # CLI 线
        _release("desktop-v1.21.3", "2026-08-08T01:00:00Z"),  # Desktop 线
    ])])
    gh = GitHubClient(http, "")
    release, _, _ = gh.fetch_latest_release("a/b", tag_regex="desktop")
    assert release["tag_name"] == "desktop-v1.21.3"


def test_fetch_tag_regex_no_match_returns_none():
    """tag_regex 无匹配时返回 None"""
    http = FakeHttp([FakeResp(200, {}, [
        _release("v1.21.3", "2026-08-08T00:00:00Z"),
    ])])
    gh = GitHubClient(http, "")
    release, _, _ = gh.fetch_latest_release("a/b", tag_regex="desktop")
    assert release is None


def test_fetch_tag_regex_none_keeps_default_order():
    """未配置 tag_regex 时行为不变（取排序第一个）"""
    http = FakeHttp([FakeResp(200, {}, [
        _release("v1.21.3", "2026-08-08T00:00:00Z"),
        _release("desktop-v1.21.3", "2026-08-08T01:00:00Z"),
    ])])
    gh = GitHubClient(http, "")
    release, _, _ = gh.fetch_latest_release("a/b")
    assert release["tag_name"] == "desktop-v1.21.3"  # 时间最新的仍被选中


def test_fetch_tag_regex_stable_suffix_blocks_beta():
    """tag_regex 锁定 Stable 后缀场景：与 prerelease 标志无关，仅按 tag 模式匹配。
    模拟 Beta/Stable 并存且 Beta 发布时间更晚，验证 tag_regex 兜底防 release_type 误改"""
    http = FakeHttp([FakeResp(200, {}, [
        _release("32.10", "2026-08-10T01:00:00Z", prerelease=True),    # Beta，更晚发布
        _release("32.10s", "2026-08-10T00:00:00Z", prerelease=False),  # Stable
    ])])
    gh = GitHubClient(http, "")
    release, _, _ = gh.fetch_latest_release("owner/repo", tag_regex=r"\d+\.\d+s$")
    assert release["tag_name"] == "32.10s"


def test_etag_304():
    http = FakeHttp([FakeResp(304, {})])
    gh = GitHubClient(http, "")
    release, etag, not_modified = gh.fetch_latest_release("a/b", etag='"old"')
    assert release is None
    assert not_modified is True
    assert http.calls[0][1].get("If-None-Match") == '"old"'


def test_403_prints_rate_limit(caplog):
    http = FakeHttp([FakeResp(403, {"x-ratelimit-reset": "0"})])
    gh = GitHubClient(http, "")
    import logging
    with caplog.at_level(logging.ERROR, logger="ReleaseMonitor"):
        release, _, _ = gh.fetch_latest_release("a/b")
    assert release is None
    assert "配额" in caplog.text


def test_http_client_retries_on_502(monkeypatch):
    """重试由 HttpClient 单层负责：502 后重试成功"""
    class FakeSession:
        def __init__(self):
            self.responses = [FakeResp(502), FakeResp(200, {}, [])]
            self.calls = 0
            self.headers = {}

        def get(self, url, headers=None, stream=False, timeout=None):
            self.calls += 1
            return self.responses.pop(0)

        def close(self):
            pass

    fake = FakeSession()
    monkeypatch.setattr("src.github_api.requests.Session", lambda: fake)
    monkeypatch.setattr("src.github_api.time.sleep", lambda s: None)
    http = HttpClient()
    resp = http.get("https://example.com/x")
    assert resp.status_code == 200
    assert fake.calls == 2


def test_validate_token_format_warns(caplog):
    http = FakeHttp([])
    gh = GitHubClient(http, "not-a-ghp")
    import logging
    with caplog.at_level(logging.WARNING, logger="ReleaseMonitor"):
        assert gh.validate_token() is None
    assert "格式" in caplog.text


def test_validate_token_ok():
    http = FakeHttp([FakeResp(200, {}, {"login": "testuser"})])
    gh = GitHubClient(http, "ghp_validtoken")
    assert gh.validate_token() == "testuser"


def test_fetch_via_mirror_when_direct_fails():
    """直连失败（返回 None）→ 自动回退到镜像 {mirror}/https://api.github.com/..."""
    http = FakeHttp([None, FakeResp(200, {"etag": '"e2"'}, [
        _release("v1.0", "2026-01-01T00:00:00Z"),
    ])])
    gh = GitHubClient(http, "", mirrors=["https://ghfast.top"])
    release, _, _ = gh.fetch_latest_release("a/b")
    assert release["tag_name"] == "v1.0"
    assert len(http.calls) == 2
    assert http.calls[1][0] == "https://ghfast.top/https://api.github.com/repos/a/b/releases?per_page=100"


def test_fetch_direct_success_no_mirror():
    """直连成功时不走镜像"""
    http = FakeHttp([FakeResp(200, {"etag": '"e"'}, [
        _release("v1.0", "2026-01-01T00:00:00Z"),
    ])])
    gh = GitHubClient(http, "", mirrors=["https://ghfast.top"])
    release, _, _ = gh.fetch_latest_release("a/b")
    assert release["tag_name"] == "v1.0"
    assert len(http.calls) == 1
    assert http.calls[0][0].startswith("https://api.github.com/")


def test_fetch_mirror_chain_all_fail_returns_none():
    """所有镜像都失败 → 返回 None 与直连失败一致"""
    http = FakeHttp([None, None, None])
    gh = GitHubClient(http, "", mirrors=["https://ghfast.top", "https://gh-proxy.com"])
    release, _, _ = gh.fetch_latest_release("a/b")
    assert release is None
    assert len(http.calls) == 3


def test_mirror_request_strips_authorization():
    """隐私：镜像回退请求不得携带 GitHub token（第三方代理不可信）"""
    http = FakeHttp([None, FakeResp(200, {}, [
        _release("v1.0", "2026-01-01T00:00:00Z"),
    ])])
    gh = GitHubClient(http, "ghp_secrettoken", mirrors=["https://ghfast.top"])
    release, _, _ = gh.fetch_latest_release("a/b")
    assert release["tag_name"] == "v1.0"
    # 直连带 token；镜像请求 headers 无 Authorization
    assert http.calls[0][1].get("Authorization") == "token ghp_secrettoken"
    assert "Authorization" not in http.calls[1][1]


def test_validate_token_via_mirror_when_direct_fails():
    """token 验证也支持镜像回退"""
    http = FakeHttp([None, FakeResp(200, {}, {"login": "testuser"})])
    gh = GitHubClient(http, "ghp_validtoken", mirrors=["https://ghfast.top"])
    assert gh.validate_token() == "testuser"
    assert http.calls[1][0] == "https://ghfast.top/https://api.github.com/user"


def test_match_asset_rules():
    rules = [{"keywords": ["moonlight"], "extensions": [".apk"]}]
    assert match_asset({"name": "Moonlight-1.0.apk", "size": 100}, rules)
    assert not match_asset({"name": "Moonlight-1.0.exe", "size": 100}, rules)
    assert not match_asset({"name": "Other-1.0.apk", "size": 100}, rules)


def test_match_asset_extensions_all():
    rules = [{"keywords": [], "extensions": ["all"]}]
    assert match_asset({"name": "anything.zip", "size": 100}, rules)


def test_match_asset_size_limit():
    rules = [{"keywords": [], "extensions": ["all"]}]
    assert not match_asset({"name": "big.zip", "size": 2 * 1024 * 1024 * 1024}, rules)


def test_match_name_regex():
    assert match_name_regex("mihon-v0.17-foss.apk", None, "foss", "a/b") is False
    assert match_name_regex("mihon-v0.17.apk", None, "foss", "a/b") is True
    assert match_name_regex("app-legacy.apk", "", "legacy", "a/b") is False
    assert match_name_regex("app-release.apk", "release", None, "a/b") is True
    assert match_name_regex("app-beta.apk", "release", None, "a/b") is False
    assert match_name_regex("x.apk", "[invalid", None, "a/b") is True  # 正则错误放行
