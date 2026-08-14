"""GitHub API 客户端 + ETag 缓存 + 资产匹配过滤"""

import json
import logging
import random
import re
import time
from typing import List, Optional, Tuple

import requests

logger = logging.getLogger("ReleaseMonitor")

# 可重试的 HTTP 状态码
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 资产大小上限（1GB）
MAX_ASSET_SIZE = 1024 * 1024 * 1024


class HttpClient:
    """通用 HTTP 客户端，带指数退避重试"""

    def __init__(self, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "GitHubReleaseMonitor/2.0"})
        self.timeout = timeout

    def close(self) -> None:
        self.session.close()

    def get(
        self, url: str, *,
        headers: Optional[dict] = None,
        stream: bool = False,
        timeout: Optional[int] = None,
        max_attempts: int = 6,
    ) -> Optional[requests.Response]:
        for attempt in range(max_attempts):
            try:
                t = int((timeout or self.timeout) * (1.5 ** attempt))
                resp = self.session.get(url, headers=headers, stream=stream, timeout=t)
                if resp.status_code < 400 or resp.status_code in (401, 403, 404):
                    return resp
                if resp.status_code in RETRYABLE_STATUS:
                    delay = min(2 ** attempt * 2, 60) * random.uniform(0.8, 1.2)
                    logger.warning(f"HTTP {resp.status_code}，{delay:.0f}s 后重试")
                    time.sleep(delay)
                    continue
                return resp
            except requests.RequestException as e:
                if attempt == max_attempts - 1:
                    logger.error(f"请求最终失败: {e}")
                    return None
                delay = min(2 ** attempt * 2, 30)
                logger.warning(f"{type(e).__name__}，{delay:.0f}s 后重试")
                time.sleep(delay)
        return None


