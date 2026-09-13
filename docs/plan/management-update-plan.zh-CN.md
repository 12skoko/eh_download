# Web 管理、服务控制与在线更新设计

## 1. 设计结论

EH Archive 采用以下最终结构：

1. 保留现有 Web，不新增 Manager Web；
2. 不新增 `web.toml`，现有业务配置文件保持不变；
3. 在现有 Web 中增加系统管理页面；
4. Web 只负责提交操作和展示结果，不直接承担长时间服务控制；
5. systemd 托管 Web、Supervisor 和一次性 Operation Executor；
6. 所有重启、配置应用和更新操作由统一的 Operation Executor 执行；
7. Operation Executor 按需启动，操作结束后退出，不是常驻 Manager；
8. 使用独立文件目录持久化操作状态、进度和历史；
9. 使用独立的管理配置文件保存部署路径、Git 和 systemd 参数；
10. 程序和 Conda 环境继续保留在 `/root`；
11. 服务不设置开机自启，由用户或 Web 手动启动；
12. systemd 配置和管理配置均由安装命令自动生成，不要求人工编写。

```text
浏览器
  │
  ▼
EH Archive Web
  ├── 业务管理
  ├── 配置编辑
  ├── 系统状态
  └── 提交管理操作
          │
          ▼
Operation Store（文件）
          │
          ▼
eharchive-operation@<ID>.service
  ├── 应用配置
  ├── 优雅重启 Supervisor
  ├── 重启 Web 或全部服务
  └── Git 更新
          │
          ▼
systemd
  ├── eharchive-web.service
  └── eharchive-supervisor.service
```

## 2. 职责边界

### 2.1 Web

Web 负责：

- 验证登录和 CSRF；
- 展示服务、配置和 Git 状态；
- 接收配置表单；
- 生成待应用配置；
- 创建管理操作；
- 请求 systemd 启动对应 Operation Executor；
- 轮询并展示操作进度、日志摘要和最终结果。

Web 不负责：

- 在 HTTP 请求中等待 Supervisor drain 完成；
- 在自身退出后继续执行更新；
- 直接执行用户提供的 shell 命令；
- 管理 EH Archive 之外的服务。

### 2.2 Supervisor

Supervisor 继续只负责：

- 读取调度配置；
- 维护自己的 lease 和 heartbeat；
- 响应 `running`、`paused` 和 `draining` 控制状态；
- 启动和回收普通 Worker 与特殊 Worker。

Supervisor 不负责更新代码或重启 Web。

### 2.3 Operation Executor

Operation Executor 是按操作 ID 启动的一次性进程：

```text
eharchive-operation@<operation-id>.service
```

它负责：

- 获取全局操作锁；
- 从操作目录读取操作类型和参数；
- 推进操作状态机；
- 调用固定的 systemd、Git、配置校验、安装和迁移命令；
- 持久化每个步骤的开始、完成和错误；
- 完成后释放锁并退出。

即使 Web 被停止，Operation Executor 仍由 systemd 独立托管，可以继续执行。

### 2.4 “不建设通用服务器管理面板”的含义

系统页面只管理 EH Archive 自身：

- 只能控制 `eharchive-web` 和 `eharchive-supervisor`；
- 只能更新安装时登记的固定 Git 仓库和分支；
- 只能编辑当前配置页面已经声明的字段；
- 只能查看 EH Archive 的操作日志。

它不提供终端、任意命令执行、通用文件管理器、任意 systemd 服务控制、系统用户管理、软件包管理或防火墙管理。这是产品边界，不是临时限制。

## 3. 管理配置

更新和服务控制需要专用配置，但该配置不属于 Web、Supervisor 或 Worker 的业务配置。

使用：

```text
/etc/eharchive/management.toml
```

该文件由 `eharchive service install` 自动生成，正常情况下无需人工编辑，也不由 Web 配置页面修改。

建议结构：

