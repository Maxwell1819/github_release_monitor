"""多通道通知：PushPlus / Server酱 / Telegram"""

import logging
import time
from pathlib import Path
from typing import List, Optional

import requests

from .downloader import DownloadResult

logger = logging.getLogger("ReleaseMonitor")

RETRY_TIMES = 3
RETRY_DELAY = 15


def _post_with_retry(
    url: str, json_body: dict, channel: str, timeout: int = 10,
) -> Optional[dict]:
    """POST 并解析 JSON，失败重试 RETRY_TIMES 次，返回响应 dict 或 None"""
    for attempt in range(RETRY_TIMES):
        try:
            resp = requests.post(url, json=json_body, timeout=timeout)
            try:
                return resp.json()
            except ValueError:
                logger.warning(
                    f"{channel} 响应非 JSON (HTTP {resp.status_code})，"
                    f"{RETRY_DELAY}s 后重试"
                )
        except requests.RequestException as e:
            logger.warning(f"{channel} 请求失败，{RETRY_DELAY}s 后重试: {e}")
        if attempt < RETRY_TIMES - 1:
            time.sleep(RETRY_DELAY)
    return None


class Notifier:
    """通知基类"""

    def send(self, title: str, content: str) -> bool:
        raise NotImplementedError


class PushPlusNotifier(Notifier):
    """PushPlus 推送"""

    API = "https://www.pushplus.plus/send"

    def __init__(self, token: str, topic: str = ""):
        self.token = token
        self.topic = topic

    def send(self, title: str, content: str) -> bool:
        data = {
            "token": self.token, "title": title,
            "content": content, "template": "html",
        }
        if self.topic:
            data["topic"] = self.topic
        j = _post_with_retry(self.API, data, "PushPlus")
        if j is None:
            return False
        if j.get("code") == 200:
            return True
        # 业务错误（如 token 无效）重试无意义，立即失败并记录原因
        logger.error(f"PushPlus 返回错误: code={j.get('code')} msg={j.get('msg', '')}")
        return False


class ServerChanNotifier(Notifier):
    """Server酱推送"""

    API = "https://sctapi.ftqq.com/{key}.send"

    def __init__(self, key: str):
        self.key = key

    def send(self, title: str, content: str) -> bool:
        url = self.API.format(key=self.key)
        j = _post_with_retry(url, {"title": title, "desp": content}, "Server酱")
        if j is None:
            return False
        if j.get("code") == 0:
            return True
        logger.error(f"Server酱 返回错误: {j}")
        return False


class TelegramNotifier(Notifier):
    """Telegram Bot 推送"""

    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, title: str, content: str) -> bool:
        url = self.API.format(token=self.bot_token)
        text = f"<b>{title}</b>\n{content}"
        j = _post_with_retry(url, {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
        }, "Telegram")
        if j is None:
            return False
        if j.get("ok"):
            return True
        logger.error(f"Telegram 返回错误: {j}")
        return False


def build_download_message(
    repo: str, version: str, results: List[DownloadResult], notes: str,
    published_at: str, apk1_sync: Optional[dict] = None,
    notes_translated: bool = False,
) -> tuple:
    """构建通知消息，返回 (title, html)"""
    ok = sum(1 for r in results if r.success)
    total = len(results)
    size = sum(r.size_mb for r in results)
    url = f"https://github.com/{repo}/releases/tag/{version}"

    files = ""
    for r in results:
        icon = "✅" if r.success else "❌"
        sz = f" ({r.size_mb:.1f} MB)" if r.size_mb > 0 else ""
        err = f" ❌{r.error}" if r.error else ""
        chk = " ✓SHA256" if r.checksum_verified else ""
        display = r.target_name or r.asset_name
        display_path = Path(display)
        tv = ""
        if apk1_sync and display_path.name.lower().endswith(".apk"):
            stem = display_path.stem + ".apk1"
            if stem in apk1_sync.get("copied", []):
                tv = " → TV版 ✓"
            elif stem in apk1_sync.get("expected", []):
                tv = " → TV版 ❌"
        files += f"{icon} {display}{sz}{chk}{err}{tv}<br>"

    tv_line = ""
    if apk1_sync:
        copied_n = len(apk1_sync.get("copied", []))
        expected_n = len(apk1_sync.get("expected", []))
        if expected_n == 0:
            tv_line = "<strong>TV 安装包：</strong> 无 .apk 资产<br>"
        elif copied_n == expected_n:
            tv_line = f"<strong>TV 安装包：</strong> {copied_n}/{expected_n} 已同步<br>"
        else:
            tv_line = (
                f"<strong>TV 安装包：</strong> {copied_n}/{expected_n} 同步失败"
                f"（保留旧版）<br>"
            )

    notes_title = "发布说明（译文）" if notes_translated else "发布说明"
    notes_html = notes.replace("\n", "<br>") if notes else "无"
    body = (
        f"<strong>仓库：</strong> {repo}<br>"
        f"<strong>版本：</strong> {version}<br>"
        f"<strong>下载状态：</strong> {ok}/{total} 文件 ({size:.1f} MB)<br>"
        f"{tv_line}"
        f"<strong>发布时间：</strong> {published_at}<br>"
        f"<strong>链接：</strong> <a href='{url}'>{url}</a><br>"
        "<hr><h3>文件列表</h3>" + files +
        f"<h3>{notes_title}</h3>" + notes_html
    )
    name = repo.split("/")[-1]
    title = f"[Github] {name} {version}"
    return title, body


class NotificationManager:
    """管理多个通知渠道"""

    def __init__(self, notifiers: List[Notifier]):
        self.notifiers = notifiers

    @property
    def enabled(self) -> bool:
        return len(self.notifiers) > 0

    def send_download(
        self, repo: str, version: str,
        results: List[DownloadResult], notes: str, published_at: str,
        apk1_sync: Optional[dict] = None, notes_translated: bool = False,
    ) -> bool:
        """发送下载通知。任一通道成功即整体成功"""
        title, body = build_download_message(
            repo, version, results, notes, published_at,
            apk1_sync=apk1_sync, notes_translated=notes_translated,
        )
        return any(n.send(title, body) for n in self.notifiers)

    def send_test(self) -> dict:
        """发送固定自测消息验证所有通道，返回 {"ok": n, "fail": n}"""
        from datetime import datetime
        title = "[GitHub Release Monitor] 通知通道自测"
        body = (
            f"<strong>时间：</strong> {datetime.now():%Y-%m-%d %H:%M:%S}<br>"
            f"<strong>状态：</strong> 配置验证测试"
        )
        ok = fail = 0
        for n in self.notifiers:
            if n.send(title, body):
                ok += 1
                logger.info(f"通道自测成功: {type(n).__name__}")
            else:
                fail += 1
                logger.error(f"通道自测失败: {type(n).__name__}")
        return {"ok": ok, "fail": fail}