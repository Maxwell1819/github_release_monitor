"""入口 + 编排主循环"""

import argparse
import logging
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from src.config import AppConfig, RepoConfig, load_config
from src.downloader import (
    TEMP_SUFFIX, DownloadResult, Downloader, MirrorManager, verify_sha256,
)
from src.github_api import GitHubClient, HttpClient, match_asset, match_name_regex
from src.notifier import (
    NotificationManager, PushPlusNotifier, ServerChanNotifier, TelegramNotifier,
)
from src.storage import (
    VersionTracker, cleanup_apk1, cleanup_flat_dir, cleanup_version_dirs,
    target_name,
)
from src.translator import Translator
from src.utils import (
    clean_notes, is_security_release, load_dotenv, needs_translation,
)

logger = logging.getLogger("ReleaseMonitor")


class ReleaseMonitor:
    def __init__(self, config: AppConfig, script_dir: Path, dry_run: bool = False):
        self.config = config
        self.script_dir = script_dir
        if dry_run:
            config.dry_run = True

        self._init_logging()

        self.http = HttpClient()
        self.github = GitHubClient(
            self.http, config.github_token, mirrors=config.mirrors,
        )
        self.downloader = Downloader(
            self.http.session,
            speed_threshold_kbps=config.download.speed_threshold_kbps,
            slow_timeout_sec=config.download.slow_timeout_sec,
        )
        self.mirror_mgr = MirrorManager(config.mirrors)
        self.tracker = VersionTracker(script_dir / "version_record.json")

        # 多通道通知
        notifiers = []
        for c in config.notify.channels:
            if c.type == "pushplus" and c.token:
                notifiers.append(PushPlusNotifier(c.token, c.topic))
            elif c.type == "serverchan" and c.key:
                notifiers.append(ServerChanNotifier(c.key))
            elif c.type == "telegram" and c.bot_token and c.chat_id:
                notifiers.append(TelegramNotifier(c.bot_token, c.chat_id))
            else:
                logger.warning(f"忽略无效通知通道: {c.type}")
        self.notif_mgr = NotificationManager(notifiers)
        if self.notif_mgr.enabled:
            logger.info(f"通知通道: {len(notifiers)} 个已启用")

        # 翻译
        tc = config.translation
        self.translator = Translator(tc.app_id, tc.secret_key) if tc.app_id and tc.secret_key else None
        if self.translator:
            logger.info("翻译功能已启用")

        self.stats = {
            "total": 0, "ok": 0, "fail": 0,
            "size_mb": 0.0, "notif_ok": 0, "notif_fail": 0,
        }
        self._last_notif = 0.0
        self._direct_failed = False  # 本轮直连失败记忆：直连失败后后续文件直接走镜像
        self._direct_lock = threading.Lock()
        self._summary: List[str] = []  # 本次运行更新摘要（仓库 + 文件状态）

    def _init_logging(self) -> None:
        level = self.config.logging.get("level", "INFO")
        keep = self.config.logging.get("keep_backup_days", 14)

        log_dir = self.script_dir / "logs"
        log_dir.mkdir(exist_ok=True)

        log_file = log_dir / "github_release_monitor.log"
        if log_file.exists():
            bak = log_dir / "backups"
            bak.mkdir(exist_ok=True)
            now = datetime.now()
            ts = now.strftime("%Y-%m-%d_%H-%M-%S-%f")
            try:
                log_file.rename(bak / f"github_release_monitor_{ts}.log")
            except Exception:
                pass
            cutoff = now - timedelta(days=keep)
            for f in bak.glob("github_release_monitor_*.log"):
                try:
                    m = re.search(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})", f.stem)
                    if m and datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S") < cutoff:
                        f.unlink()
                except Exception:
                    pass

        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        logger.handlers.clear()
        fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    @staticmethod
    def _file_ready(fp: Path, expected: int) -> bool:
        """文件已存在且大小与 release 元数据一致（expected<=0 视为不可校验）"""
        return fp.exists() and (expected <= 0 or fp.stat().st_size == expected)

    def _finalize_tracker(self, key: str) -> None:
        """标记已发现并落盘"""
        self.tracker.mark_seen(key)
        self.tracker.save()

    def _collect_download_queue(
        self, assets: List[dict], target_names: List[str], base: Path, key: str,
    ) -> List[tuple]:
        """构建下载队列：跳过已存在且大小一致的文件"""
        queue = []
        for a, tname in zip(assets, target_names):
            fp = base / tname
            if self._file_ready(fp, a.get("size", 0)):
                logger.info(f"文件已存在: {tname}")
                self.tracker.update_file(key, tname, True)
                continue
            queue.append((a, fp))
        return queue

    def _sync_apk1(
        self, cfg: RepoConfig, assets: List[dict], target_names: List[str],
        file_dir: Path, key: str, version: str,
    ) -> Optional[dict]:
        """复制改名 .apk1，返回通知用 apk1_info（未开启时返回 None）。
        输出位置：apk_rename_output 配置时平铺到该目录；
        否则与 .apk 同目录（keep_versions==1 平铺仓库根目录、子目录模式放版本目录）。
        仅当全部 .apk 资产复制成功时更新记录并清理超量旧 apk1"""
        if not cfg.rename_apk1:
            return None
        expected_names = []
        apk_files = []
        for a, tname in zip(assets, target_names):
            if not tname.lower().endswith(".apk"):
                continue
            expected_names.append(Path(tname).stem + ".apk1")
            fp = file_dir / tname
            if self._file_ready(fp, a.get("size", 0)):
                apk_files.append(fp)
        if not expected_names:
            return None
        out = self.config.apk_rename_output or file_dir
        out.mkdir(parents=True, exist_ok=True)
        new_names = []
        for src in apk_files:
            dst = out / (src.stem + ".apk1")
            try:
                shutil.copy2(src, dst)
                logger.info(f"复制改名: {src.name} -> {dst}")
                new_names.append(dst.name)
            except OSError as e:
                logger.error(f"复制失败 {src.name}: {e}")
        if len(new_names) == len(expected_names):
            self.tracker.set_apk1(key, version, new_names)
            self.tracker.save()
            logger.info(f"APK1 同步: {len(new_names)} 个文件")
        else:
            logger.warning(
                f"本次 {len(expected_names) - len(new_names)} 个 .apk 资产未复制成功，"
                f"保留旧 .apk1 文件"
            )
            return {"expected": expected_names, "copied": new_names}
        # 清理超出 keep_versions 的旧版本 apk1：
        # 自定义路径模式按 keep_versions 留存；平铺模式（keep_versions==1）只留最新。
        # 子目录模式（≥2/0）apk1 在版本目录内，随版本目录一起清理，无需单独处理
        if self.config.apk_rename_output or cfg.keep_versions == 1:
            cleanup_apk1(
                out, self.tracker.get_apk1(key), cfg.keep_versions,
                current_names=new_names,
            )
        # 修剪映射：避免 version_record.json 无限累积历史版本记录
        self.tracker.prune_apk1(key, cfg.keep_versions)
        self.tracker.save()
        return {"expected": expected_names, "copied": new_names}

    def _apply_mtime(self, asset: dict, file_path: Path) -> None:
        """将文件修改时间设为 release 资产发布时间（失败忽略）"""
        ts = asset.get("updated_at") or asset.get("created_at")
        if not ts:
            return
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            os.utime(file_path, (t, t))
        except (ValueError, TypeError, OSError):
            pass

    def _cleanup_versions(self, cfg: RepoConfig, base: Path, keep_names: List[str]) -> None:
        """按 keep_versions 清理旧版本文件/目录"""
        if cfg.keep_versions == 1:
            cleanup_flat_dir(base, keep_names)
        else:
            cleanup_version_dirs(base, cfg.keep_versions)

    def _download_file(
        self, asset: dict, target: Path, repo_key: str,
        all_assets: List[dict], lock: threading.Lock,
    ) -> DownloadResult:
        name = asset.get("name", "")
        orig_url = asset.get("browser_download_url", "")
        errors: List[str] = []

        # 直连优先（本轮已失败过则跳过直连直接走镜像）
        with self._direct_lock:
            direct_skipped = self._direct_failed
        if not direct_skipped:
            result = self.downloader.download(orig_url, target, name, token=self.config.github_token)
            err_msg = result.error or "未知错误"
            if result.success:
                logger.info(f"直连下载成功: {name}")
                result.target_name = target.name
                return self._finish_download(asset, result, repo_key, all_assets, lock)
            errors.append(f"直连: {err_msg}")
            with self._direct_lock:
                self._direct_failed = True
            logger.info(f"直连失败 ({err_msg})，本轮后续文件将直接使用镜像: {name}")
        else:
            logger.info(f"直连已失败过，直接使用镜像下载: {name}")
            result = None

        # 镜像下载：两轮循环（每轮按序切换全部镜像），全败才回直连兜底
        if self.mirror_mgr.enabled:
            for rnd in range(2):
                self.mirror_mgr.reset()
                for i in range(len(self.mirror_mgr.mirrors)):
                    if i > 0:
                        self.mirror_mgr.switch()
                    mirror_url = self.mirror_mgr.translate(orig_url)
                    logger.info(
                        f"镜像 [{i + 1}/{len(self.mirror_mgr.mirrors)}]"
                        f" 第{rnd + 1}轮: {self.mirror_mgr.current()}"
                    )
                    # 镜像无需 GitHub token（第三方代理不认识，泄露无益）
                    result = self.downloader.download(mirror_url, target, name, token="")
                    if result.success:
                        result.target_name = target.name
                        return self._finish_download(asset, result, repo_key, all_assets, lock)
                    errors.append(f"镜像{rnd + 1}-{i + 1}: {result.error or '未知错误'}")
                logger.warning(f"镜像第{rnd + 1}轮全部失败")
        else:
            result = None

        # 两轮镜像全失败 → 回直连兜底 1 次（镜像可能只是临时故障）
        if result is None:
            if self.mirror_mgr.enabled:
                logger.warning("所有镜像失败，回直连重试 1 次")
            else:
                logger.warning("未配置镜像，直连重试 1 次")
            result = self.downloader.download(
                orig_url, target, name, token=self.config.github_token,
            )
        if result.success:
            result.target_name = target.name
            return self._finish_download(asset, result, repo_key, all_assets, lock)
        errors.append(f"直连重试: {result.error or '未知错误'}")
        logger.error(f"全部下载源失败: {'; '.join(errors)}")

        result.target_name = target.name
        return self._finish_download(asset, result, repo_key, all_assets, lock)

    def _finish_download(
        self, asset: dict, result: DownloadResult, repo_key: str,
        all_assets: List[dict], lock: threading.Lock,
    ) -> DownloadResult:
        """下载完成后统一收尾：SHA256 校验、mtime 同步、状态记录"""
        # SHA256 校验
        if result.success and result.file_path:
            ok, err = verify_sha256(
                Path(result.file_path), all_assets, self.http.session,
                self.config.github_token, source_name=asset.get("name", ""),
            )
            result.checksum_verified = ok
            if not ok and err != "无 checksum 文件":
                result.success = False
                result.error = err

        # mtime 同步
        if result.success and result.file_path:
            self._apply_mtime(asset, Path(result.file_path))

        with lock:
            self.tracker.update_file(repo_key, result.target_name, result.success)
        return result

    def _process_repo(self, cfg: RepoConfig) -> None:
        repo = cfg.repo
        if not repo:
            return
        key = repo.replace("/", "_")
        name = repo.split("/", 1)[1]
        base = self.config.download_path / key

        logger.info(f"处理仓库: {repo}")

        # 获取 release（带 ETag）
        cached_etag = self.tracker.get_etag(key)
        release, new_etag, not_modified = self.github.fetch_latest_release(
            repo,
            release_type=cfg.release_type,
            etag=cached_etag,
            tag_regex=cfg.tag_regex,
        )

        if not_modified:
            logger.debug(f"已是最新: {repo}")
            return

        if not release:
            logger.warning(f"未找到 release: {repo}")
            return

        version = release.get("tag_name", "")
        if not version:
            logger.warning(f"release 缺少 tag: {repo}")
            return

        # 过滤匹配文件（files 规则 + 文件名正则）
        assets = [
            a for a in release.get("assets", [])
            if match_asset(a, cfg.files)
            and match_name_regex(a.get("name", ""), cfg.include_regex, cfg.exclude_regex, repo)
        ]
        if not assets:
            logger.warning(f"无匹配文件: {repo} {version}")
            return

        target_names = [
            target_name(a["name"], version, name, cfg.rename_with_repo)
            for a in assets
        ]

        # 检查版本记录
        rec = self.tracker.get(key)
        new_version = True
        if rec and rec.get("version") == version:
            if self.tracker.is_complete(key, target_names):
                logger.info(f"已是最新: {repo} {version}")
                if new_etag:
                    self.tracker.set(key, version, etag=new_etag)
                    self.tracker.save()
                return
            logger.info(f"补充下载未完成文件: {repo} {version}")
            new_version = False
        else:
            logger.info(f"发现新版本: {repo} {version}")

        # 安全版本检测
        body_raw = release.get("body") or ""
        is_security = is_security_release(version, body_raw, release.get("name", ""))
        if is_security:
            logger.warning(f"⚠ 安全更新: {repo} {version}")

        body = clean_notes(body_raw)

        # 首次运行静默
        first_seen = self.tracker.is_first_seen(key)

        # 该版本此前是否已有文件成功下载：补充下载（new_version=False）但
        # 该版本从未成功下载过（如 dry-run 先验证再真实运行）时按首次真实下载通知
        had_success_before = bool(
            rec
            and rec.get("version") == version
            and any(rec.get("files", {}).get(n) for n in target_names)
        )

        if self.config.dry_run:
            logger.info(f"[DRY-RUN] 将保存: RELEASE_NOTES_{name}_{version}.md")
            logger.info(f"[DRY-RUN] 将下载: {', '.join(target_names)}")
            logger.info(f"[DRY-RUN] 目录: {base}")
            logger.info(f"[DRY-RUN] ETag: {new_etag}")
            if first_seen:
                logger.info("[DRY-RUN] 首次发现，跳过通知")
            if is_security:
                logger.info("[DRY-RUN] 安全更新")
            self.tracker.set(key, version, etag=new_etag)
            self._finalize_tracker(key)
            return

        # 目录结构：keep_versions==1 平铺仓库根目录；否则版本子目录
        if cfg.keep_versions == 1:
            file_dir = base
        else:
            file_dir = base / version
        file_dir.mkdir(parents=True, exist_ok=True)

        # 保存 release notes
        (file_dir / f"RELEASE_NOTES_{name}_{version}.md").write_text(
            f"# {name} {version}\n\n"
            f"发布时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n{body or '无发布说明'}",
            encoding="utf-8",
        )

        self.tracker.set(key, version, etag=new_etag, clear_files=True)

        # 跳过已存在的文件
        download_queue = self._collect_download_queue(assets, target_names, file_dir, key)

        results: List[DownloadResult] = []

        if download_queue:
            logger.info(f"开始下载 {len(download_queue)} 个文件")
            lock = threading.Lock()
            with ThreadPoolExecutor(max_workers=self.config.download.max_concurrent) as ex:
                fut = {
                    ex.submit(self._download_file, a, p, key, assets, lock): a
                    for a, p in download_queue
                }
                for f in as_completed(fut):
                    r = f.result()
                    results.append(r)
                    if r.success:
                        self.stats["ok"] += 1
                        self.stats["size_mb"] += r.size_mb
                    else:
                        self.stats["fail"] += 1
        else:
            for a, tname in zip(assets, target_names):
                results.append(DownloadResult(
                    asset_name=a["name"], success=True, target_name=tname,
                ))

        # 复制改名 .apk1（可选）
        apk1_info = self._sync_apk1(cfg, assets, target_names, file_dir, key, version)

        # 版本清理（keep_versions）——仅当本次存在成功下载时才清理，
        # 全部失败时保留旧版本文件（下载成功后才删）
        if any(r.success for r in results):
            self._cleanup_versions(
                cfg, base,
                [*target_names, f"RELEASE_NOTES_{name}_{version}.md"],
            )

        self._finalize_tracker(key)

        # 通知（首次发现跳过；补充下载但此前从未成功下载过视为首次真实下载）
        if not first_seen and (new_version or not had_success_before) and self.notif_mgr.enabled:
            self._send_notification(
                repo, version, body, results, apk1_info,
                release.get("published_at") or "",
            )
        elif first_seen:
            logger.info(f"首次发现 {repo}，跳过通知")

        self.stats["total"] += len(assets)

        # 更新摘要（便于查阅：每仓库成功/失败文件）
        for r in results:
            status = "✅" if r.success else f"❌({r.error or '未知错误'})"
            self._summary.append(f"  {repo} {version} | {r.target_name or r.asset_name} {status}")

    def _send_notification(
        self, repo: str, version: str, body: str,
        results: List[DownloadResult], apk1_info: dict, published_at: str,
    ) -> None:
        wait = max(0, self.config.notify.min_interval_sec - (time.time() - self._last_notif))
        if wait > 0:
            logger.info(f"等待 {wait:.0f}s 避免推送频率限制...")
            time.sleep(wait)

        notes_text = body
        notes_translated = False
        if self.config.notify.translate_notes and self.translator and notes_text \
                and needs_translation(notes_text):
            logger.info("检测到非中文发布说明，开始翻译...")
            translated = self.translator.translate(notes_text)
            if translated:
                notes_text = translated
                notes_translated = True
            else:
                logger.warning("翻译失败，使用原文")

        self._last_notif = time.time()
        ok = self.notif_mgr.send_download(
            repo, version, results, notes_text, published_at,
            apk1_sync=apk1_info, notes_translated=notes_translated,
        )
        if ok:
            self.stats["notif_ok"] += 1
        else:
            self.stats["notif_fail"] += 1

    def run(self) -> None:
        start = datetime.now()
        logger.info(f"Release Monitor 启动 - {start:%Y-%m-%d %H:%M:%S}")
        if self.config.dry_run:
            logger.info("=== DRY-RUN 模式 ===")

        try:
            self._clean_partials()
            self._direct_failed = False  # 每轮运行重新探测直连

            if not self.config.repos:
                logger.warning("未配置仓库")
                return

            for rc in self.config.repos:
                try:
                    self._process_repo(rc)
                except Exception as e:
                    logger.error(f"处理仓库异常: {e}")

            elapsed = (datetime.now() - start).total_seconds()
            summary_lines = "\n".join(self._summary) if self._summary else "  无更新"
            logger.info(
                f"{'=' * 10} 统计 {'=' * 10}\n"
                f"文件: {self.stats['ok']}/{self.stats['total']} 成功, "
                f"{self.stats['fail']} 失败\n"
                f"大小: {self.stats['size_mb']:.1f} MB\n"
                f"通知: {self.stats['notif_ok']} 成功, {self.stats['notif_fail']} 失败\n"
                f"耗时: {elapsed:.1f}s\n"
                f"{'=' * 6} 本次更新 {'=' * 6}\n{summary_lines}"
            )
        finally:
            self.http.close()
            logger.info("HTTP Session 已关闭")

    def _clean_partials(self) -> None:
        """清理上次异常中断残留的 .part 临时文件"""
        try:
            for f in self.config.download_path.rglob(f"*{TEMP_SUFFIX}"):
                f.unlink(missing_ok=True)
        except (PermissionError, OSError):
            logger.warning("无权访问下载目录，跳过清理临时文件")


