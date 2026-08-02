# EH Archive 使用与运行说明

本文档对应当前重构版 EH Archive 6.0，覆盖安装、配置、首次启动、采集、下载、校验、压缩、上传、清理、Web 控制和旧 MySQL 迁移。配置中的文件和目录路径都必须填写绝对路径；在 PowerShell 中仍建议先进入项目根目录执行命令。

## 1. 运行结构

EH Archive 由 PostgreSQL、一个 Web 进程和一个 Supervisor 进程组成：

```text
EHentai/E-Hentai
        |
        +-- 采集、详情、种子、直接下载
        |
   PostgreSQL 状态库
        |
        +-- eharchive-supervisor
        |      +-- collect
        |      +-- details
        |      +-- torrent/direct download
        |      +-- validate/prepare/upload/cleanup/delete
        |      `-- thumbnail batch
        |
        +-- eharchive-web (浏览器/API 控制)
        |
        +-- qBittorrent（种子后台传输）
        `-- LANraragi（归档上传和元数据确认）
```

Web 只修改数据库中的控制字段，不直接下载、上传或删除文件。Supervisor 按状态启动有限批次的任务子进程；qBittorrent 已接受的传输会在 qBittorrent 自己的后台继续运行。

## 2. 前置条件

必需：

- Python 3.11 或更高版本；
- 可连接的 PostgreSQL 数据库；
- 一个可访问 EH 的账号 Cookie（至少填写 `ipb_member_id` 和 `ipb_pass_hash`）；
- 用于种子下载的 qBittorrent；
- 用于归档上传的 LANraragi，并取得 API Authorization 值；
- 配置中列出的下载、准备、隔离和回收目录，并保证运行账户有读写权限。

可选：

- aria2：安装 `aria2` 额外依赖并启动 JSON-RPC 服务；
- H@H：使用 EH H@H 客户端，并把其完成目录配置到 `hah_download`；
- 旧 MySQL 迁移：安装 `migration` 额外依赖。

qBittorrent、LANraragi、aria2 和 H@H 都可以部署在其他主机，只要本机能够访问其地址或共享目录。

## 3. 安装

> 注意：`.venv` 只属于 Python venv 方案。使用 Conda 时不会在项目目录生成 `.venv`；Conda 环境保存在 Conda 的环境目录中。激活环境后直接使用 `python`、`eharchive`、`eharchive-web` 和 `eharchive-supervisor` 命令即可。

### Windows + Conda（推荐按此执行）

下面命令在 PowerShell 7 中执行。假设 Conda 已经安装，并且当前目录是项目根目录：

```powershell
Set-Location 'D:\F\program\program\python\eh-v6'
conda create -n eharchive python=3.11 -y
conda activate eharchive
python --version
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

如果你已经有可用的 Conda 环境（例如 `atf`），不必新建 `eharchive`；把上面所有 `conda activate eharchive` 替换为 `conda activate atf`，并在该环境中执行两条 `pip install` 命令即可。

如果需要 aria2 或旧 MySQL 迁移，再安装完整依赖：

```powershell
python -m pip install -e ".[dev,aria2,migration]"
```

确认安装成功：

```powershell
eharchive --help
eharchive db --help
```

以后每次运行程序前都先执行：

```powershell
Set-Location 'D:\F\program\program\python\eh-v6'
conda activate eharchive
```

如果 `conda activate` 提示 PowerShell 未初始化，先执行一次 `conda init powershell`，重启 PowerShell 7 后再激活。也可以完全不激活，直接用 `conda run`：

```powershell
conda run -n eharchive eharchive --help
conda run -n eharchive eharchive-web --config-dir config
conda run -n eharchive eharchive-supervisor --config-dir config
```

### Windows / PowerShell 7

不用激活虚拟环境也可以直接执行，能避免 PowerShell 执行策略影响：

```powershell
Set-Location 'D:\F\program\program\python\eh-v6'
python --version                         # 应为 3.11+
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e "."
```

如果要运行测试、Ruff、aria2 或迁移脚本，一次安装完整额外依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,aria2,migration]"
```

### Linux / macOS

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

