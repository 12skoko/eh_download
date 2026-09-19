# 特殊模块使用与扩展

本次实现采用独立工作流、模块契约和可选业务关联。`video_archive` 与 `lanraragi_compare` 共用任务队列、租约、事件和资源互斥。普通 Supervisor 的任务选择、暂停、退避与回收流程保持原实现。

## 使用 LANraragi 核对

升级后打开 Web **特殊处理 → LANraragi 数据库核对 → 开始新的核对**。Supervisor 领取排队任务，详情页显示阶段、任务历史及报告入口。报告可分页浏览并下载完整 JSON。

核对范围为数据库 `completed` 档案与 LANraragi 标签中 EH / EX Gallery URL 提取的数字 GID，包含：

- 双方独有的 GID。
- 双方重复的 GID。
- 数据库无法解析的 ID。
- LANraragi 缺少有效来源标签的档案。
- LANraragi 独有 GID 在数据库中的其他状态。

核对不修改 Manga，不创建 Manga 关联，不执行删除、补采或自动修复。GID 相同不证明内容相同；两端采集时间分别记录，不属于跨系统原子快照。HTTP 错误、鉴权错误、非法 JSON 和错误响应结构均使任务失败，只有有效空列表才表示远端空库。

“重新核对”创建新的工作流，保留历史。同一实例已有活动运行时拒绝重复创建；失败后可以手动重试，也可以取消后创建新运行。取消正在运行的核对会等到安全检查点，HTTP 请求受配置超时约束。

配置示例见 [`config.sample/special/lanraragi_compare.toml`](../config.sample/special/lanraragi_compare.toml)：

```toml
enabled = true
max_concurrency = 1
timeout_seconds = 120
```

复制到 `config/special/lanraragi_compare.toml` 可覆盖默认值；没有该文件时使用以上默认值。URL 与认证继续使用现有 `app.toml` 和 `secrets.toml`。目录页只读本地配置，不访问 LANraragi。修改 Supervisor 启用项或模块并发配置后重启 Supervisor。

CLI 与 Web 使用同一个模块命令服务：

```powershell
conda activate eh
eharchive --config-dir config special create lanraragi_compare
eharchive --config-dir config special action 123 retry --row-version 7
eharchive --config-dir config special action 123 cancel --row-version 7
```

原 `scripts/compare_lanraragi_database.py` 命令、输入参数、输出 JSON 结构保留。解析与基础比较逻辑已经抽到主包，脚本和 Web 使用同一实现。

## LANraragi 元数据更新

Web **特殊处理 → LANraragi 元数据更新**，或档案详情页的 **更新元数据** 按钮。
在同一个选择区域输入或批量粘贴完整 Gallery ID，档案以横向换行的标签显示，支持逐项删除和清空。
也可点击 **加入元数据不一致的待复核档案**，将这些任务追加到列表并去重。
提交时只处理列表中最终保留的 ID。
每批默认最多 500 个档案，超过上限需指定 ID 分批执行。

1. **生成差异预览**：读取本地详细元数据和 LANraragi，展示标题差异、待补入/移除的标签。
   预览不修改远端或档案状态。仅接受无普通任务/租约的 `completed` 档案，或因
   `upload / lrr_metadata_mismatch` 进入 `manual_review` 的档案；有其他活动特殊工作流时拒绝接管。
2. **确认更新并校验**：以本地保存的信息更新标题和标签，不重新从 E 站抓取，也不上传文件。
   原有 `date_added` 保留；不改摘要。标签按精确文本去重、去首尾空白、忽略顺序比较，
   大小写和标签内部文字仍参与比较。已一致的档案跳过写入，只做复核。
3. 文件身份及元数据确认通过后补回 `lrr_archive_id`。原来是 `completed` 的保持完成；
   指定元数据错误的 `manual_review` 通过公共仓储的 `confirm_uploaded` 状态转换进入
   `uploaded`，再由普通 cleanup 流程完成清理。失败的档案保留原有状态和错误。