```toml
[deployment]
repository = "/root/ehentai"
config_dir = "/root/ehentai/config"
python = "/root/miniconda3/envs/eh/bin/python"
management_dir = "/root/eharchive-data/log/management"

[git]
remote = "origin"
branch = "main"
require_clean_worktree = true
fast_forward_only = true

[systemd]
web_unit = "eharchive-web.service"
supervisor_unit = "eharchive-supervisor.service"
operation_unit_template = "eharchive-operation@.service"

[health]
web_live_url = "http://127.0.0.1:8787/health/live"
web_start_timeout_seconds = 60
supervisor_start_timeout_seconds = 60
supervisor_drain_timeout_seconds = 0
poll_seconds = 2

```

`supervisor_drain_timeout_seconds = 0` 表示默认一直等待当前任务自然完成。页面可显示等待时长，并允许用户取消本次 drain；不提供强制停止并重启功能，任务卡死时由用户手工排查处理。

实际生成时，`management_dir` 设置为当前 `<app.log_dir>/management`。管理配置是安装信息的唯一来源。systemd unit 和 Web 管理功能都引用同一份配置，避免在多个脚本中重复保存仓库路径、Python 路径和 unit 名称。

管理操作历史不自动清理。修改 `app.log_dir` 时，正在运行的配置发布操作继续使用原管理目录；新配置验证和服务健康检查成功后，Executor 再将 `management.toml` 中的 `management_dir` 切换到新日志目录，后续操作使用新位置。旧管理目录不会自动删除。

## 4. 持久化操作存储

管理操作不使用主 PostgreSQL 或额外 SQLite。每个操作使用一个独立目录：

```text
<app.log_dir>/management/
├── history/
│   └── <operation-id>/
│       ├── request.json
│       ├── state.json
│       ├── events.jsonl
│       ├── configuration.json
│       └── operation.log
└── staging/
    └── <operation-id>/
```

全局操作锁独立放在 `/run/eharchive/deployment.lock`，不属于日志或历史目录，不随 `app.log_dir` 变化。

各文件职责：

| 文件 | 说明 |
|---|---|
| `request.json` | 创建后不再修改的操作类型、操作者和白名单参数 |
| `state.json` | 当前状态、阶段、时间、heartbeat、结果和错误 |
| `events.jsonl` | 只追加的步骤事件和状态变化历史 |
| `configuration.json` | 配置 revision、修改字段、生效范围和备份路径 |
| `operation.log` | Git、安装、迁移和 systemd 命令的详细输出 |

`state.json` 包含：

- 操作 ID 和类型；
- `pending/running/succeeded/failed/cancelled/interrupted` 状态；
- 当前阶段；
- 创建、开始、heartbeat 和结束时间；
- 操作者；
- 旧 commit 和目标 commit；
- 结果摘要；
- 稳定错误码和错误信息。

`state.json` 和 `configuration.json` 使用“同目录临时文件、flush、fsync、原子 replace”更新，避免进程中断留下半个 JSON。`events.jsonl` 和 `operation.log` 只追加，每次写入后 flush。

Web 通过扫描操作目录并读取 `state.json` 展示活动操作和历史。操作量很小，无需额外索引数据库。系统不自动清理这些文件。

配置发布历史由每个操作的 `configuration.json` 保存，其中记录：

- 配置文件；
- 原 revision 和新 revision；
- 修改字段；
- 生效范围；
- 备份路径；
- 应用和恢复结果。

## 5. 操作并发模型

服务控制和代码更新采用全局排他锁。同一时间只允许一个会改变部署状态的操作运行：

- `apply_config`；
- `restart_supervisor`；
- `restart_web`；
- `restart_all`；
- `git_update`。

只读操作不占用排他锁：

- 查询服务状态；
- 查询 Git 状态；
- 查看操作历史和日志。

Executor 统一使用固定路径 `/run/eharchive/deployment.lock` 的 `flock` 获得排他锁。该锁用于配置应用、服务重启和 Git 更新，不仅用于升级或数据库迁移。

