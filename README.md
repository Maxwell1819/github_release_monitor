# GitHub Release Monitor

轻量自托管 GitHub Release 监控下载器：定时检查最新 release → 按规则筛选资产 → 断点续传下载（直连失败自动走镜像加速）→ 可选改名 / 复制为 `.apk1` → 多通道推送通知（支持百度翻译 release notes）。

仅依赖 `requests`，Python 3.8+，专为 NAS / 飞牛 OS / Linux 计划任务设计。

> **English version: [README_EN.md](README_EN.md)**

---

## 特性

| 模块 | 说明 |
|---|---|
| **监控** | 定时检查 `repos[]` 所有仓库的最新 release，ETag 缓存避免重复请求 |
| **筛选** | release 类型（latest / prerelease）+ `tag_regex` 锁定发布线 + 关键词 / 扩展名 / 正则过滤资产 |
| **下载** | 直连 → 镜像加速站两轮循环 → 直连兜底；断点续传、慢速自动切源、接近完成不切源；SHA256 校验 |
| **改名** | `rename_with_repo` 加 `{项目}-{文件名}-{版本}` 前缀；`rename_apk1` 复制 `.apk` 为 `.apk1`（TV 装机） |
| **通知** | PushPlus / Server酱 / Telegram 多通道，任一成功即成功；非中文 release notes 自动百度翻译 |
| **状态** | 版本 / ETag / 文件状态持久化；首次发现静默（避免部署时历史版本轰炸）；日志自动轮转清理 |

## 快速开始

```bash
git clone https://github.com/Maxwell819/github_release_monitor.git
cd github_release_monitor

pip install requests        # 唯一依赖

cp .env.example .env        # 填入 GitHub Token / 推送 Token / 百度翻译密钥
chmod 600 .env
cp config.example.json config.json   # 编辑监控仓库列表与下载路径
```

运行：

```bash
python3 main.py --dry-run      # 只检查不下载：验证配置、网络、资产匹配
python3 main.py --test-notify  # 向所有通道发测试消息
python3 main.py                # 正式运行一次
```

## 命令行

| 参数 | 作用 |
|---|---|
| （无） | 完整运行一次：检查 → 下载 → 通知 |
| `--dry-run` | 只检查不下载不通知（状态照常写入） |
| `--test-notify` | 发送自测消息，验证通知通道 |
| `--daemon` | 守护化：flock 防重入，父进程秒退、子进程后台续跑 |

### NAS 计划任务（飞牛 / 群晖）

计划任务常有 900 秒超时，用 `--daemon` 规避：

```
/usr/bin/python3 /volume1/Scripts/github_release_monitor/main.py --daemon
```

## 配置说明

配置集中在 `config.json`（模板见 `config.example.json`）。所有字符串支持 `${VAR}` 从 `.env` 读取。

### 顶层字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `github_token` | string | `""` | GitHub Token，建议配置（限额 60 → 5000 次/时）；常用 `${GITHUB_TOKEN}` |
| `download_path` | string | `./downloads` | 下载根目录，每仓库建 `owner_repo/` 子目录 |
| `apk_rename_output` | string | `null` | `.apk1` 自定义输出目录；`null` 则与 `.apk` 同目录 |
| `mirrors` | list | `[]` | 镜像加速域名（ghproxy 类前缀镜像），按序轮询 |
| `dry_run` | bool | `false` | 全局 dry-run 开关（CLI `--dry-run` 可覆盖） |
| `repos` | list | `[]` | 监控仓库列表（核心配置） |

### download（下载参数）

| 字段 | 默认 | 说明 |
|---|---|---|
| `max_concurrent` | `3` | 并发下载数 |
| `speed_threshold_kbps` | `1024` | 慢速阈值（KB/s），低于此值持续 `slow_timeout_sec` 秒切源 |
| `slow_timeout_sec` | `10` | 慢速持续多久切源；`0` 禁用慢速检测 |

### notify（通知参数）