定位顺序为主记录 ID、当前产物 generation 对应的最新上传尝试 `expected_archive_id`、
远端标签中的完整 EH/EX 来源 ID。来源回退要求唯一命中。每次读写前都检查远端 ID、文件名、
字节大小及已有来源标签；缺少本地文件信息、来源冲突或多重匹配时只报告，不猜测目标。
后续普通上传遇到 `lrr_metadata_mismatch` 时，也会保留已通过文件身份校验的 LANraragi ID。

预览与确认之间检查本地行版本、产物 generation 和元数据指纹，执行时再次检查远端预览快照。
有变化则要求新预览。确认后由 Manga 集成组件临时接管可处理的档案，并持有工作流资源；
网络请求均在短事务之外。逐个成功结果和状态恢复在租约校验事务中提交，失败/取消恢复剩余
接管状态。停止不撤销远端已经发生的写入；未完成确认的写入需要重新预览。
过期 Worker 必须先确认已停止，再解除租约；重试总是重新生成预览，不自动重放写入。

报告可在任务详情中分页查看或下载 JSON；中途停止生成部分结果，已确认成功的档案也在详情中列出。
新模块仅在 `special/catalog.py` 注册，没有在 Supervisor、通用 worker 或核心仓储中增加模块分支。
网络比较/更新位于 `services/lanraragi_metadata.py`，业务状态协调位于模块的 `integration.py`。

可选配置 `config/special/lanraragi_metadata.toml` 见同名 sample；不存在时使用默认值。
URL 和认证复用现有配置。无需数据库迁移；更新代码后重启 Web 和 Supervisor 才会加载新模块。

```powershell
conda activate eh
eharchive --config-dir config special create lanraragi_metadata --inputs '{"manga_ids":["4194902/07c0c45119"]}'
eharchive --config-dir config special create lanraragi_metadata --inputs '{"mismatch_only":true}'
# 查看预览后，用详情页的当前 row_version 确认：
eharchive --config-dir config special action 123 confirm --row-version 7 --inputs '{"confirmed":true}'
```

验证（2026-09-19）：新模块 20 项隔离测试及上传、特殊模块、Supervisor 回归共 96 项通过；
Ruff 与 `git diff --check` 通过。测试包含 Web/CSRF、重复标签、身份冲突、批量部分失败、
预览后本地/远端变化、取消、过期租约和更新回读不一致。测试文件保留在 Git 忽略的 `tests/`。
另行运行的旧下载残留清理测试有 12 项失败，与其使用已移除的 `only_id` 接口有关；
该接口在 HEAD 中同样不存在，本次未修改清理模块或其通用路由。
四个用户提供的真实样本只读预览均为内容一致，未对生产任务或远端执行恢复/更新。

## 视频模块不改变操作流程

五个后台操作仍是加载候选、提交选中种子、检查并整合、取消、完成后源清理。

进入视频处理、种子选择、单个/批量检查与单个/批量源清理仍由用户触发。检查未就绪时回到 `downloading`，不自动排下一次检查。整合任务内连续完成转换、打包和登记，Manga 返回 `downloaded`，随后由普通流程校验、上传和清理。Manga 达到 `completed` 后才允许手动源清理；清理失败不会撤销整合结果。

原视频 URL 与 `special video-archive collect-ready`、`cleanup-completed` CLI 是继续支持的公开入口，调用最终模块实现。`special/service.py`、`repository.py`、`video.py` 等原导入路径只作公开导出，没有旧模型、双写或第二套执行器。

## 数据库升级

最终 DDL 与字段说明见[重构计划第 4 节](plan/SPECIAL_MODULE_REFACTOR_PLAN.md)。本次迁移为 `0015_special_modules`，接在仓库现有的 `0014_screening_pipeline` 之后。

| 对象 | 本次变化 |
| --- | --- |
| `special_workflow` | 删除 `manga_id`、`resume_status` 及旧活动 Manga 唯一索引；新增 `schema_version`、`cancel_requested_at`、`resource_claims`，对应检查与 GIN 索引 |
| `special_workflow_manga` | 唯一新增表：`workflow_id`、`manga_id`、`resume_status`、`context`；联合主键、两个 RESTRICT 外键及 Manga 查询索引 |
| `event_log` | 增加 workflow/job 两个表达式索引；字段不变 |
| `special_job`、`manga`、`job_attempt` | 本次迁移不修改结构 |

