"""断点续传下载器 + 镜像加速 + 速度监控 + SHA256 校验"""

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import requests

from .utils import fmt_size, sha256sum

logger = logging.getLogger("ReleaseMonitor")

TEMP_SUFFIX = ".part"

# 接近完成时不再切换下载源：已下载比例或剩余字节阈值
NEARLY_DONE_RATIO = 0.9
NEARLY_DONE_MIN_LEFT = 10 * 1024 * 1024  # 10MB

# ghproxy 类镜像域名（前缀格式 {host}/https://github.com/...），其余替换 host
_PROXY_DOMAINS = {
    "ghproxy.net", "ghproxy.com", "gh.api.99988866.xyz",
    "ghfast.top", "gh-proxy.com", "ghproxy.cxkpro.top",
}


@dataclass
class DownloadResult:
    asset_name: str
    success: bool = False
    file_path: Optional[str] = None
    size_mb: float = 0.0
    error: Optional[str] = None
    checksum_verified: bool = False
    target_name: str = ""


class MirrorManager:
    """镜像管理：顺序切换 + URL 翻译（ghproxy 类前缀、其他替换 host）"""

    def __init__(self, mirrors: List[str]):
        self.mirrors = mirrors
        self._idx = 0

    @property
    def enabled(self) -> bool:
        return len(self.mirrors) > 0

    def current(self) -> Optional[str]:
        return self.mirrors[self._idx] if self.mirrors else None

    def switch(self) -> None:
        if self.mirrors:
            self._idx = (self._idx + 1) % len(self.mirrors)

    def reset(self) -> None:
        self._idx = 0

    def translate(self, original_url: str) -> str:
        if not self.mirrors:
            return original_url
        host = self.mirrors[self._idx]
        domain = host.split("/")[2] if "//" in host else host
        if domain in _PROXY_DOMAINS:
            return f"{host}/{original_url}"
        return host + original_url.replace("https://github.com", "")