安装后的主要命令为 `eharchive`、`eharchive-web` 和 `eharchive-supervisor`。如果入口脚本没有出现在 PATH 中，可将命令替换为 `python -m eh_archive.cli`，Web 和 Supervisor 分别替换为 `python -m eh_archive.web.app`、`python -m eh_archive.supervisor.app`。

## 4. 配置

### 4.1 创建本地配置文件

仓库只保存 `*.sample.toml` 模板，实际运行配置使用同名 `.toml` 文件并由 `.gitignore` 忽略。首次配置时先复制四个模板：

```powershell
Copy-Item 'config\app.sample.toml' 'config\app.toml'
Copy-Item 'config\supervisor.sample.toml' 'config\supervisor.toml'
Copy-Item 'config\crawl.sample.toml' 'config\crawl.toml'
Copy-Item 'config\secrets.sample.toml' 'config\secrets.toml'
```

然后编辑四个本地 `.toml` 文件。`config/secrets.toml` 包含 Cookie、密码和 token，不能提交到 Git；`app.toml` 中的存储目录必须替换为实际绝对路径：

```toml
database_url = "postgresql+psycopg://user:password@127.0.0.1:5432/eh_archive"
web_secret = "change-this-long-random-secret"

[accounts.default]
# 可直接粘贴浏览器复制的 Cookie 字符串
cookies_str = "igneous=填写值;ipb_member_id=填写值;sl=dm_2;sk=填写值;ipb_pass_hash=填写值"

[networks.direct]
# proxies = { http = "http://user:pass@host:port", https = "http://user:pass@host:port" }

[networks.archive]
# archive 账号使用固定网络；需要时在此配置 proxies

[qbittorrent]
host = "http://127.0.0.1:8080"
username = "qbit 用户名"
password = "qbit 密码"

[lanraragi]
Authorization = "Bearer LANraragi_API_Token"
```

数据库连接字符串的优先级为：`EHARCHIVE_DATABASE_URL` 环境变量、`secrets.toml`、`app.toml`。Web 密钥的优先级为 `EHARCHIVE_WEB_SECRET` 环境变量、`secrets.toml`。PowerShell 临时设置方式：

```powershell
$env:EHARCHIVE_DATABASE_URL = 'postgresql+psycopg://user:password@127.0.0.1:5432/eh_archive'
$env:EHARCHIVE_WEB_SECRET = 'change-this-long-random-secret'
```

如果使用两个 EH 账号，分别在 `[accounts.browse]`、`[accounts.archive]` 填 Cookie，再在 `app.toml` 的 `[sessions.browse]` 和 `[sessions.archive]` 指定对应的 `account`、`network`。单账号安装保持默认的 `default/direct` 即可。

### 4.2 `config/app.toml`

最重要的配置项：

| 配置项 | 作用 |
| --- | --- |
| `database_url` | PostgreSQL URL；通常放在 `secrets.toml` 更安全 |
| `web_host`、`web_port` | Web 监听地址，默认 `127.0.0.1:8787` |
| `qbittorrent_url` | qBittorrent Web API 地址 |
| `qbit_torrent_path` | qBittorrent 主机看到的种子保存路径，可与本地 `roots.torrent_download` 不同 |
| `lanraragi_url` | LANraragi 地址 |
| `max_file_size` | 单个产物允许的最大字节数 |
| `fallback_method` | 无种子或种子停滞时的 `direct`、`hah` 或 `aria2` |
| `aria2_enabled`、`hah_enabled` | 启用对应可选下载器 |
| `[roots]` | 受控文件根目录；每个值都必须是运行机器上的绝对目录 |

`roots` 不接受相对路径，也不会根据启动目录补全。目录不存在时，程序会在需要时创建；更换存储盘时只需修改这些绝对根目录。数据库仍只保存受控位置键和文件名，不保存这些绝对路径。

各位置键的含义如下。