历史视频工作流 ID、任务 ID、状态和业务 payload 保留。`payload.entry` 移入关联 `context.entry`，取消请求移入核心时间字段。历史版本基线未知时保留 `null`，下一次合法领取建立基线，不冒用当前版本作为历史证据。无法核实的资源所有权保守保留；冲突数据阻止升级。

正式升级采用维护窗口：暂停领取，等待 Worker 结束，停止 Web / CLI / Supervisor 写入，备份数据库，然后执行：

```powershell
conda activate eh
eharchive --config-dir config db upgrade
```

迁移发现 `running` 特殊任务会拒绝执行。过期租约也需要先核实旧进程已经停止，再通过原界面解除，不能仅因时间到期就认为外部任务已经停止。完整切换 Web、Supervisor 和 Worker 后再恢复领取。

升级脚本不直接降级到旧结构，因为无 Manga 的工作流不能映射回旧表；回滚需维护窗口前备份与对应旧程序，发生外部副作用后先核实远端状态。本次开发只在隔离 schema 中演练迁移，没有对运行中的业务库执行升级。

## 新增模块

1. 在 `special/modules/<kind>/` 实现定义、配置、创建服务和执行器。
2. 在 `special/catalog.py` 增加一个 `ModuleRegistration`。这里是模块装配入口，通用 worker、仓储和 Supervisor 不增加模块分支。
3. 可选注册 dashboard、detail、模板和 `install_routes`，复杂模块可提供专用路由；所有动作仍需服务端验证与既有认证、CSRF 保护。
4. 可选通过集成组件关联业务实体。Manga 组件支持零个/一个/多个关联；视频集成额外要求恰好一个。其他实体按真实业务建立专用存储，不伪造 Manga ID。

`WorkflowDefinition` 声明数据版本、可读取版本、迁移函数、创建入口、动作与集成组件。`OperationDefinition` 声明允许生命周期/阶段、运行阶段、输入校验、租约、可选执行期限、副作用类别与重试策略。

操作结果可以使用 `OperationResult` + `repository.commit_result()` 表达完成、人工等待、延迟后续操作或完成后的维护操作。`next_operation` 未显式指定时，不自动排后续任务。自动重试默认关闭；明确声明错误白名单、最大次数和退避后才启用。需要核实的外部写入已经开始时不盲目自动重试。

模块可使用 `ExecutionContext` 的短事务、进度与输出接口。worker 自动续租只更新 job 租约，不推进阶段；每次结果提交仍独立验证执行身份。可选操作执行期限到达后拒绝续租和提交，外部调用应同时设置自己的网络/子进程超时，并在循环中检查执行上下文。期限失效不代表业务取消成功，也不会自动释放未核实占用。

自定义动作在 `definition.actions` 注册；Web 通用路由为 `POST /special/workflows/{id}/actions/{action}`。默认支持手动重试、取消、已确认旧 Worker 停止后的只读任务过期恢复和 `migrate-data`。数据升级要求没有 queued/running job；不支持的历史版本禁止领取，历史与输出仍可查看。未安装模块同样回退到通用历史页。

### 事务与资源协议

所有领取、资源申请/释放、进度和结果事务统一先取得 `pg_advisory_xact_lock(697321408214)`，再锁工作流、任务或关联业务行。不要在已持有业务行锁后进入核心资源 API，不要在这些短事务内访问网络或执行文件转换。

资源持久化在 `special_workflow.resource_claims`。workflow 范围覆盖人工等待阶段；job 范围用于单次操作或终态维护。同一个资源键在两个范围间仍互斥。一次申请整组资源，冲突时不写入任何一项。全局和模块并发数从数据库中统计 running job，多 Supervisor 共享限制。

资源字段只能由核心 API 更新。不要依赖 JSON 本身提供跨行唯一性，也不要用进程会话锁代替持久化占用。未核实旧进程的租约到期不释放 workflow 占用。