class Downloader:
    """断点续传下载器"""

    def __init__(
        self, session: requests.Session,
        speed_threshold_kbps: int = 1024,
        slow_timeout_sec: int = 10,
    ):
        self.session = session
        self.speed_threshold = speed_threshold_kbps * 1024
        self.slow_timeout = slow_timeout_sec

    def download(
        self, url: str, target: Path, name: str, token: str = "",
    ) -> DownloadResult:
        result = DownloadResult(asset_name=name)
        local_size = 0
        size_mismatch_retried = False
        part_path = target.with_suffix(TEMP_SUFFIX)
        resume_candidate = self._resume_candidate(target) or self._resume_candidate(part_path)

        for attempt in range(6):
            hdrs = {}
            if token:
                hdrs["Authorization"] = f"token {token}"

            if resume_candidate is not None:
                local_size = self._resume_size(resume_candidate)
                if local_size is None:
                    # 残留文件已被清理（并发下载），从头开始
                    resume_candidate = None
                    local_size = 0
                else:
                    hdrs["Range"] = f"bytes={local_size}-"
                    logger.info(f"断点续传 {name} ({fmt_size(local_size)})")

            write_path = resume_candidate if resume_candidate is not None else part_path
            write_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                resp = self.session.get(url, headers=hdrs, stream=True, timeout=(10, 20))
            except Exception as e:
                logger.error(f"下载请求失败 {name}: {e}")
                result.error = str(e)
                return result

            ct = resp.headers.get("Content-Type", "")
            if "text/html" in ct.lower():
                if "Range" in hdrs:
                    # 服务器不支持断点续传，放弃部分文件从头下载
                    logger.warning(f"服务器不支持续传，从头重新下载: {name}")
                    self._discard_partial(target)
                    local_size = 0
                    resume_candidate = None
                    continue
                result.error = f"返回 HTML 页面 ({ct})"
                logger.error(f"下载返回错误页面: {name}")
                return result

            if "Range" in hdrs and resp.status_code in (416, 200):
                logger.warning(f"Range 无效或服务器不支持续传，重新下载: {name}")
                self._discard_partial(target)
                local_size = 0
                resume_candidate = None
                continue

            if resp.status_code >= 400:
                result.error = f"HTTP {resp.status_code}"
                return result

            total = self._total_size(resp, local_size)
            if total > 0 and local_size >= total:
                logger.info(f"文件已完整: {name}")
                result.success = True
                result.file_path = str(target)
                try:
                    result.size_mb = target.stat().st_size / (1024 * 1024)
                except OSError:
                    result.size_mb = local_size / (1024 * 1024)
                return result

            ok, too_slow = self._write_stream(resp, write_path, local_size, total, name)
            if not ok:
                if too_slow:
                    result.error = "下载速度过慢"
                    return result
                delay = min(2 ** attempt * 2, 60) * random.uniform(0.8, 1.2)
                logger.warning(f"下载中断，{delay:.0f}s 后重试")
                time.sleep(delay)
                resume_candidate = self._resume_candidate(part_path) or self._resume_candidate(target)
                if resume_candidate is not None:
                    local_size = self._resume_size(resume_candidate)
                    if local_size is None:
                        resume_candidate = None
                        local_size = 0
                continue

            try:
                actual_size = write_path.stat().st_size
                if total > 0 and actual_size != total:
                    self._safe_delete(write_path)
                    if not size_mismatch_retried:
                        # CDN 偶发返回异常内容，删 .part 从头重试 1 次
                        size_mismatch_retried = True
                        logger.warning(
                            f"{name} 大小不匹配 ({actual_size}/{total})，从头重试 1 次")
                        resume_candidate = None
                        local_size = 0
                        continue
                    result.error = "文件大小不匹配"
                    logger.error(f"{name} 大小不匹配 ({actual_size}/{total})")
                    return result

                if write_path.suffix == TEMP_SUFFIX:
                    write_path.rename(target)
                elif part_path.exists():
                    part_path.unlink(missing_ok=True)
            except OSError:
                # 完成阶段部分文件被并发流程清理（如另一下载的降级），安全重试
                logger.warning(f"完成阶段文件异常，重新下载: {name}")
                resume_candidate = None
                local_size = 0
                continue

            result.success = True
            result.file_path = str(target)
            try:
                result.size_mb = target.stat().st_size / (1024 * 1024)
                logger.info(f"下载完成: {name} ({fmt_size(target.stat().st_size)})")
            except OSError:
                pass
            return result

        result.error = "下载失败"
        return result

    def _write_stream(
        self, resp: requests.Response, path: Path,
        offset: int, total: int, name: str,
    ) -> Tuple[bool, bool]:
        """写入响应流。返回 (是否成功, 是否因速度过慢中止)"""
        mode = "ab" if offset > 0 and resp.status_code == 206 else "wb"
        downloaded = offset
        last_log = time.time()
        last_size = downloaded
        last_pct = -1
        slow_start: Optional[float] = None

        try:
            with open(path, mode) as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    if now - last_log >= 2:
                        chunk_bytes = downloaded - last_size
                        elapsed = now - last_log
                        speed_bps = chunk_bytes / elapsed if elapsed > 0 else 0
                        # 进度每跨过 10% 输出一次（大文件不刷屏）
                        pct = downloaded * 100 // total if total > 0 else 0
                        if pct != last_pct:
                            last_pct = pct
                            self._log_progress(name, downloaded, total, speed_bps)
                        ok, slow_start = self._check_speed(speed_bps, slow_start, downloaded, total)
                        if not ok:
                            return False, True
                        last_log = now
                        last_size = downloaded
            return True, False
        except requests.exceptions.Timeout:
            logger.warning("下载连接无响应，可能已挂死")
            return False, True
        except Exception as e:
            logger.warning(f"写入文件失败: {e}")
            return False, False

    def _log_progress(self, name: str, downloaded: int, total: int, speed_bps: float) -> None:
        speed = speed_bps / (1024 * 1024)
        if total > 0:
            logger.info(
                f"[下载] {name} | {downloaded * 100 // total}% "
                f"| {fmt_size(downloaded)} | {speed:.1f} MB/s"
            )
        else:
            logger.info(f"[下载] {name} | {fmt_size(downloaded)} | {speed:.1f} MB/s")

    def _check_speed(
        self, speed_bps: float, slow_start: Optional[float],
        downloaded: int = 0, total: int = 0,
    ) -> Tuple[bool, Optional[float]]:
        """返回 (是否继续, 更新后的 slow_start)。慢速状态局部化，并发下载互不干扰。
        已下载 ≥90% 或剩余 <10MB 时即使慢速也不切源（快下完了，切源反而更慢）"""
        if speed_bps < self.speed_threshold:
            if total > 0 and (
                downloaded >= total * NEARLY_DONE_RATIO
                or total - downloaded < NEARLY_DONE_MIN_LEFT
            ):
                return True, slow_start
            if slow_start is None:
                slow_start = time.time()
            elif time.time() - slow_start > self.slow_timeout:
                logger.warning(f"下载速度持续过慢 ({speed_bps / 1024:.0f} KB/s)，切换下载源")
                return False, slow_start
        else:
            slow_start = None
        return True, slow_start

    def _resume_candidate(self, target: Path) -> Optional[Path]:
        if not target.exists():
            return None
        size = target.stat().st_size
        return target if size >= 1024 * 1024 else None

    @staticmethod
    def _resume_size(path: Path) -> Optional[int]:
        try:
            return path.stat().st_size
        except OSError:
            return None

    @staticmethod
    def _total_size(resp: requests.Response, offset: int) -> int:
        if resp.status_code == 206:
            cr = resp.headers.get("Content-Range", "")
            if "/" in cr:
                try:
                    return int(cr.rsplit("/", 1)[-1])
                except ValueError:
                    pass
        try:
            return int(resp.headers.get("Content-Length", 0)) + offset
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _safe_delete(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    @classmethod
    def _discard_partial(cls, target: Path) -> None:
        """删除目标文件与残留的部分下载文件"""
        cls._safe_delete(target)
        cls._safe_delete(target.with_suffix(TEMP_SUFFIX))


def verify_sha256(
    file_path: Path, assets: List[dict], session: requests.Session, token: str = "",
    source_name: str = "",
) -> Tuple[bool, Optional[str]]:
    """校验 SHA256：从 release assets 中查找 .sha256 文件。
    source_name: 文件名被重命名前的原始 asset 名（用于匹配 checksum 资产）"""
    stems = [file_path.stem.lower()]
    if source_name:
        stems.append(Path(source_name).stem.lower())
    for asset in assets:
        name = asset.get("name", "").lower()
        if not (name.endswith(".sha256") or name.endswith(".sha256sum")):
            continue
        chk_name = name.replace(".sha256sum", "").replace(".sha256", "")
        if not any(chk_name == s or chk_name.startswith(s + ".") for s in stems):
            continue
        url = asset.get("browser_download_url", "")
        if not url:
            continue
        headers = {"Authorization": f"token {token}"} if token else {}
        try:
            resp = session.get(url, headers=headers, timeout=30)
        except Exception as e:
            logger.warning(f"下载 checksum 失败，跳过校验: {e}")
            return True, None
        if resp.status_code != 200:
            logger.warning(f"下载 checksum 失败 (HTTP {resp.status_code})，跳过校验")
            return True, None
        content = resp.text.strip()
        expected = content.split()[0] if " " in content else content
        if len(expected) != 64:
            return False, "无效 SHA256 格式"
        actual = sha256sum(file_path)
        if actual.lower() != expected.lower():
            file_path.unlink(missing_ok=True)
            return False, "SHA256 不匹配"
        return True, None
    return False, "无 checksum 文件"