- 安装命令创建 `/run/eharchive/`；机器重启后，Executor 在获取锁前确保该目录存在；
- 目录由 root 管理，所有 Executor 使用同一锁文件；
- 从执行部署变更前到操作结束始终持锁，结束时释放锁，但不删除或替换锁文件；
- 不将该目录绑定到单个 Operation unit 的退出清理生命周期；
- 修改 `app.log_dir` 只切换管理日志和历史目录，不迁移锁；
- 获取锁后重新读取管理配置，避免使用等待期间已过期的目录；已创建操作仍从其原目录读取请求并写入结果。

创建操作前，Web 同时检查现有 `state.json`，用于尽早提示冲突；文件锁才是最终并发保证。Executor 意外退出后，新的操作检查旧操作 heartbeat 和对应 systemd unit 状态，再将确认失效的旧操作标记为 `interrupted`。

## 6. 配置生效模型

仓库模板与真实配置按目录分离：

```text
config.sample/                 # Git 跟踪的业务配置模板
├── app.toml
├── supervisor.toml
├── crawl.toml
├── secrets.toml
├── migration.toml
└── special/
    └── video_archive.toml

config/                        # Git 整体忽略的真实配置
├── app.toml
├── supervisor.toml
├── crawl.toml
├── secrets.toml
├── migration.toml
└── special/
    └── video_archive.toml
```

管理配置不属于业务配置目录：仓库参考模板放在 `deployment/management.sample.toml`，真实文件继续由安装程序生成到 `/etc/eharchive/management.toml`。

不增加 `web.toml`。运行时继续使用：

```text
app.toml
supervisor.toml
crawl.toml
special/video_archive.toml
secrets.toml
```

配置是否需要重启不再由文件名粗略决定，而由字段或配置区域携带生效策略。

| 策略 | 含义 |
|---|---|
| `next_worker` | 新启动的 Worker 自动读取，无需重启常驻进程 |
| `supervisor` | 重启 Supervisor 后生效 |
| `web` | 重启 Web 后生效 |
| `web_and_supervisor` | 两个常驻进程都需重启 |

保存多个字段时合并生效范围。

### 6.1 `app.toml`

| 字段类别 | 示例 | 策略 |
|---|---|---|
| Web 监听 | `web_host`、`web_port` | `web` |
| 公共基础配置 | `database_url`、`timezone`、`log_level`、`log_dir` | `web_and_supervisor` |
| 存储根目录 | `[roots]` | `web_and_supervisor` |
| 上传策略 | `upload_backend`、大文件阈值 | `supervisor` |
| 外部服务 | qBittorrent、LANraragi、SMB、aria2、H@H | `supervisor` |
| EH 请求策略 | 请求延迟、重试和冷却 | `supervisor` |

Worker 会在新进程启动时重新读取部分参数，但 Supervisor 的健康检查也使用并缓存其中一些配置。因此这类字段统一归为 `supervisor`，确保运行状态和健康检查一致。

### 6.2 `supervisor.toml`

所有字段使用 `supervisor`。Supervisor 在启动时缓存调度间隔、批量大小、模块开关、维护窗口、lease 和特殊任务并发。

### 6.3 `crawl.toml`

采集地址、筛选规则、排除分类和视频标记使用 `next_worker`：

- 已运行的 Collect 或 Screen 继续使用旧配置；
- 下一次启动的 Collect 或 Screen 自动使用新配置；
- 不重启 Web；
- 不重启 Supervisor。

### 6.4 `special/video_archive.toml`

| 字段类别 | 策略 |
|---|---|
| `enabled`、`work.max_concurrency` | `supervisor` |
| ffmpeg、输出、安全限制和任务参数 | `next_worker` |

### 6.5 `secrets.toml`

继续不允许从 Web 编辑。人工修改后通过系统页面选择相应的服务重启操作。

## 7. 配置发布流程

配置保存和配置生效作为一次可追踪的发布操作处理。

```text
Web 接收表单
  → 检查原文件 revision
  → 生成候选 TOML
  → 在隔离目录组合完整配置并执行加载校验
  → 计算修改字段和生效范围
  → 将候选文件写入 <app.log_dir>/management/staging/<operation-id>/
  → 创建 apply_config 操作
  → 启动 Operation Executor
  → Executor 再次检查 revision
  → 备份原配置
  → 原子替换配置
  → 按生效范围重启服务或直接完成
  → 执行健康检查
  → 写入配置 revision 和操作结果
```