| 配置键 | 用途 | 当前运行时 |
| --- | --- | --- |
| `torrent_download` | EH Archive 本机读取种子完成文件的目录；不是 qBittorrent API 的保存路径 | 使用 |
| `hah_download` | H@H 客户端完成下载的目录；程序扫描其中带 `galleryinfo.txt` 的画廊目录 | 使用 H@H 时使用 |
| `direct_download` | EH direct 下载得到的 ZIP；包含临时下载文件和验证后的代次文件 | 使用 direct 时使用 |
| `aria2_download` | aria2 提交的临时文件和完成后的 ZIP | 启用 aria2 时使用 |
| `prepared` | 把下载目录压缩成 ZIP 后、上传 LANraragi 前的标准产物目录 | 使用 |
| `quarantine` | 校验失败、LANraragi 不支持或需要人工复核的隔离产物 | 使用 |
| `trash` | 为可回收删除预留的受控目录 | 当前清理代码不自动移入这里 |

示例（Windows 本机读取、Linux 主机运行 qBittorrent；`D:/eharchive-data` 请替换成你的实际目录）：

```toml
qbit_torrent_path = "/home/ubuntu/ptcache/ehentai"

[roots]
torrent_download = "D:/eharchive-data/torrent_download"
hah_download = "D:/eharchive-data/hah_download"
direct_download = "D:/eharchive-data/direct_download"
aria2_download = "D:/eharchive-data/aria2_download"
prepared = "D:/eharchive-data/prepared"
quarantine = "D:/eharchive-data/quarantine"
trash = "D:/eharchive-data/trash"
```

qBittorrent 返回 `/home/ubuntu/ptcache/ehentai/1234567/archive.zip` 后，EH Archive 会按根目录后的相对部分读取 `D:/eharchive-data/torrent_download/1234567/archive.zip`。两边必须保持根目录下的相对目录结构一致；如果 qBittorrent 与 EH Archive 在同一台机器，就把两个配置设成同一个绝对目录。

### 4.3 `config/crawl.toml`

将要定时采集的 EH 列表 URL 放在 `[urls]`：

```toml
observation_days = 1
name_keywords = ["关键词"]
tag_keywords = ["artist:某作者"]
exclude_categories = ["Western"]

[urls]
latest = "https://e-hentai.org/?f_search=..."
```

采集会跟随列表的下一页，最多 100 页。名称、标签、分类过滤和观察期会决定档案进入 `download_pending`、`deferred` 或 `skipped`。修改后 Supervisor 下一个采集周期会使用新配置；需要立即采集可按第 6 节执行 CLI。

### 4.4 `config/supervisor.toml`

常用项：

- `poll_seconds`：Supervisor 调度轮询间隔；
- `collect_interval_seconds`：自动采集周期，默认 3 小时；
- `batch_size`：每个任务子进程处理的最大条数；
- `lease_seconds`、`lease_recovery_seconds`：任务租约和过期恢复；
- `retry_limit`：网络或临时失败的重试次数；
- `max_concurrency`：各任务槽的并发数，默认每类为 1；
- `thumbnail_interval_seconds`：缩略图批处理周期。

不要把 `torrent_download` 的并发数理解为 qBittorrent 的传输数。它只限制 EH Archive 同时查找、提交和轮询种子的控制任务；已经提交的种子由 qBittorrent 自己管理。

## 5. 初始化数据库并检查连接

先在 PostgreSQL 创建数据库和用户（以下命令需要 PostgreSQL 客户端权限）：

```powershell
psql -U postgres -c "CREATE USER eharchive WITH PASSWORD 'change-me';"
psql -U postgres -c "CREATE DATABASE eh_archive OWNER eharchive;"
```

已有数据库时不需要重复创建。填好连接字符串后，在项目根目录运行：

Conda 环境（例如环境名为 `eh`）：

```powershell
conda activate eh
eharchive --config-dir config db ping
eharchive --config-dir config db upgrade
```

如果使用 venv，才使用下面的 `.venv` 路径：

```powershell
.\.venv\Scripts\eharchive.exe --config-dir config db ping
.\.venv\Scripts\eharchive.exe --config-dir config db upgrade
```

`db upgrade` 使用 Alembic 将 PostgreSQL schema 升到最新版本；不要用 SQLite URL 替代 PostgreSQL。若 `db ping` 失败，先检查数据库是否启动、主机端口、用户名密码以及 PostgreSQL 的 `pg_hba.conf`。

## 6. 启动服务

### Conda 环境

打开两个 PowerShell 7 窗口，两个窗口都先进入项目根目录并激活同一个环境：