class GitHubClient:
    """GitHub Releases API 客户端"""

    API = "https://api.github.com"

    def __init__(self, http: HttpClient, token: str = "", mirrors: Optional[List[str]] = None):
        self.http = http
        self.token = token
        self.mirrors = list(mirrors or [])

    def _mirror_url(self, url: str, mirror: str) -> str:
        """API 镜像 URL：ghproxy 类前缀格式 {host}/{原URL}"""
        return f"{mirror}/{url}"

    def _request(
        self, url: str, headers: Optional[dict] = None,
    ) -> Optional[requests.Response]:
        """带镜像回退的请求：直连快速探测（2 次）失败时，
        依次尝试 {镜像}/https://api.github.com/... 兜底。
        镜像请求剥离 Authorization（第三方代理不信任，泄露无益）"""
        resp = self.http.get(url, headers=headers, timeout=30, max_attempts=2)
        if resp is not None or not self.mirrors:
            return resp
        logger.warning("直连 api.github.com 失败，尝试镜像回退")
        mirror_headers = {
            k: v for k, v in (headers or {}).items()
            if k.lower() != "authorization"
        }
        for i, mirror in enumerate(self.mirrors, 1):
            logger.info(f"API 镜像 [{i}/{len(self.mirrors)}]: {mirror}")
            resp = self.http.get(
                self._mirror_url(url, mirror), headers=mirror_headers,
                timeout=30, max_attempts=2,
            )
            if resp is not None:
                return resp
        return None

    def _headers(self, etag: Optional[str] = None) -> dict:
        h = {
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            h["Authorization"] = f"token {self.token}"
        if etag:
            h["If-None-Match"] = etag
        return h

    def validate_token(self) -> Optional[str]:
        """验证 token，返回用户名或 None"""
        if not self.token:
            logger.info("未配置 Token，使用匿名访问")
            return None
        if not (self.token.startswith("ghp_") or self.token.startswith("github_pat_")):
            logger.warning("Token 格式异常")
            return None
        data = self._get_json(f"{self.API}/user")
        if data and data.get("login"):
            logger.info(f"Token 验证通过: {data['login']}")
            return data["login"]
        logger.warning("Token 无效，使用匿名访问")
        return None

    def _get_json(self, url: str) -> Optional[dict]:
        resp = self._request(url, headers=self._headers())
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            logger.error(f"JSON 解析失败: {url}")
            return None

    def fetch_latest_release(
        self, repo: str, *,
        release_type: str = "latest",
        etag: Optional[str] = None,
        tag_regex: Optional[str] = None,
    ) -> Tuple[Optional[dict], Optional[str], bool]:
        """
        获取最新 release。

        Args:
            tag_regex: 可选，用正则筛选 release tag 名（如 "desktop" 匹配
                desktop-v1.2.3）。不配置时取按发布时间排序后的第一个。

        Returns:
            (release, new_etag, is_not_modified)
        """
        url = f"{self.API}/repos/{repo}/releases?per_page=100"

        # 重试（429/5xx/网络异常）由 HttpClient.get 内部处理；直连失败自动回退镜像
        resp = self._request(url, headers=self._headers(etag))

        if resp is None:
            logger.error(f"请求失败: {repo}")
            return None, None, False

        if resp.status_code == 304:
            logger.debug(f"[ETag] 未变化: {repo}")
            return None, etag, True

        new_etag = resp.headers.get("etag")

        if resp.status_code == 404:
            logger.error(f"仓库不存在: {repo}")
            return None, new_etag, False
        if resp.status_code == 403:
            reset = resp.headers.get("x-ratelimit-reset")
            try:
                reset_time = time.strftime("%H:%M:%S", time.localtime(int(reset))) if reset else "N/A"
            except (ValueError, TypeError, OSError):
                reset_time = "N/A"
            logger.error(f"API 配额耗尽，重置时间: {reset_time}")
            return None, new_etag, False
        if resp.status_code >= 400:
            logger.error(f"API 错误 {resp.status_code}: {repo}")
            return None, new_etag, False

        try:
            releases = resp.json()
        except json.JSONDecodeError:
            logger.error(f"JSON 解析失败: {repo}")
            return None, new_etag, False

        filtered = self._filter_releases(releases, release_type, tag_regex)

        if filtered:
            return filtered[0], new_etag, False

        logger.info(f"无匹配 Release: {repo}")
        return None, new_etag, False

    @staticmethod
    def _filter_releases(
        releases: List[dict], release_type: str,
        tag_regex: Optional[str] = None,
    ) -> List[dict]:
        """过滤草稿 + 预发布（可选按 tag_regex 筛 tag 名），按发布时间降序"""
        result = []
        for r in releases:
            if r.get("draft"):
                continue
            if release_type != "prerelease" and r.get("prerelease"):
                continue
            if tag_regex:
                try:
                    if not re.search(tag_regex, r.get("tag_name", "")):
                        continue
                except re.error as e:
                    logger.error(f"tag_regex 无效: {e}")
                    return []
            result.append(r)
        result.sort(key=lambda x: x.get("published_at") or x.get("created_at") or "", reverse=True)
        return result


def match_name_regex(
    name: str,
    include_regex: Optional[str], exclude_regex: Optional[str],
    repo: str,
) -> bool:
    """正则匹配资产文件名：exclude 优先排除，include 非空时必须命中"""
    try:
        if exclude_regex and re.search(exclude_regex, name, re.IGNORECASE):
            return False
        if include_regex:
            return bool(re.search(include_regex, name, re.IGNORECASE))
    except re.error as e:
        logger.error(f"正则无效 ({repo}): {e}，忽略过滤")
    return True


def match_asset(asset: dict, rules: List[dict]) -> bool:
    """匹配 asset：关键词 + 扩展名 + 1GB 大小上限"""
    name = asset.get("name", "").lower()
    size = asset.get("size", 0)
    if size > MAX_ASSET_SIZE:
        return False
    for rule in rules:
        kw = rule.get("keywords", [])
        ext = rule.get("extensions", [])
        if not all(k.lower() in name for k in kw):
            continue
        if "all" in ext or any(name.endswith(e.lower()) for e in ext):
            return True
    return False