如果新配置导致服务无法启动：

1. Executor 恢复本次操作创建的配置备份；
2. 使用旧配置重新启动受影响服务；
3. 将操作标记为失败；
4. 写入失败信息、服务状态和日志；Web 恢复后可从页面查看，若仍无法启动则由用户手工排查。

对于 `next_worker`，配置原子替换后操作直接完成，不停止任何服务。

## 8. Supervisor 重启状态机

虽然 Web 在只重启 Supervisor 时不会退出，但仍统一交给 Operation Executor，以获得崩溃恢复、排他锁、完整历史和相同的进度展示。Executor 只在操作期间运行，不会增加常驻服务。

```text
pending
  → acquire_lock
  → capture_previous_control_state
  → request_draining
  → wait_for_children
  → wait_for_supervisor_exit
  → start_supervisor
  → wait_for_heartbeat
  → restore_previous_control_state
  → verify_heartbeat
  → succeeded
```

如果重启前 Supervisor 为 `paused`，重启后保持 `paused`；如果为 `running`，健康检查通过后恢复 `running`。

drain 默认不设置硬超时。页面提供：

- 当前运行 Worker；
- 已等待时间；
- 取消本次 drain。

取消请求提交给当前操作，由持锁的 Executor 在 drain 等待阶段处理，恢复原控制状态并将操作标记为 `cancelled`，不创建第二个竞争锁的操作。离开 drain 等待阶段后不再接受该取消请求。

不提供强制停止并重启按钮、API 或独立强制操作，也不在等待超时后自动强制停止。任务卡死时由用户手工排查处理。

## 9. Web 重启状态机

```text
pending
  → acquire_lock
  → stop_web
  → start_web
  → wait_for_live_health
  → succeeded
```

Web 提交操作后立即返回操作 ID。浏览器进入重连页面：

1. 定时请求 `/health/live`；
2. Web 恢复后读取操作目录中的 `state.json`；
3. 成功则返回系统页面；
4. Web 恢复但操作失败时，展示已记录的失败结果；若 Web 始终无法启动，不要求浏览器读取 systemd 状态或操作日志，由用户手工排查。

Executor 将启动失败写入操作状态和日志。用户通过 `systemctl status`、`journalctl` 或操作日志定位问题，不建设独立的故障展示服务。

## 10. 全部重启状态机

```text
pending
  → acquire_lock
  → capture_previous_control_state
  → request_supervisor_draining
  → wait_for_supervisor_exit
  → stop_web
  → start_web
  → wait_for_web_health
  → start_supervisor
  → wait_for_supervisor_heartbeat
  → restore_previous_control_state
  → succeeded
```

先停止 Supervisor 的任务流，再重启 Web，避免 Web 已退出而 drain 尚未完成。

## 11. Git 更新设计

### 11.1 更新检查

更新检查是只读操作，不停止服务：

```text
验证仓库路径
  → 验证 remote 和当前 branch
  → git fetch --prune <remote>
  → 解析 HEAD 和 <remote>/<branch>
  → 验证是否可 fast-forward
  → 展示 commit 差异和文件摘要
```

检查结果包含目标 commit。执行更新时再次 fetch 和验证，不能直接相信较早的检查结果。

### 11.2 更新状态机

```text
pending
  → acquire_lock
  → verify_repository
  → verify_clean_worktree
  → fetch_remote
  → resolve_target_commit
  → verify_fast_forward
  → capture_service_and_control_state
  → request_supervisor_draining_if_previously_active
  → wait_for_supervisor_exit_if_previously_active
  → stop_web_if_previously_active
  → fast_forward_checkout
  → sync_python_environment
  → validate_configuration
  → upgrade_database
  → refresh_systemd_units_if_needed
  → start_web_if_previously_active
  → verify_web_health_if_started
  → start_supervisor_if_previously_active
  → verify_supervisor_heartbeat_if_started
  → restore_previous_control_state_if_supervisor_started
  → record_deployed_commit
  → succeeded
```