```powershell
Set-Location 'D:\F\program\program\python\eh-v6'
conda activate eh
```

窗口一（Web）：

```powershell
eharchive-web --config-dir config
```

窗口二（Supervisor）：

```powershell
eharchive-supervisor --config-dir config
```

### venv 环境

打开两个 PowerShell 7 窗口，分别在项目根目录执行：

窗口一（Web）：

```powershell
.\.venv\Scripts\eharchive-web.exe --config-dir config
```

窗口二（Supervisor）：

```powershell
.\.venv\Scripts\eharchive-supervisor.exe --config-dir config
```

浏览器访问：

- `http://127.0.0.1:8787/`：最近档案的简单列表；
- `http://127.0.0.1:8787/docs`：FastAPI Swagger API 文档；
- `http://127.0.0.1:8787/health`：数据库、组件、存储目录和状态计数健康信息。

生产环境请用 Windows Task Scheduler/NSSM/WinSW 或 Linux systemd 托管这两个常驻进程，并保证二者使用同一个配置目录和 PostgreSQL URL。升级程序前先将 Web 和 Supervisor 停止或把 `all` 组件暂停。

## 7. 采集、下载和上传

本节中的 `.\.venv\Scripts\eharchive.exe` 只适用于 venv。使用 Conda 时先执行 `conda activate eh`，然后把它替换为 `eharchive`。

### 7.1 自动采集

把列表 URL 写入 `crawl.toml` 后，Supervisor 按 `collect_interval_seconds` 自动运行。也可以立即执行一次：

```powershell
.\.venv\Scripts\eharchive.exe --config-dir config collect 'https://e-hentai.org/?f_search=...'
```

该命令会保存列表中的基础信息并跟随下一页。它使用 browse 会话；Cookie、代理或 EH 返回登录页时，错误会记录在日志和档案事件中。

### 7.2 手工加入单个画廊

```powershell
.\.venv\Scripts\eharchive.exe --config-dir config add `
  'https://e-hentai.org/g/1234567/abcdef1234/' `
  --priority 100 `
  --remark '手工优先'
```

`add` 只接受 `e-hentai.org`/`exhentai.org` 的 `/g/<数字>/<slug>/` URL。重复加入同一个画廊会更新优先级和备注，不会创建重复记录。

### 7.3 典型处理链路

```text
discovered/deferred
        -> download_pending
        -> downloading -> downloaded
        -> validating -> preparing -> upload_pending
        -> uploading -> uploaded -> completed
```

Supervisor 会自动运行 `details`、`torrent_download`、`direct_download`、`validate`、`prepare`、`upload`、`cleanup` 和 `delete`。种子优先提交 qBittorrent；没有可用种子、种子丢失或长期停滞时，根据 `fallback_method` 切换 direct/H@H/aria2。direct 下载会先向 EH archive 页面提交 `dltype=org`，解析临时链接后以分片、断点续传方式下载，并在注册产物前验证 ZIP、大小、CRC、SHA-1 和 SHA-256。

上传到 LANraragi 前必须有完整 MangaInfo。上传成功必须同时拿到 40 位 SHA-1 archive ID 并通过远端 metadata 确认，之后才会清理本地文件和 qBittorrent/aria2 任务。HTTP 409、结果不确定或 archive ID 无法确认时会进入 `manual_review`，不会猜测上传是否成功。

### 7.4 手动运行单类任务

正常运行不需要手工执行；排障或需要立即处理时可以运行有限批次：

```powershell
.\.venv\Scripts\eharchive.exe --config-dir config task details --limit 10
.\.venv\Scripts\eharchive.exe --config-dir config task torrent_download --limit 10
.\.venv\Scripts\eharchive.exe --config-dir config task direct_download --limit 10
.\.venv\Scripts\eharchive.exe --config-dir config task validate --limit 10
.\.venv\Scripts\eharchive.exe --config-dir config task prepare --limit 10
.\.venv\Scripts\eharchive.exe --config-dir config task upload --limit 10
.\.venv\Scripts\eharchive.exe --config-dir config task cleanup --limit 10
```

`delete` 只处理已经被新版本替代且状态为 `outdated` 的记录；不要用它代替普通清理。任务执行是有租约和 attempt fencing 的，已过期的进程不能覆盖新产物。

### 7.5 缩略图批处理

缩略图再生成独立于主上传链路，Supervisor 会按间隔自动处理；需要立即处理时：

```powershell
.\.venv\Scripts\eharchive.exe --config-dir config thumbnails --limit 100
```

它不会把 `uploaded` 或 `completed` 档案改回下载状态。

### 7.6 Picacg 导入

导出目录的每个子目录需要有 `cid.txt` 和 `index.html`：

```powershell
.\.venv\Scripts\eharchive.exe --config-dir config picacg import `
  'D:\PicacgExport' `
  --base-url 'https://picacg.example/comic'
