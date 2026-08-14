# GitHub Release Monitor

A lightweight self-hosted GitHub Release monitor & downloader: polls the latest release → filters assets by rules → downloads with resume (auto mirror fallback when direct access fails) → optionally renames / copies to `.apk1` → sends multi-channel notifications (with Baidu Translate for non-English release notes).

Only dependency is `requests`; Python 3.8+. Built for NAS / FeiQun OS / Linux cron jobs.

> **中文版本：[README.md](README.md)**

---

## Features

| Module | Description |
|---|---|
| **Monitor** | Polls the latest release of every repo in `repos[]`; ETag caching avoids redundant requests |
| **Filter** | Release type (`latest` / `prerelease`) + `tag_regex` to lock a release line + keyword / extension / regex asset filters |
| **Download** | Direct → mirror accelerator (two cycles) → direct fallback; resume, slow-speed auto-switch, no-switch when nearly done; SHA256 verification |
| **Rename** | `rename_with_repo` prepends `{project}-{filename}-{version}`; `rename_apk1` copies `.apk` as `.apk1` (TV installers) |
| **Notify** | PushPlus / ServerChan / Telegram multi-channel, any success = success; auto-translates non-English release notes |
| **State** | Version / ETag / file state persisted; silent on first discovery (no historical flood at deploy); log auto-rotation |

## Quick Start

```bash
git clone https://github.com/Maxwell819/github_release_monitor.git
cd github_release_monitor

pip install requests        # the only dependency

cp .env.example .env        # fill in GitHub token / push tokens / Baidu keys
chmod 600 .env
cp config.example.json config.json   # edit repo list & download path
```

Run:

```bash
python3 main.py --dry-run      # check only: verify config, network, asset matching
python3 main.py --test-notify  # send a test message to all channels
python3 main.py                # full run
```

## CLI

| Argument | Description |
|---|---|
| (none) | Full run: check → download → notify |
| `--dry-run` | Check only, no download / notify (state still written) |
| `--test-notify` | Send a test message to verify channels |
| `--daemon` | Daemonize: flock single-instance; parent exits instantly, child keeps running |

### NAS Scheduled Task (FeiQun / Synology)

Scheduled tasks often have a 900s timeout — use `--daemon` to bypass it:

```
/usr/bin/python3 /volume1/Scripts/github_release_monitor/main.py --daemon
```

## Configuration

All settings live in `config.json` (template: `config.example.json`). Every string supports `${VAR}` resolution from `.env`.

### Top-Level Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `github_token` | string | `""` | GitHub token (raises limit 60 → 5000 req/h); commonly `${GITHUB_TOKEN}` |
| `download_path` | string | `./downloads` | Download root; each repo gets `owner_repo/` subdir |
| `apk_rename_output` | string | `null` | Custom `.apk1` output dir; `null` = same dir as `.apk` |
| `mirrors` | list | `[]` | Mirror accelerator domains (ghproxy-style), polled in order |
| `dry_run` | bool | `false` | Global dry-run switch (CLI `--dry-run` overrides) |
| `repos` | list | `[]` | Monitored repo list (core config) |

### download

| Field | Default | Description |
|---|---|---|
| `max_concurrent` | `3` | Concurrent downloads |
| `speed_threshold_kbps` | `1024` | Slow-speed threshold (KB/s); below it for `slow_timeout_sec` → switch source |
| `slow_timeout_sec` | `10` | Seconds of slow speed before switching; `0` disables |

### notify

| Field | Default | Description |
|---|---|---|
| `channels` | `[]` | Channel list, see table below |
| `min_interval_sec` | `12` | Minimum interval between notifications (anti rate-limit) |
| `translate_notes` | `true` | Translate non-English release notes via Baidu |

**Channel types**:

| type | Required fields | Optional |
|---|---|---|
| `pushplus` | `token` | `topic` (group push) |
| `serverchan` | `key` | — |
| `telegram` | `bot_token`, `chat_id` | — |

Any channel succeeding means overall success; channels don't affect each other.

### baidu_translate (optional)

| Field | Description |
|---|---|
| `appid` | Baidu Translate AppID (https://api.fanyi.baidu.com) |
| `secret` | Secret key. Both required to enable translation |

### logging

| Field | Default | Description |
|---|---|---|
| `level` | `"INFO"` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `keep_backup_days` | `14` | Retention days for archived logs |

### repos[] (Repo Entry)

| Field | Type | Default | Description |
|---|---|---|---|
| `repo` | string | required | `owner/name`, e.g. `alist-org/alist` |
| `files` | list | required | Asset matching rules; any rule hit → download |
| `release_type` | string | `"latest"` | `latest` stable only / `prerelease` includes pre-releases |
| `tag_regex` | string | `null` | Regex on the release **tag name** (for multi-line repos, e.g. `desktop`) |
| `include_regex` | string | `null` | Asset **filename** regex whitelist |
| `exclude_regex` | string | `null` | Asset filename regex blacklist (takes priority) |
| `rename_with_repo` | bool | `false` | Prepend `{project}-{filename}-{version}` |
| `rename_apk1` | bool | `false` | Copy `.apk` as `.apk1` into `apk_rename_output` |
| `keep_versions` | int | `1` | `0` keep all / `1` flat latest-only / `≥2` versioned subdirs keeping N |

**files matching** (a rule must satisfy all):
- `keywords`: filename must contain **every** keyword (lowercased)
- `extensions`: ends with any listed extension, or `"all"`
- Assets >1GB auto-skipped

## Full Example

`config.example.json` ships 6 classic project examples covering every parameter:

| Example | Demonstrates |
|---|---|
| `alist-org/alist` | multi-rule OR in `files`, `exclude_regex` |
| `termux/termux-app` | `prerelease`, `rename_apk1`, `rename_with_repo` |
| `BtbN/FFmpeg-Builds` | multi-keyword AND, `include_regex` + `exclude_regex` combo |
| `starship/starship` | `keep_versions: 2` versioned subdirs |
| `rclone/rclone` | `include_regex` for arch, `keep_versions: 0` |
| `esengine/DeepSeek-Reasonix` | `tag_regex` locking the desktop line |

## Directory Layout

```
{download_path}/{owner_repo}/            keep_versions=1 flat
{download_path}/{owner_repo}/{version}/  keep_versions≥2 versioned subdirs
{apk_rename_output}/*.apk1               apk1 output (TV installers)
logs/github_release_monitor.log          run log (auto-archived on start)
version_record.json                      version / ETag / file state
```

## FAQ

**No notification on first deploy?** First discovery of each repo is silent (anti history-flood); notifications start from the second run.

**Downloads slow?** Put the fastest mirror first in `mirrors`; direct access blocked in your region → auto mirror fallback. If you have a proxy, put its address in `.env` (e.g. `HTTPS_PROXY=http://192.168.1.1:7890`).

**Scheduled task times out?** Use `--daemon`: parent exits instantly, child continues in background, flock prevents duplicates.

**Want to monitor a repo that only publishes tags (no releases)?** Not supported — please open an issue.

## Tests

```bash
/usr/bin/python3 -m pytest tests/ -q
```

93 tests covering API client, downloader (resume / mirror switch / slow-speed policy), storage cleanup, translation segmentation, notification retry, and main-flow integration.

## License

[MIT](LICENSE)