更新分别保存和恢复 Web、Supervisor 的运行状态：原来停止的保持停止，原来运行的才重新启动。原本停止的服务不为健康检查临时启动，对应检查记为“不适用”；Supervisor 重新启动后恢复更新前的 `running` 或 `paused` 控制状态。

Git 实际执行语义为：

```bash
git fetch --prune <remote>
git merge --ff-only <remote>/<branch>
```

不接受 Web 输入的仓库路径、remote、branch 或额外 Git 参数。

### 11.3 Python 环境同步

使用管理配置中记录的 Conda Python 绝对路径执行安装，不依赖 shell 中是否已经激活 Conda：

```text
<python> -m pip install -e <repository>
```

若项目采用锁文件同步命令，则统一封装在更新模块中。更新模块记录完整输出和退出码。

### 11.4 数据库迁移与恢复

更新前记录：

- 旧 commit；
- 目标 commit；
- 当前数据库 migration revision；
- 服务状态；
- Supervisor 控制状态。

失败处理：

- 停服务前失败：保持原服务状态不变；
- 停服务后、数据库迁移开始前失败：如已修改代码或环境，恢复旧 commit 和旧 Python 安装，再按更新前的运行状态恢复服务；
- 数据库迁移命令失败：停止后续步骤，记录失败状态、命令输出和退出码，交由用户手工排查，不自动降级数据库或继续启动服务；
- 数据库迁移完成后启动失败：保留新 commit，记录失败状态和日志，交由用户手工排查，不自动 downgrade 数据库；
- 配置不兼容：在迁移前终止并恢复旧 commit。

暂不设计数据库迁移中途失败的自动恢复机制。上述服务恢复均遵循“原来停止的保持停止，原来运行的才启动”。

数据库 migration 应遵循向前兼容原则，使旧 Web/Supervisor 在结构扩展期间仍可读取数据库。删除字段或不可逆的数据变换应拆成独立版本，不与首次部署新代码同时发生。

## 12. systemd 设计

使用系统级 unit，因为当前部署目录和 Conda 环境位于 `/root`。

### 12.1 `eharchive-web.service`

- `WorkingDirectory`：固定仓库路径；
- `ExecStart`：Conda Python 的绝对路径；
- `--config-dir`：绝对路径；
- `Restart=on-failure`；
- 不执行 enable。

### 12.2 `eharchive-supervisor.service`

- 与 Web 使用同一仓库、Conda 环境和配置目录；
- `Restart=on-failure`；
- draining 成功退出时不自动拉起；
- 由 Operation Executor 在正确时机明确 start。

### 12.3 `eharchive-operation@.service`

模板实例参数为操作 UUID：

```text
systemctl start eharchive-operation@<operation-id>.service
```

unit 固定执行：

```text
<python> -m eh_archive.management.executor
    --management-config /etc/eharchive/management.toml
    --operation-id <operation-id>
```

该服务：

- `Type=oneshot`；
- 不启用开机自启；
- 不常驻；
- 退出状态与操作结果对应；
- 由 journald 和独立操作日志同时记录。

### 12.4 systemd 接管后的日志行为

改用 systemd 不替换现有项目日志，只改变 stdout 的接收位置。

当前 `configure_logging` 同时创建：

1. stdout 的 JSON 日志；
2. `app.log_dir` 下的 JSON 文件日志。

在 `screen` 中，stdout 显示在对应 screen 窗口；改用 systemd 后，同一份 stdout 由 journald 接收，可通过以下命令查看：

```bash
journalctl -u eharchive-web -f
journalctl -u eharchive-supervisor -f
journalctl -u 'eharchive-operation@*.service'
```

原有文件日志继续保留：