Manga 集成通过真实外键、原有状态/普通任务/租约检查、行版本与产物 generation 协调普通流程。框架不把普通流程接入新的资源锁系统。

### 输出与恢复

报告存储于 `app.log_dir/special_outputs/<workflow>/<job>/<lease-token>/`，按临时文件写入、fsync、原子替换发布。执行凭据隔离不同尝试的文件路径，旧 Worker 不能覆盖新尝试的文件。

`payload.outputs` 只保存少量引用：`id`、`job_id`、`name`、`media_type`、`size_bytes`、`storage_key`、`created_at`。清单注册与业务完成应在同一个经过租约检查的事务中完成，核对模块已如此实现。大量条目保存在报告文件中，Web 每页最多展示 100 条。

文件已发布而数据库提交失败时，未被清单引用的文件是孤立输出，不展示为成功结果。重新执行使用新的 job/lease 路径，不把孤立输出冒认为本次结果。维护人员可将目录文件与清单引用核对后处理孤立文件；框架不自动删除未核实的文件。文件缺失会明确显示“输出不可用”，不显示空报告。

## 验证

```powershell
conda activate eh
python -m pytest -q tests/test_special_modules.py tests/test_supervisor.py

# 使用配置的 PostgreSQL，只创建并清理 codex_special_test_<随机值> schema。
$env:EH_TEST_POSTGRES = '1'
python -m pytest -q tests/test_special_postgres.py

# 可选：只读复制配置库 public 中的旧视频数据到隔离 schema 演练。
# 只在源库尚未迁移时使用。
$env:EH_TEST_LEGACY_COPY = '1'
python -m pytest -q tests/test_special_postgres.py
```

SQLite 测试覆盖业务与 Web；真实 PostgreSQL 测试验证特殊模块增量迁移、约束、事务回滚和多连接并发。测试不会调用真实 qBittorrent、转换器或对 LANraragi 执行修改操作。

### 本次验证结果（2026-09-13）

- 新增 22 项业务、契约和 Web 测试通过；5 项真实 PostgreSQL 测试通过。
- 回归测试排除已确认的旧基线问题后，347 项通过；1 项 Linux 专属测试在 Windows 跳过。PostgreSQL 用例另行启用并全部通过。
- 旧基线问题：`test_web_import.py` 引用 HEAD 中不存在的 `_gallery_id`；CLI 互斥参数断言与当前实现不符；首页“确认排空”文案断言与当前页面不符。后两项已在未修改的 HEAD 副本中复现。
- 浏览器实测通过：模块目录、核对历史、中文统计、报告分组、重新核对、取消排队任务，以及原视频列表和操作面板。
- 真实数据库历史副本演练保留现有 4 个视频工作流与任务，测试 schema 均已清理。
- 修改涉及的特殊模块代码通过 Ruff 检查，`git diff --check` 通过。未迁移正式业务库，也未触发真实视频转换或源文件删除。

### 总览与日志修复（2026-09-13 晚间，更新上述部署状态）

- 查明原 PostgreSQL 测试遗漏私有 `alembic_version`，使业务库版本标记前移而结构未升级；此前“未迁移正式业务库”的描述不完整，实际曾误写版本标记。现已创建私有版本表并增加业务库标记不变断言。
- 已备份并在受锁事务中执行 `0016_repair_special_schema` 修复，业务库结构与版本一致，保留 4 个视频工作流关联及全部任务。备份位于 `logs/tools/special_schema_backup_20260913_225543.json`。
- 使用真实数据库渲染总览返回 HTTP 200。Web 启动入口将 Uvicorn 启动、请求和异常日志统一写入配置日志文件，重启 Web 后生效。
- 本次 23 项业务/Web 日志测试及 6 项 PostgreSQL 测试通过；旧历史复制用例因源库已迁移不再执行。
- 完整空库迁移链并未通过验证：隔离问题修正后发现旧 `0011_conflict_rename` 与当前元数据重复添加字段。当前测试明确覆盖 `0015`、`0016` 特殊模块迁移，不再宣称完整空库迁移链通过。