.\.venv\Scripts\eharchive.exe --config-dir config picacg screen
```

导入记录先以 `picacg/<cid>` 和 `discovered` 保存；`screen` 会按真实名称与 EH 记录去重，未匹配的项目才进入正常下载队列。

## 8. 可选下载器配置

### aria2

安装额外依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[aria2]"
```

在 `app.toml` 设置 `aria2_enabled = true`，并在 `secrets.toml` 配置 archive 网络下的 JSON-RPC：

```toml
[networks.archive.aria2]
host = "http://127.0.0.1:6800/rpc"
secret = ""
```

aria2 必须由外部进程运行；EH Archive 只提交、轮询和清理任务。

### H@H

在 `app.toml` 设置 `hah_enabled = true`，并确保 H@H 客户端完成目录与 `[roots].hah_download` 相同。程序通过 EH archive 页面排队，然后扫描带有对应画廊前缀且包含 `galleryinfo.txt` 的目录。未部署 H@H 时不要把 `fallback_method` 设为 `hah`。

## 9. Web/API 控制

启用 `web_secret` 后，所有写请求都必须带：

```text
Authorization: Bearer <web_secret>
```

GET 请求可用于健康检查和查询。常用接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/health` | 健康状态和各状态数量 |
| GET | `/api/manga?status=manual_review` | 按状态分页查询 |
| GET | `/api/manga/{manga_id}` | 档案、attempt 和最近事件 |
| POST | `/api/manga` | 手工添加 URL |
| PATCH | `/api/manga/{manga_id}/remark` | 更新备注 |
| POST | `/api/manga/{manga_id}/actions/retry` | 重试、恢复或覆盖跳过 |
| POST | `/api/manga/{manga_id}/actions/cancel` | 请求取消 |
| POST | `/api/manga/{manga_id}/actions/validate` | 从已下载产物重新校验 |
| POST | `/api/manga/{manga_id}/actions/upload` | 从验证/人工状态重新上传 |
| POST | `/api/manga/{manga_id}/archive-confirmation` | 人工确认 LANraragi archive ID |
| PUT | `/api/control/{component}` | 暂停或恢复组件 |

写操作使用 `row_version` 做并发保护；先 GET 档案取得最新 `row_version`，再把它放进 POST/PATCH body。PowerShell 示例：

```powershell
$base = 'http://127.0.0.1:8787'
$headers = @{ Authorization = 'Bearer change-this-long-random-secret' }
$item = Invoke-RestMethod "$base/api/manga/1234567/abcdef1234" -Headers $headers