- Web 继续在 `app.log_dir/web/` 创建会话日志；
- Supervisor 继续在 `app.log_dir/supervisor/` 创建主会话日志；
- Supervisor 启动的普通 Worker 继续通过 `EHARCHIVE_MAIN_LOG` 写入同一份 Supervisor 主日志；
- 普通 Worker 继续在 `app.log_dir/detail/<operation>/` 写入独立的人类可读运行报告；
- 特殊 Worker 继续在 `app.log_dir/special/<kind>/` 写入独立任务日志；
- Operation Executor 在 `<app.log_dir>/management/history/<operation-id>/operation.log` 写入独立操作日志。

因此部署后同时存在两种查看方式：

- `journalctl`：适合实时查看进程 stdout、启动失败和 systemd 退出码；
- 项目文件日志：适合按运行批次、任务和操作长期查看。

systemd unit 使用默认的 `StandardOutput=journal` 和 `StandardError=journal`，不把 journald 输出再次重定向到项目文件，避免额外复制。项目文件日志仍由 Python 日志模块自己写入。

## 13. 一键安装与人工控制

安装命令：

```bash
conda activate eh
eharchive --config-dir config service install
```

自动执行：

1. 检查 Linux、systemd、Git 和 root 权限；
2. 获取仓库、配置目录和当前 Conda Python 的绝对路径；
3. 检测当前 Git remote 和 branch；
4. 生成 `/etc/eharchive/management.toml`；
5. 在 `<app.log_dir>/management` 下创建状态、暂存和日志目录，单独创建 `/run/eharchive/` 作为固定锁目录；
6. 生成并安装三个 systemd unit；
7. 执行 `systemctl daemon-reload`；
8. 验证 unit 和路径；
9. 不执行 `systemctl enable`；
10. 不自动启动服务，除非传入 `--start`。

可选立即启动：

```bash
eharchive --config-dir config service install --start
```

项目 CLI：

```text
eharchive service install [--start]
eharchive service repair
eharchive service uninstall
eharchive service start [web|supervisor|all]
eharchive service stop [web|supervisor|all]
eharchive service restart [web|supervisor|all]
eharchive service status
eharchive service logs [web|supervisor|operation]

eharchive update check
eharchive update apply
eharchive operation show <operation-id>
eharchive operation list
```

原生 systemd 命令仍然可用：

```bash
systemctl start eharchive-web eharchive-supervisor
systemctl stop eharchive-web eharchive-supervisor
systemctl status eharchive-web eharchive-supervisor
journalctl -u eharchive-web -f
journalctl -u eharchive-supervisor -f
```

系统重启后服务保持停止，直到人工 start。

`service repair` 根据 management 配置重新生成 unit、补齐状态和日志目录并执行 daemon-reload，用于 Git 更新后部署定义发生变化的情况。

## 14. Web 系统页面

新增 `/system`。

### 14.1 总览

- Web unit 状态、PID 和启动时间；
- Supervisor unit 状态、PID、heartbeat 和控制状态；
- 当前 Worker；
- 当前 commit、branch 和 remote；
- 可用更新；
- 活动操作；
- 尚待应用的配置变更；
- 最近操作历史。

### 14.2 操作详情

`/system/operations/<id>` 展示：

- 操作类型和发起者；
- 状态机步骤；
- 当前阶段；
- 等待时间；
- 旧 commit 和目标 commit；
- 配置 revision；
- 错误码和错误摘要；
- 实时日志尾部；
- 允许执行的后续动作。

### 14.3 API

建议固定接口：

```text
GET  /api/system/status
GET  /api/system/git
POST /api/system/git/fetch
POST /api/system/operations
GET  /api/system/operations
GET  /api/system/operations/{id}
POST /api/system/operations/{id}/cancel
```

创建操作的 `kind` 只能来自固定枚举，参数使用独立模型校验，不接受命令字符串。

## 15. 故障恢复

### Executor 异常退出

- systemd 保存退出码和 journal；
- `state.json` 中保留最后 heartbeat 和阶段；
- Web 将操作显示为“执行器失联”；
- 用户可以执行恢复操作；
- 新 Executor 根据阶段判断应继续、回滚配置还是终止。

### Web 启动失败