def _daemonize(script_dir: Path) -> None:
    """守护化：--daemon 模式下父进程拉起独立会话子进程后立即退出，
    避免 fnOS 计划任务 900s 超时掐杀；flock 防重入"""
    import fcntl
    import os
    import subprocess

    if os.environ.get("GHM_DAEMON_CHILD") == "1":
        return
    if "--daemon" not in sys.argv:
        return

    log_dir = script_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    lock_fd = os.open(log_dir / ".monitor.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("已有实例在运行,跳过本次触发", flush=True)
        sys.exit(0)

    env = dict(os.environ, GHM_DAEMON_CHILD="1")
    args = [a for a in sys.argv if a != "--daemon"]
    with open(log_dir / "stdout.log", "ab") as out:
        subprocess.Popen(
            [sys.executable, str(script_dir / "main.py"), *args],
            env=env,
            start_new_session=True,
            pass_fds=(lock_fd,),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    print("任务已在后台启动,日志: logs/github_release_monitor.log", flush=True)
    sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub Release Monitor")
    parser.add_argument("--dry-run", action="store_true", help="只检查不下载不通知")
    parser.add_argument("--test-notify", action="store_true", help="发送测试通知")
    parser.add_argument("--daemon", action="store_true", help="守护化运行（计划任务用）")
    args, _ = parser.parse_known_args()

    script_dir = Path(__file__).parent.absolute()
    _daemonize(script_dir)

    config_path = script_dir / "config.json"
    if not config_path.exists():
        example = script_dir / "config.example.json"
        print(f"配置文件不存在: {config_path}")
        if example.exists():
            print(f"请复制模板后编辑: cp {example} {config_path}")
        sys.exit(1)

    load_dotenv(script_dir / ".env")

    config = load_config(config_path)
    if args.dry_run:
        config.dry_run = True

    monitor = ReleaseMonitor(config, script_dir)
    monitor.github.validate_token()

    if args.test_notify:
        result = monitor.notif_mgr.send_test()
        print(f"通知自测: {result['ok']} 成功, {result['fail']} 失败")
        sys.exit(0 if result["fail"] == 0 else 1)

    monitor.run()


if __name__ == "__main__":
    main()