| 字段 | 默认 | 说明 |
|---|---|---|
| `channels` | `[]` | 通道列表，见下表 |
| `min_interval_sec` | `12` | 通知最小间隔（秒），防推送限流 |
| `translate_notes` | `true` | 非中文 release notes 是否百度翻译 |

**channels 通道类型**：

| type | 必填字段 | 可选字段 |
|---|---|---|
| `pushplus` | `token` | `topic`（群组推送） |
| `serverchan` | `key` | — |
| `telegram` | `bot_token`, `chat_id` | — |

任一通道发送成功即整体成功，互不影响。

### baidu_translate（翻译，可选）

| 字段 | 说明 |
|---|---|
| `appid` | 百度翻译 AppID（<https://api.fanyi.baidu.com>） |
| `secret` | 密钥。两者都填才启用翻译 |

### logging（日志）

| 字段 | 默认 | 说明 |
|---|---|---|
| `level` | `"INFO"` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `keep_backup_days` | `14` | 历史日志保留天数，超期自动清理 |

### repos[]（仓库项）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `repo` | string | 必填 | `owner/name`，如 `alist-org/alist` |
| `files` | list | 必填 | 资产匹配规则数组，任一规则命中即下载 |
| `release_type` | string | `"latest"` | `latest` 仅正式版 / `prerelease` 含预发布 |
| `tag_regex` | string | `null` | 正则筛选 release **tag 名**（多发布线仓库用，如 `desktop`） |
| `include_regex` | string | `null` | 资产**文件名**正则白名单 |
| `exclude_regex` | string | `null` | 资产文件名正则黑名单（优先于 include） |
| `rename_with_repo` | bool | `false` | 文件名加 `{项目}-{原名}-{版本}` 前缀 |
| `rename_apk1` | bool | `false` | 复制 `.apk` 为 `.apk1` 输出到 `apk_rename_output` |
| `keep_versions` | int | `1` | 版本保留策略：`0` 不清理 / `1` 平铺只留最新 / `≥2` 版本子目录保留 N 个 |

**files 匹配规则**（一条规则需同时满足）：
- `keywords`：文件名必须**包含所有**关键词（小写）
- `extensions`：以任一扩展名结尾，或 `"all"`
- 资产 >1GB 自动跳过

## 完整示例

`config.example.json` 内含 6 个经典项目示例，覆盖全部参数用法：

| 示例 | 展示参数 |
|---|---|
| `alist-org/alist` | files 多规则 OR、`exclude_regex` |
| `termux/termux-app` | `prerelease`、`rename_apk1`、`rename_with_repo` |
| `BtbN/FFmpeg-Builds` | keywords 多词 AND、`include_regex` + `exclude_regex` 组合 |
| `starship/starship` | `keep_versions: 2` 版本子目录 |
| `rclone/rclone` | `include_regex` 限定架构、`keep_versions: 0` |
| `esengine/DeepSeek-Reasonix` | `tag_regex` 锁定 desktop 发布线 |

## 目录结构

```
{download_path}/{owner_repo}/            keep_versions=1 平铺
{download_path}/{owner_repo}/{version}/  keep_versions≥2 版本子目录
{apk_rename_output}/*.apk1               apk1 输出（TV 装机）
logs/github_release_monitor.log          运行日志（启动自动归档旧日志）
version_record.json                      版本 / ETag / 文件状态
```

## 常见问题

**首次部署收不到通知？** 每个仓库首次发现版本会静默（防历史轰炸），第二次运行起正常通知。

**下载慢？** 把最快的镜像放 `mirrors` 第一位；国内环境直连被墙时自动切镜像。若有代理，在 `.env` 填代理地址即可（如 `HTTPS_PROXY=http://192.168.1.1:7890`）。

**计划任务超时？** 用 `--daemon`：父进程秒退，子进程后台执行，flock 防重入。

**想监控只发 tag 不发 release 的仓库？** 不支持，此类仓库请提 issue。

## 测试

```bash
/usr/bin/python3 -m pytest tests/ -q
```

93 个测试覆盖 API 客户端、下载器（断点续传 / 镜像切换 / 慢速策略）、存储清理、翻译分段、通知重试、主流程集成。

## License

[MIT](LICENSE)