- Operation Executor 仍在运行；
- Executor 写入失败状态和操作日志，用户通过 `systemctl status`、`journalctl` 和 operation log 手工排查；
- 不要求不可用的 Web 展示失败详情，不增加独立故障展示服务；
- 配置应用导致失败时自动恢复该配置的备份；
- Git 更新中的数据库迁移完成后启动失败时保留新 commit，不自动降级数据库，由用户手工处理。

### Supervisor 启动失败

- Web 若健康则继续提供管理页面；
- 操作记录启动错误；
- 保留 Supervisor 原控制状态；
- 修复后可从页面或 CLI 重新执行启动。

### 机器重启

- Web 和 Supervisor 不自动启动；
- 未完成操作根据状态文件显示为 `interrupted`；
- 人工启动 Web 后可查看操作停在哪个阶段；
- 不自动继续 Git 更新或强制恢复，避免在未知外部状态下继续执行。

## 16. 实现范围

预计新增：

```text
deployment/management.sample.toml
src/eh_archive/management/__init__.py
src/eh_archive/management/config.py
src/eh_archive/management/state.py
src/eh_archive/management/lock.py
src/eh_archive/management/systemd.py
src/eh_archive/management/git.py
src/eh_archive/management/configuration.py
src/eh_archive/management/executor.py
src/eh_archive/management/installer.py
src/eh_archive/web/templates/system.html
src/eh_archive/web/templates/system_operation.html
```

预计修改：

```text
src/eh_archive/cli.py
src/eh_archive/web/app.py
src/eh_archive/web/configuration.py
src/eh_archive/web/templates/base.html
src/eh_archive/web/templates/config.html
docs/deployment.md
docs/operations.md
docs/usage.zh-CN.md
```

## 17. 验收标准

### 安装与服务

- 一条命令生成管理配置、状态目录和全部 unit；
- 不要求人工编写 systemd 文件；
- 安装后没有服务被设为开机自启；
- Web、Supervisor 可分别通过 Web、CLI 和 systemctl 控制；
- systemd 和 Supervisor lease 共同防止重复 Supervisor。

### 配置

- 每个可编辑配置字段具有明确生效策略；
- `crawl.toml` 修改后下一批 Worker 生效，不重启常驻进程；
- `supervisor.toml` 只重启 Supervisor；
- Worker 相关 `app.toml` 字段不重启 Web；
- 公共配置变更能正确重启两个服务；
- 配置导致启动失败时自动恢复本次备份。

### 操作状态

- 所有管理操作写入独立操作目录；
- Web 重启后能继续显示同一操作；
- 操作步骤、错误和日志可以追溯；
- 并发点击不会启动冲突操作；
- 修改 `app.log_dir` 后仍使用 `/run/eharchive/deployment.lock`，不会产生新旧目录各自持锁的情况；
- drain 不提供强制停止并重启功能，卡死任务由用户手工处理；
- Executor 异常退出后不会把操作错误显示为成功。

### 更新

- 更新只作用于安装时固定的仓库、remote 和 branch；
- 工作区不干净或不能 fast-forward 时，在停服务前失败；
- Web 停止后更新继续运行；
- 配置校验和数据库 upgrade 在启动新服务前完成；
- 完成后原本运行的服务恢复运行并使用目标 commit，原本停止的服务保持停止；
- 数据库迁移命令失败时停止后续步骤并记录状态和日志，不要求自动恢复；
- 更新失败时记录失败阶段、服务状态和恢复结果；Web 无法启动时可通过日志手工排查。

## 18. 实现顺序

实现可以按模块拆分提交，但最终交付应完整包含：

1. 管理配置加载与自动生成；
2. 原子状态文件、事件日志、文件锁和操作状态机；
3. systemd unit 生成、安装与控制；
4. 配置候选、发布、重启范围和回滚；
5. Supervisor drain/restart；
6. Web 和全部服务重启；
7. Git 检查、更新、环境同步和数据库迁移；
8. Web 系统页面、操作详情和重连体验；
9. CLI 管理命令；
10. 单元测试、集成测试和 Linux 实机部署验证。

这些项目共同构成完整功能，不将 JSON 临时状态、screen 控制或人工 unit 文件作为过渡实现保留在最终设计中。