$body = @{
  row_version = $item.row_version
  reason = '人工确认后重试'
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "$base/api/manga/1234567/abcdef1234/actions/retry" `
  -Headers $headers -ContentType 'application/json' -Body $body
```

取消是两阶段操作：Web 先写入 `cancel_requested`，当前任务到达安全边界后 Supervisor 收尾为 `cancelled`。可暂停全部流程或单个组件：

```powershell
$body = @{ state = 'paused'; reason = '维护存储' } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "$base/api/control/all" `
  -Headers $headers -ContentType 'application/json' -Body $body
```

维护结束时把 `state` 改为 `running`。上传结果不确定且档案处于 `manual_review` 时，只能在 LANraragi 确认后使用 40 位 SHA-1 archive ID 调用 `archive-confirmation`；`reason` 是必填项。

## 10. 从旧 MySQL 迁移

迁移脚本在 `scripts/`，不属于运行时服务。建议使用旧库只读账号，并保留旧 MySQL 直到新系统完成一个完整周期。

先创建迁移专用配置。它不是运行时配置，不会被主程序读取；复制后只在本机保留 `config/migration.toml`：

```powershell
Copy-Item 'config\migration.sample.toml' 'config\migration.toml'
```

编辑 `config/migration.toml` 中的 `[mysql]` 和 `[postgres]`。用户名、密码、主机、端口和数据库名都是独立字段，不需要拼接 URL，也不需要编码密码。

在 Conda 环境中安装迁移依赖，再执行 dry-run：

```powershell
conda activate eh
python -m pip install -e ".[migration]"
python scripts\migrate_mysql_to_postgresql.py `
  --config 'config\migration.toml' `
  --dry-run --report migration-report.json
```

确认状态映射后再写入新库：

```powershell
python scripts\migrate_mysql_to_postgresql.py `
  --config 'config\migration.toml' `
  --apply --report migration-report.json

python scripts\verify_migration.py `
  --config 'config\migration.toml'

python scripts\reconcile_migration.py `
  --config 'config\migration.toml' `
  --config-dir config
```

如果部署在 Linux 服务器上，使用 Bash 命令，不要复制上面的 PowerShell 反引号：

```bash
cd /home/ubuntu/ehentai_download_v6
conda activate eh
cp config/migration.sample.toml config/migration.toml
chmod 600 config/migration.toml

python scripts/migrate_mysql_to_postgresql.py \
  --config config/migration.toml \
  --dry-run \
  --report ./migration-dry-run.json

python scripts/migrate_mysql_to_postgresql.py \
  --config config/migration.toml \
  --apply \
  --report ./migration-apply.json

python scripts/verify_migration.py \
  --config config/migration.toml

python scripts/reconcile_migration.py \
  --config config/migration.toml \
  --config-dir config
```

迁移脚本不会删除旧 MySQL 行、旧文件或远端归档。`verify_migration.py` 关注行数、详情缺失、重复 archive ID、无指纹产物和迁移审计事件；`reconcile_migration.py` 关注数据库登记产物是否仍存在。

## 11. 运维、停止和故障排查

- 日志目录由 `app.toml` 的 `log_dir` 指定，也必须是绝对目录；不要把 Cookie、Authorization 或代理密码写入事件备注。
- 先看 `/health`，再看 `/api/manga/{manga_id}` 的 `attempts` 和 `events`。失败会有 `error_code`、下次重试时间和最后一次操作。
- 维护前先暂停 `all`，等待正在执行的任务到安全边界，再停止两个进程。普通前台运行直接按 `Ctrl+C`；Supervisor 会终止子进程并在下次启动时恢复过期租约。
- `manual_review` 不是自动重试状态：检查 EH 页面、文件、LANraragi metadata 或重复上传后，用 Web action 或人工 archive confirmation 明确恢复。
- 看到 `qBittorrent no longer reports...`、种子长期 `stalleddl` 时，检查 qBittorrent 任务和磁盘；程序会在停滞阈值后清理并切换 fallback。
- LANraragi 返回 401/403 通常是 Authorization 错误；415 会把产物移到 quarantine；409 或不确定的 5xx 结果必须人工核对 archive ID。
- `db ping` 正常但没有新档案时，检查 `crawl.toml` 的 `[urls]`、browse Cookie、代理、分类/关键词过滤和观察期。
- 如果提示 `qbittorrent-api is missing`，确认当前 Conda/venv 已执行 `python -m pip install -e .`；aria2 仍需单独安装 `.[aria2]`。

定期备份 PostgreSQL 和各 `roots` 根目录；数据库记录的是受控位置键和安全文件名，搬迁存储根目录时应连同文件一起迁移并修改 `app.toml`。

## 12. 开发验证

安装 `[dev]` 后可以在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe format src scripts migrations tests
.\.venv\Scripts\ruff.exe check src scripts migrations tests
```

测试和 `db upgrade` 都应在与实际服务相同的 Python 环境中执行。真实 qBittorrent、LANraragi、EH Cookie/代理和 PostgreSQL 联调仍需要对应服务可用，单元测试不会替代这些外部依赖检查。
