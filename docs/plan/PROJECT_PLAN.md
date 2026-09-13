# EH Archive 重构项目计划

## 1. 文档目的

本文档定义 **EH Archive** 的长期重构方向、目标架构、数据库结构、状态控制、迁移策略、跨平台要求和分阶段实施计划。

本次重构以旧程序已经验证可用的业务逻辑为基础：采集方式、页面解析、种子选择、H@H/直接下载、压缩、LANraragi 上传和清理规则原则上继续沿用。重构重点是控制流程、错误恢复、数据一致性、可观测性、可维护性和跨平台运行能力。

本文档中的架构决策从第一阶段开始生效。后续阶段只能在此基础上补充功能，不临时更换数据库、任务模型或运行方式。

### 1.1 当前实现基线（2026-08-25）

- 阶段 1 至阶段 7 的主体功能已经落地，当前 Alembic schema head 为 `0012_attempt_progress`。
- 阶段 8 只完成了跨平台路径约束、部署说明、健康快照和 Web 运维入口；仓库尚未提供可直接安装的 systemd/Windows 服务模板、内置日志轮转以及 PostgreSQL 备份恢复脚本。
- 阶段 9 的 MySQL 迁移、校验、对账和历史下载目录清理工具已经存在，但生产迁移演练、正式切换和回退窗口属于部署现场工作，不能仅由仓库代码判定完成。
- 阶段 10 仍是后续增强项。本文后续各阶段保留为设计与验收清单；描述具体运行行为时，以当前代码和迁移为准。

## 2. 已确认的总体决策

- 项目正式名称固定为 **EH Archive**；Python 包名使用 `eh_archive`，命令和服务名称统一使用 `eharchive` 前缀。
- 新数据库固定使用 PostgreSQL。
- 旧 MySQL 数据通过根目录 `scripts/` 中的一次性迁移脚本迁移到 PostgreSQL，`manga` 和 `mangainfo` 中的业务数据必须完整保留；旧字段映射和 MySQL 访问代码不进入主程序包。
- `GP` 表不进入新业务数据库；点数不足已经不再是主要运行约束。
- `random` 表不进入新业务数据库；`relatetation` 不再参与新程序逻辑。
- 旧 MySQL 在正式切换后作为只读历史库保留；新程序不用的字段不迁入 PostgreSQL，需要时只能由 `scripts/` 中的人工查询工具读取。Web、Supervisor、任务和主业务服务不连接 MySQL；旧 MySQL 停止或离线不会影响新系统运行。
- 删除旧的 `main/old/special` 三种 `run_mode`。
- 删除 `state/autostate` 双状态控制，新程序只使用一套状态。
- 高优先级不再编码进状态，统一使用整数 `priority`。
- 不使用 Redis、Celery等外部队列。
- 外部业务应用只要求 qBittorrent 和 LANraragi。
- Python 直接下载作为主要直接下载实现；为 aria2 和 H@H 保留适配接口，但不把它们设为必需组件。
- 程序由一个常驻 Web 进程和一个常驻 Supervisor 进程组成。
- Supervisor 按数据库状态启动有限生命周期的任务子进程；Web 不执行实际任务。
- torrent-download 与 direct-download 是两个独立控制任务槽，并固定为各自最多一个，可同时运行。torrent-download 的单实例限制只约束 EH Archive 串行查找、下载和校验 `.torrent` 文件、提交 qBittorrent 以及轮询状态，不限制已经提交到 qBittorrent 的后台下载数量；qBittorrent 实际传输并发完全由其自身配置决定。
- 虚拟环境是平台相关的可重建产物，不随项目跨平台迁移。
- 从第一阶段开始遵守 Windows/Linux 跨平台约束。
- `queue_source` 固定记录任务来源：`automatic`（定时采集）或 `manual`（手工 URL、人工加入或人工覆盖）。来源不参与业务分支，只用于审计、筛选和迁移。
- 每一次阶段执行都创建一个持久化 `attempt`；档案领取、状态提交、产物替换和有外部副作用的调用必须使用 fencing 条件，过期 attempt 不得写回新结果。
- 文件位置不在数据库保存绝对路径或通用相对路径。数据库只保存受控的 `artifact_location` 配置键、文件名、文件类型和产物代次；实际根目录全部由配置文件提供。
- 首次选择和提交 torrent 前必须取得完整 MangaInfo；direct/H@H 和上传也必须通过各自的详情前置检查。
- LANraragi 使用 Archive API：上传响应必须给出合法的 40 位十六进制 archive ID，并通过该 ID 的 metadata 查询确认后才能标记成功。当前实现不会假定 archive ID 等于本地产物 SHA1，也不会在上传结果不确定时自动按 SHA1 搜索远端档案。

## 3. 旧程序流程基线

### 3.1 旧调度方式

旧 `main.py` 使用 APScheduler 定时启动四个独立脚本：

1. `collect.py`：采集列表页和基础档案信息。
2. `download_torrent.py`：优先查找种子并推送 qBittorrent。
3. `download_hah.py`：处理无种子或种子失败的档案。
4. `complete_download.py`：检查完成、压缩、补充 MangaInfo、上传、删除过时档案和清理。

脚本通过 MySQL 中的 `state/autostate` 交换控制信息。任意脚本非零退出后，旧 `main.py` 会停止调度并发送通知。这一“系统性错误应停止”的原则保留，但需要与单个档案错误和临时网络错误区分。

### 3.2 旧 run_mode

- `main`：处理自动采集的新档案，使用 `autostate`，qBittorrent 分类为 `autoeh`，每次运行一轮后退出。
- `old`：处理历史存量，使用 `state`，qBittorrent 分类为 `eh`。
- `special`：仍使用 `state`，通过 `13/14/15` 编码高优先级，qBittorrent 分类为 `speh`。

新程序不再保留这三套模式。所有档案进入同一流程，手工优先档案只提高 `priority`。

### 3.3 Manga 与 MangaInfo 的旧获取时机

旧程序中，列表采集只创建 `Manga` 基础信息。

- torrent 路线：先完成 qBittorrent 下载，再访问详情页并创建 `MangaInfo`。
- H@H/direct/aria2 路线：必须先访问详情页获得归档入口，因此在下载前创建 `MangaInfo`。

新程序统一详情服务和幂等写入方式，并把完整 MangaInfo 设为首次选择和提交 torrent 的前置条件。种子选择使用 `estimated_size_raw` 检查异常小的候选；详情缺失时由 torrent-download 现场获取并幂等写入，获取或字段校验失败时不得提交种子。已经提交到 qBittorrent 的任务只按 hash 轮询，不重复获取详情。torrent 失败后切换 direct/H@H 时仍必须取得有效的当次归档入口；任何下载方式在进入上传前都必须保证 MangaInfo 完整。

### 3.4 双账号与限额

旧程序使用两套 Cookie：

- browse 账号：用于列表、详情、种子页面和 torrent 文件；限额主要绑定 IP，因此可使用代理池轮换。
- archive 账号：拥有点数，用于 H@H、direct、aria2 和点数相关请求；限额绑定账号，换 IP 无法解除，因此使用固定网络策略。

新程序使用“角色绑定”而不是“单账号/双账号模式”：

- `browse_session`：普通浏览和 torrent 流程。
- `archive_session`：所有可能消耗点数的归档流程。

单账号配置让两个角色指向同一个账号；双账号配置让两个角色分别指向不同账号。业务代码不判断用户拥有几个账号。

## 4. 目标运行架构

```text
Browser
   |
FastAPI Web
   |
PostgreSQL
   |
Supervisor
   |-- collect task
   |-- torrent-download task
   |-- direct-download task
   |-- validate task
   |-- prepare task
   |-- upload task
   |-- cleanup task
   `-- outdated-delete task

External: qBittorrent, LANraragi
Optional adapters: aria2, H@H Downloader
```

### 4.1 Web 进程

Web 只负责管理和展示：

- 查看档案、状态、优先级、当前错误和事件历史。
- 查看和编辑档案人工备注。
- 查看 Supervisor、qBittorrent 和 LANraragi 健康状态。
- 手工添加 URL，并设置优先级。
- 暂停或恢复全部流程或某个组件。
- 重试、跳过、隔离、恢复、取消或标记人工处理。
- 查看当前运行任务、租约和下次重试时间。
- 查看迁移和运行统计。

Web 不直接执行采集、下载、压缩、上传、文件删除或 qBittorrent 操作。Web 的控制动作只更新 PostgreSQL，实际操作由 Supervisor 或任务子进程完成。

### 4.2 Supervisor 进程

Supervisor 是唯一的统筹调度者：

- 维护定时采集计划。
- 查询各状态是否存在可执行档案。
- 确保同类任务不重复启动。
- 分别限制 torrent-download、direct-download、验证、压缩和上传并发。
- 启动任务子进程并记录运行标识。
- 解释任务退出码，区分正常完成、临时错误、档案级错误和系统级错误。
- 拒绝过期 attempt 写回；过期租约由管理网页在人工确认旧进程已停止后解除并转入 `manual_review`，Supervisor 当前不自动接管。
- 响应 Web 写入的暂停、恢复和人工重试操作。
- 检查 PostgreSQL、存储目录、qBittorrent 和 LANraragi 健康状态。

Supervisor 本身不执行长时间网络或文件任务，避免一次上传卡住整个调度循环。

### 4.3 任务子进程

任务子进程按职责拆分，但共享同一项目、配置、数据库访问层和状态迁移服务。每个子进程处理有限批次或运行有限时间后退出。

建议初始限制：

- collect：同一时间最多一个。
- torrent-download：同一时间最多一个任务子进程；在有限批次内逐条处理档案，任意时刻只对一个档案执行查找种子、下载并校验 `.torrent`、推送 qBittorrent 和确认 hash。提交确认后结束该档案 attempt，可以继续处理下一条；Python 不实现 BitTorrent 传输。
- direct-download：同一时间最多一个任务子进程；负责 Python direct，并为 aria2/H@H adapter 保留入口。
- torrent-download 与 direct-download 可以同时运行，互不占用对方的并发名额。
- 已提交到 qBittorrent 的任务不占用 torrent-download 控制任务槽，也不持有 Python 租约。数据库中可以同时存在多个 `download_method=torrent,status=downloading` 的档案，例如 10 个种子同时由 qBittorrent 下载。
- validate：同一时间最多一个。
- prepare：同一时间最多一个，避免与大量上传争用磁盘。
- upload：同一时间最多一个。
- cleanup：同一时间最多一个。
- outdated-delete：同一时间最多一个。

并发限制必须可配置，但默认值保持保守。

## 5. 新业务流程

### 5.1 正常流程

```text
discovered
   |-- skipped
   |-- unavailable
   |-- deferred
   `-- 筛选通过 -> download_pending
         `-- 获取完整 MangaInfo（失败时保持等待，不提交 torrent）

download_pending
   `-- downloading
         |-- torrent 成功
         |-- torrent 无种/失败 -> direct/hah/aria2
         `-- downloaded

downloaded
   `-- validating
         |-- quarantined
         |-- manual_review
         |-- preparing
         `-- upload_pending

preparing
   `-- upload_pending

upload_pending
   `-- uploading
         |-- upload_pending（明确未接收，可重试）
         |-- manual_review（无法自动判断）
         `-- uploaded

uploaded
   `-- 清理成功 -> completed

outdated
   `-- 远端和本地旧数据删除确认 -> deleted
```

### 5.2 MangaInfo 统一获取规则

- 列表采集产生 `discovered`。
- 筛选通过且观察期结束后，进入 `download_pending`，同时尽早尝试使用 `browse_session` 获取详情页并写入 `mangainfo`。
- MangaInfo 获取失败会记录 `details` 操作的错误和重试时间；首次 torrent 选择和提交必须等待详情完整。
- 历史或人工调整可能使 `downloaded`、`validating` 暂时缺少 MangaInfo，但正常自动流程在首次选种前就会补齐详情；`upload_pending` 和 `uploading` 必须存在可上传的 MangaInfo。
- direct/H@H/aria2 需要生成归档入口时，必须使用 `archive_session` 执行 `ensure_details()`；详情获取失败时不能开始该次 direct/H@H attempt，档案留在安全的下载等待状态。
- torrent 完成后，如果 MangaInfo 仍缺失，只重试详情获取，不重新下载 torrent；详情成功后才允许继续上传前置检查。
- direct fallback 时可以再次访问详情页并刷新 MangaInfo。刷新和 parent 关系更新必须幂等，临时下载链接不入库、不写日志。
- MangaInfo 的“完整”必须由字段校验器定义，至少包括上传所需标题、来源链接、分类、上传者、发布时间、语言、页数和标签字段；缺失字段只能阻塞上传，不得把文件标为已完成。

### 5.3 下载选择

- 默认优先 torrent。
- torrent-download 按 priority 和入队时间串行领取档案；对当前档案查找种子、下载并校验 `.torrent` 文件，然后推送 qBittorrent。同一时刻不并行获取或提交多个 `.torrent`。
- 首次选择种子前必须保证 MangaInfo 完整且 `estimated_size_raw` 可以解析。torrent 页面使用 HTML 结构解析；`Outdated Torrents` 分区及红色时间的种子无条件忽略，仅剩过时种子时 fallback。非过时种子中只要存在视频标记且 remark 不含 `skip video` 就进入 `manual_review`，即使该种子同时带有重采样标记；remark 含 `skip video` 时视频种子按普通种子处理。明确的 `1280x/800x/1920x/2560x` 重采样直接忽略。
- 小于 `estimated_size_raw` 60% 的候选视为异常；所有候选都过小时进入 `manual_review`。对其余候选使用“同时更大且更新”淘汰旧版本；唯一胜出版本没有 Seeder、或剩余不同大小版本无法比较时进入 `manual_review`。剩余候选大小全部相同时先选 Seeder 最多者，Seeder 相同时选发布时间最新者。
- qBittorrent 类别固定为精确字符串 `eharchive`，它同时是程序对种子任务的所有权边界。提交和按 save_path 反查 hash 只使用该类别；轮询发现任务类别为空或不是 `eharchive` 时不得检查错误、标签、停滞或完成状态，不读取产物、不 fallback、不删除任务，数据库保持 `downloading`。任务移回 `eharchive` 后恢复轮询。fallback 删除前必须重新确认类别；cleanup 对已移出类别的任务跳过 qBittorrent 删除，但继续清理程序自己的产物并完成业务流程。
- qBittorrent 返回稳定 hash 且可按 hash 查询到任务后，设置 `download_method=torrent`、保存 `external_download_id` 并进入 `downloading`，随即释放该档案的 attempt 和控制任务处理名额。
- 已进入 qBittorrent 的后台任务不受 EH Archive 的单实例限制；允许多个档案同时保持 `downloading` 并由 qBittorrent 并行传输，具体数量、排队和限速服从 qBittorrent 自身配置。
- qBittorrent 完成后进入 `downloaded`。
- 没有种子、种子长期无进度或确认失败后，切换到配置允许的回退方式。
- torrent fallback 到 direct/H@H/aria2 前，必须先通过 `ensure_details()` 获取有效详情和当次归档入口；详情失败只阻塞回退方式，不影响其他档案或仍在运行的 torrent。
- Python 直接下载对应 `download_method=direct`。
- aria2 对应 `download_method=aria2`，作为可选适配器。
- H@H Downloader 对应 `download_method=hah`，作为可选适配器。
- 下载方式可以在重试或回退时改变，历史变化写入事件表。

### 5.4 直接下载要求

- 获取下载入口和实际传输使用同一个 `archive_session` 配置。
- Python 下载支持流式写入和 `.part` 临时文件；临时文件名包含安全化 manga_id、目标 artifact generation 和 attempt ID，避免不同 attempt 共用同一个临时文件。
- 支持 HTTP Range 时进行断点续传。
- 明确设置连接超时、读取超时和整体任务期限。
- 重试使用指数退避和随机抖动。
- 长时间重试前重新获取可能已经过期的归档入口。
- aria2 适配器必须传递必要的 Cookie、User-Agent、Referer 和代理配置。
- aria2 任务完成只代表传输结束，不代表文件有效；所有方式都必须进入统一验证流程。

### 5.5 产物验证

从 `downloaded` 进入 `validating` 后执行：

- 确认文件或目录真实存在。
- 确认大小稳定，下载进程不再写入。
- 登记文件大小；最终 ZIP 另外登记 LANraragi 所需的 SHA-1。
- ZIP 使用格式识别、目录读取和 CRC 检查。
- 拒绝 HTML、JSON、纯文本错误页面或其他不符合预期的内容。
- 确认归档非空，并包含合理的内容文件。
- 有预计大小或页数时进行宽松一致性检查。
- 无效产物移动到隔离目录，状态设为 `quarantined`，禁止上传。
- 无法安全判断的产物进入 `manual_review`。
- 已经是可上传归档的产物进入 `upload_pending`。
- 下载结果为目录时进入 `preparing`。

验证不能只依赖文件大小阈值，14KB 错误页面必须通过格式和结构检查识别。

### 5.6 压缩和准备

- 压缩到目标存储根目录中的 attempt 专属临时文件，不直接写最终文件名。
- 压缩完成后再次验证 ZIP 结构和 CRC。
- 自动生成的正式文件名包含安全化 manga_id 和 artifact generation；每个 generation 使用新文件名，不覆盖上一 generation。人工登记或冲突改名的既有文件名由受控路径校验保护。
- 临时文件与最终文件位于同一文件系统时，验证成功后使用原子重命名生成最终产物。
- 源产物与目标根目录跨文件系统时，不实现复杂的跨盘事务、断点复制或双向回滚。任务直接复制到目标根目录中的 attempt 临时文件，完整写入并验证后，再在目标文件系统内原子重命名为正式文件。
- 跨盘复制中断只允许留下 attempt 临时文件，数据库继续指向原 generation；该临时文件不得被上传任务识别，后续恢复任务可以删除并重新生成。
- 进程中断留下的临时文件可以安全删除或继续，不得被上传任务识别为正式产物。
- 成功后进入 `upload_pending`。

### 5.7 LANraragi 上传

- 使用 `PUT /api/archives/upload` 的 `multipart/form-data`，Authorization 使用配置中的 Bearer API key。
- multipart 字段固定为：`file`、`file_checksum`、可选 `category_id`、`tags`、`title` 和 `summary`。`file_checksum` 必须发送最终 ZIP 首次验证时登记的 SHA-1。
- 上传前再次确认受控位置、文件类型和大小与当前 artifact generation 登记值一致，并确认 MangaInfo 和已登记的 SHA-1 完整；可信的受控文件不会在上传前重复计算哈希。
- 对被 upload 任务领取的文件使用流式 multipart，避免完整读入内存；连接、写入和响应等待分别设置超时。默认 `large_upload_threshold_bytes=2147483648`，达到或超过阈值的档案保留在 `upload_pending`，等待后续的大文件传输方式；设为 `0` 才会让普通 HTTP 上传领取所有大小的文件。
- HTTP 200 只有在 JSON 同时满足 `operation=upload`、`success=1`、`id` 为 40 位十六进制字符串，并且随后按该 ID 查询 metadata 成功时才算成功。当前实现不比较 archive ID 与本地文件 SHA1 是否相等。
- HTTP 409 表示 LANraragi 判断档案重复。系统记录完整响应并直接进入 `manual_review`，不自动把重复判断转换为上传成功，也不自动登记 `lrr_archive_id`。人工核对现有档案后，可以经审计登记已有 archive ID 并进入 `uploaded`，或选择其他安全恢复路径。
- 上传请求体已经开始发送但响应丢失、进程退出或返回 500 时，当前实现直接进入 `manual_review`，禁止自动重传；只有响应已经返回合法 archive ID 时才会按该 ID 查询 metadata。
- HTTP 400/415/417/422 分别按请求非法、文件类型不支持、SHA1 校验失败和业务校验失败处理：不自动重传，记录稳定错误码；417 必须清空已登记的 SHA-1，并回到 `validating` 重新计算。
- HTTP 423 表示远端资源锁定，作为临时错误有限退避；401/403、磁盘不足、数据库不可用等系统性错误暂停上传组件。
- 上传成功后再调用 `GET /api/archives/{id}/metadata` 做一次远端确认；只有确认成功才允许进入 `uploaded`。
- 缩略图生成使用 `/api/archives/{id}/files/thumbnails` 作为独立批次收尾操作，不影响单个档案上传成功状态。

### 5.8 清理

- `uploaded` 本身表示“远端已确认、等待本地清理”，不增加 `cleanup_pending`。
- 清理任务持有租约期间状态仍为 `uploaded`。
- 只有远端已经确认的档案才允许删除本地产物和外部下载记录。
- 清理操作必须幂等：文件或外部记录已经不存在也视为该项完成。
- 全部清理项成功后进入 `completed`。
- 单项清理临时失败时保持 `uploaded`，记录错误并安排重试。
- 清理前使用已持久化的 40 位 `lrr_archive_id` 调用 `GET /api/archives/{id}/metadata` 确认远端仍存在。返回 400 表示 LANraragi 中的永久副本已经不存在：必须保留本地产物和外部下载记录，进入 `manual_review` 并记录 `lrr_archive_missing_before_cleanup`，不得把远端缺失当作本地清理的幂等完成。

### 5.9 过时档案

- 新旧关系通过 `superseded_by_id` 保存。
- 旧档案进入 `outdated` 后停止其其他自动任务。
- 管理网页把档案设为 `outdated` 时只要求填写数据库中存在且不是自身的替代档案 ID，不限制替代档案当时的状态。delete 任务只在替代档案至少进入 `download_pending` 或其后的正常流水线状态时领取旧档案。
- 使用 `DELETE /api/archives/{id}` 删除旧档案；只有响应 `success=1` 且随后 `GET /api/archives/{id}/metadata` 返回 400，才把远端删除项视为完成。
- 远端删除返回 423 或结果不明确时保持 `outdated`，禁止重复猜测删除结果；必要时进入 `manual_review`。
- 本地遗留和外部下载记录清理完成后进入 `deleted`。
- 数据库记录本身不物理删除，保留业务和事件历史。
- `force_delete_pending` 只能由管理网页从 `uploaded`、`completed`、`outdated` 或 `manual_review` 设置，必须填写原因并二次确认当前档案 ID。delete 任务会跳过替代档案就绪检查，但保留相同的远端删除、本地删除、fencing、审计和最终 `deleted` 状态。

## 6. 状态与控制模型

### 6.1 status 枚举

- `discovered`：已采集，等待筛选或入队
- `deferred`：暂缓处理，例如新发布档案等待观察期
- `download_pending`：等待选择或执行下载方式；首次选择 torrent 前必须补齐 MangaInfo
- `downloading`：下载器已接收或 Python 正在下载
- `downloaded`：下载完成并完成首次产物检查，等待独立验证任务
- `validating`：正在复核产物结构，并在最终 ZIP 缺少 SHA-1 时补充登记；状态名为兼顾流程可读性保留
- `preparing`：等待或正在压缩、整理待上传文件
- `upload_pending`：已有通过基础检查并登记 SHA-1 的待上传档案
- `uploading`：正在上传
- `uploaded`：LRR 已确认接收，尚未完成本地清理
- `completed`：上传和清理全部完成
- `quarantined`：产物异常，已隔离，禁止上传
- `manual_review`：无法安全自动判断，需要人工处理
- `skipped`：规则决定不下载
- `unavailable`：远端档案不可用
- `outdated`：被明确的新版本替代，等待删除旧 LRR 档案和本地遗留项
- `force_delete_pending`：仅由 Web 人工确认进入，等待 delete 无条件执行远端及本地删除
- `rename_pending`：仅由 Web 对同名冲突档案发起，等待 validate 改名并重新验证
- `deleted`：过时档案已完成删除
- `cancel_requested`：用户请求停止该档案的后续自动操作，等待当前子进程在安全边界退出
- `cancelled`：档案已停止自动处理；已有文件和远端档案不因取消请求自动删除

`status` 只表达档案当前所在的流程位置。它不编码来源、优先级、run_mode 或具体错误类型。

`queue_source` 是独立字段，取值只能为：

- `automatic`：scheduler 按 `crawl.toml` 发现的档案。
- `manual`：Web/CLI 显式加入的 URL、人工加入或人工覆盖筛选结果的档案。

取消操作也必须经过状态迁移服务：可执行状态先进入 `cancel_requested`，子进程在不破坏文件和外部任务的安全边界退出后才进入 `cancelled`；上传请求已经发送但结果未知时不得用取消绕过 `manual_review`。

### 6.2 priority

- 普通自动任务默认为 `0`。
- 手工优先任务使用正整数，例如 `100`。
- 数值越大越优先。
- 同优先级按进入队列时间或创建时间排序，防止结果随机。
- 旧 `special` 状态迁移为正常状态加高优先级，不保留 special 模式。

典型领取顺序：

```text
priority DESC, next_retry_at ASC NULLS FIRST, created_at ASC
```

### 6.3 download_method

```text
torrent
direct
hah
aria2
NULL
```

- `direct` 明确表示 Python 直接下载。
- `NULL` 表示尚未选择方式。
- 下载方式不决定是否成功；成功、等待和错误由 `status` 及错误字段表达。

### 6.4 延迟、重试和错误

- `defer_until`：观察期结束时间，仅 `deferred` 使用。
- `attempt_count`：当前逻辑操作的尝试次数。
- `next_retry_at`：临时错误后的下次可执行时间。
- `last_error_operation`：错误发生在 collect/torrent_download/direct_download/validate/prepare/upload/cleanup/delete 中的哪一步。
- `last_error_code`：稳定、可查询的机器错误码。
- `last_error_detail`：供人工诊断的详细信息。
- `last_error_at`：最近未解决错误时间。

不增加 `has_error`。当前错误由 `last_error_code IS NOT NULL` 表达，避免布尔值和错误内容不一致。

操作成功进入下一个稳定位置后：

- 清空当前错误字段。
- 清空 `next_retry_at`。
- 将 `attempt_count` 归零。
- 把本次成功和此前错误追加到事件表。

### 6.5 错误分级

#### 临时错误

示例：连接超时、连接重置、429、502、503、504、临时文件占用。

- 回到当前操作的安全等待状态。
- 设置 `next_retry_at`。
- 不停止其他档案。
- 超过配置次数后进入 `manual_review`，而不是无限重试。

#### 档案级确定错误

示例：无效归档、HTML 错误页面、无法解析的单个档案、远端明确不可用。

- 无效产物进入 `quarantined`。
- 远端不可用进入 `unavailable`。
- 需要判断的冲突进入 `manual_review`。
- 其他档案继续运行。

#### 系统级错误

示例：PostgreSQL 不可用、存储根目录不可访问、磁盘空间不足、认证失败、数据库版本不匹配、连续大量请求返回同一种限制页面。

- 暂停相关组件或全部流水线。
- 不把所有档案逐条改为失败。
- Web 显示暂停原因。
- 人工修复后由 Web 恢复。

### 6.6 Attempt、租约与 fencing

任务领取使用 PostgreSQL 事务和 `FOR UPDATE SKIP LOCKED`。

领取档案时在同一事务中：

1. 创建 `job_attempt`，记录 operation、attempt_no、触发来源、当前状态和当前 artifact generation。
2. 将 `job_attempt.id` 写入 `manga.active_attempt_id`。
3. 写入新的 `lease_token`、`lease_owner` 和 `lease_until`。
4. 将档案改为对应执行中状态并提交事务；网络和文件操作在事务外执行。

长任务定期续租。任何续租、状态提交、错误提交、产物替换或清理操作都必须同时匹配：

```text
manga_id
active_attempt_id
lease_token
artifact_generation（涉及文件时）
```

这组条件就是 fencing。条件更新影响 0 行表示 attempt 已经过期或产物已经被替换：旧进程必须立即停止写回，把自己的 attempt 标记为 `abandoned`；不得修改新状态、覆盖新文件、删除新产物或重发外部请求。

`row_version` 继续用于 Web 与 Supervisor 的乐观并发控制，但不能替代任务 fencing。`lease_until` 只说明何时允许接管，也不能单独证明旧进程已经停止。

在 qBittorrent 提交、LANraragi 上传、LANraragi 删除等可能产生外部副作用的调用前，attempt 必须先持久化 `external_effect_started_at`。获得稳定外部 ID 后立即写入 `external_task_id`。进程崩溃后，恢复逻辑根据该标记和稳定 ID决定查询外部系统、继续、人工审核或安全重试。

每次重新下载、从目录生成 ZIP、移动到 quarantine 或人工替换文件时都递增 `artifact_generation`。自动生成的正式文件名至少包含安全化 manga_id 和 generation，例如 `<safe_manga_id>.g2.zip`；临时文件名还包含 attempt ID，例如 `<safe_manga_id>.g2.a123.tmp`。安全化规则必须确定、跨平台一致并检测截断后的名称冲突。人工登记和冲突改名的文件名仍必须通过同一受控路径与不覆盖检查。

产生新 generation 的 attempt 先基于领取时观察到的旧 generation 工作，只写自己的 attempt 临时文件。临时产物完整验证后，在提交前再次检查 fencing；随后生成 generation 专属正式文件，并使用条件更新把新的 generation、location、filename 和指纹一起写入数据库。条件更新影响 0 行时，新正式文件只是未被数据库引用的 orphan，当前 attempt 标记为 `abandoned`，不得覆盖或删除数据库当前 generation 的文件。

任何产物提升、替换、隔离或删除前都必须尽可能靠近文件操作再次验证 `active_attempt_id + lease_token + artifact_generation`。本项目不为文件系统实现分布式事务；安全性依靠数据库条件更新、generation/attempt 独立文件名、临时文件不被消费以及恢复对账共同保证。

qBittorrent 后台下载期间不占用 torrent-download 控制任务槽，也不长期持有 Python 租约。记录 qBittorrent hash 并结束提交 attempt 后，Supervisor 可以继续串行提交下一个档案；因此数据库和 qBittorrent 中可以同时存在多个正在下载的种子。Supervisor 定期启动单实例、短生命周期的 torrent-download 任务，按有限批次逐条检查这些 hash 的状态。torrent-download 只负责与 qBittorrent 交互，不由 Python 实现种子内容传输，也不向 qBittorrent 施加额外的后台下载数量限制。

当前实现不自动回收或接管过期租约。管理网页只在 attempt 仍为 `running` 且租约已经过期时提供“解除过期任务”：操作者必须确认旧操作系统进程已经停止；操作会把 attempt 标记为 `abandoned`、把档案转入 `manual_review`、清空活动租约并写入审计事件。它不会终止进程、运行 cleanup、删除文件或猜测上传/删除结果。人工核对 qBittorrent、LANraragi 和文件系统后，再从详情页选择安全的恢复状态。

### 6.7 状态迁移入口

自动任务的状态变化通过 repository/状态迁移服务执行并受 attempt fencing 约束。Web 的通用人工状态调整由 Web 服务层直接校验目标状态、必填字段和 `row_version` 后更新，不在路由中运行子模块；`force_delete_pending` 与 `rename_pending` 另有只能从专用网页操作进入的限制。

状态迁移服务负责：

- 验证允许的前置状态。
- 验证 `active_attempt_id + lease_token + artifact_generation` fencing 条件，或验证 Web 操作版本。
- 更新状态、错误、重试和时间字段。
- 增加 `row_version`。
- 在同一事务中结束当前 attempt，写入 resulting_status，并清空活动租约。
- 写入事件表。

### 6.8 状态迁移矩阵

以下矩阵是 Web、Supervisor 和任务使用的业务状态机允许流转集合，表中未列出的流转默认禁止。一次性迁移脚本使用 `scripts/` 内部的 migration-only importer 写入第 9.4 节规定的初始状态并验证目标状态不变量；`legacy_import` 不作为主程序状态迁移服务、Web API 或任务入口。

| 当前状态 | 事件 | 必要前置条件 | 下一状态 | 关键处理 |
|---|---|---|---|---|
| 不存在 | 发现新档案 | manga_id、标题和链接解析成功 | `discovered` | 设置 queue_source、priority 和采集时间 |
| `discovered` | 命中观察期 | defer_until 可计算 | `deferred` | 保存重新评估时间 |
| `discovered` | judge 结果为 0 | 仅记录，不进入 screenall | `discovered` | `screen_pending=false` |
| `discovered` | judge 结果为 1 | 等待同名版本筛选 | `discovered` | `screen_pending=true` |
| `discovered` | screenall 未选中 | 同名版本比较后淘汰 | `skipped` | 保存稳定原因码和 screen_group_id |
| `discovered` | judge 命中直接队列 | 下载规则允许 | `download_pending` | torrent 首次选择前必须补齐 MangaInfo |
| `discovered` | 远端明确不可用 | 404/410/版权移除等明确证据 | `unavailable` | 保存永久原因码 |
| `discovered` | 无法安全判断 | 元数据矛盾或解析不完整 | `manual_review` | 不自动下载 |
| `deferred` | 观察期结束 | 当前时间达到 defer_until | `discovered` | 重新筛选 |
| `download_pending` | torrent 已接收 | torrent 合法且 qBittorrent 返回稳定 hash | `downloading` | method=torrent，保存 external_download_id |
| `download_pending` | direct/H@H/aria2 已开始 | ensure_details 成功、会话固定、后端可用 | `downloading` | 设置实际 download_method；临时 URL 不入库 |
| `download_pending` | MangaInfo 暂时失败 | 尚未提交 torrent | `download_pending` | 记录 details 错误并退避，详情完整后再选择种子 |
| `download_pending` | 所有下载方式均不可用 | 已有明确、不可恢复证据 | `unavailable` | 保存原因，不继续自动重试 |
| `downloading` | direct 下载主机返回 404/410 | archive 文件域名不能证明画廊已删除 | `manual_review` | 记录 archive_unavailable，保留人工判断依据 |
| `downloading` | 下载完成 | 外部状态与受控目录中的产物共同确认 | `downloaded` | 写入 location/filename/kind，递增 generation |
| `downloading` | torrent 无种或最终失败 | 回退方式仍可用 | `download_pending` | 记录回退原因，下一次 direct 前 ensure_details |
| `downloading` | 可恢复错误 | 可安全续传或重新获取 | `download_pending` | 保留安全临时文件并设置退避 |
| `downloaded` | 开始验证 | 当前 generation 产物存在且大小稳定 | `validating` | 创建 validate attempt |
| `validating` | 产物验证通过 | 结构、CRC 和大小已验证，最终 ZIP 的 SHA-1 已登记 | `upload_pending` | 保存当前 generation 校验结果和 checked_at |
| `validating` | 目录需要打包 | 目录位于受控根目录且内容合理 | `preparing` | 创建 prepare attempt |
| `validating` | 明确无效 | HTML/JSON/空文件/CRC 错误/路径越界 | `quarantined` | 隔离产物并递增 generation |
| `validating` | 无法安全判断 | 内容冲突或检查期间文件变化 | `manual_review` | 禁止上传 |
| `validating` | 可恢复失败 | 临时文件占用或读取暂时失败，当前产物未变化 | `downloaded` | 保留产物并设置退避，稍后重新验证 |
| `preparing` | 最终档案生成并验证 | 临时文件完成，原子重命名且新 SHA-1 已登记 | `upload_pending` | 递增 generation，保存最终产物校验结果 |
| `preparing` | 可恢复失败 | 原始目录仍完整存在 | `downloaded` | 清理不完整临时文件并退避 |
| `upload_pending` | 开始上传 | MangaInfo 完整，当前 generation、size 和 SHA-1 已登记 | `uploading` | 创建 upload attempt，先写 external_effect_started_at |
| `upload_pending` | MangaInfo 暂时失败 | 产物校验结果仍有效 | `upload_pending` | 只重试 ensure_details |
| `upload_pending` | 文件缺失或大小变化 | 当前文件不存在或 size 与登记值不一致 | `validating` | 清空旧 SHA-1 并重新验证 |
| `uploading` | LANraragi 明确成功 | 200、success=1、合法 archive ID，metadata 核对通过 | `uploaded` | 保存 lrr_archive_id |
| `uploading` | LANraragi 判断重复 | HTTP 409 | `manual_review` | 保存响应；必须人工判断和登记已有 archive ID |
| `uploading` | 明确未产生成功结果 | 请求体未发送，或 423 可安全退避 | `upload_pending` | 有限重试 |
| `uploading` | 结果未知或冲突 | 发送后断线、500、响应非法或远端核对仍不确定 | `manual_review` | 禁止盲目重传 |
| `uploading` | checksum 或本地文件检查失败 | HTTP 417、文件缺失或大小变化 | `validating` | 清空并重新计算当前 generation 的 SHA-1 |
| `uploading` | 文件类型不支持 | HTTP 415 且响应契约有效 | `quarantined` | 保留响应和产物证据 |
| `uploading` | 请求或业务校验被拒绝 | HTTP 400/422 且没有成功证据 | `manual_review` | 记录稳定错误码，不自动重传 |
| `uploaded` | 远端确认存在且本地清理完成 | metadata 200，所有清理目标均安全完成 | `completed` | 保留业务、attempt 和事件历史 |
| `uploaded` | 本地清理暂时失败 | 远端成功依据仍有效 | `uploaded` | 只重试未完成清理项 |
| `uploaded` | 清理前发现远端缺失 | metadata 返回 400 | `manual_review` | 保留所有本地产物和外部下载记录 |
| `uploaded/completed` | 被新版本替代 | replacement 关系唯一 | `outdated` | 停止该旧档案的其他自动任务 |
| `outdated` | 旧档案远端和本地删除完成 | 替代档案至少进入 download_pending 或后续正常流水线，DELETE success=1、metadata 返回 400，且本地遗留项已安全清理 | `deleted` | 保留业务、attempt 和删除事件历史 |
| `outdated` | 替代档案尚未就绪 | 替代档案未进入 download_pending 或后续正常流水线 | `outdated` | 暂不领取，继续等待 |
| `outdated/force_delete_pending` | 删除结果不确定 | ID 非法、423 或响应冲突 | `manual_review` | 禁止猜测删除完成 |
| `force_delete_pending` | 强制删除完成 | Web 已完成人工确认；远端与本地删除完成 | `deleted` | 跳过替代档案检查，保留完整审计 |
| `skipped` | 人工覆盖筛选 | 用户明确要求处理 | `download_pending` | 记录 actor、理由和审计事件 |
| `unavailable` | 人工确认重新尝试 | 已有新的可用证据 | `download_pending` | 清除永久错误并记录依据 |
| `quarantined` | 人工重新下载 | 用户确认重新获取 | `download_pending` | 保留隔离证据，后续创建新 generation |
| `quarantined` | 人工提供替换文件 | 文件来自受控配置目录 | `validating` | 递增 generation，不允许直接上传 |
| `manual_review` | 人工选择安全恢复点 | 目标状态要求的字段完整 | `discovered/download_pending/downloaded/completed/skipped/unavailable/quarantined/outdated/deleted` | 记录 actor、理由和证据 |
| `manual_review` | 人工确认 409 对应现有档案 | 合法 archive ID 与现有档案已人工核对 | `uploaded` | 登记 lrr_archive_id 和人工审核事件 |
| `manual_review` | 人工确认同名档案需分别保留 | 当前错误为 lrr_409 且已有安全文件名 | `rename_pending` | validate 改名真实文件、同步文件名、重新验证和计算 SHA1 |
| 可取消状态 | 用户请求取消 | 尚未处于结果未知的外部副作用中 | `cancel_requested` | 阻止领取新操作并通知活动子进程 |
| `cancel_requested` | 安全退出完成 | 无正在提交的外部副作用 | `cancelled` | 不自动删除文件、下载器任务或远端档案 |
| `cancelled` | 人工恢复 | 目标安全状态的前置条件满足 | `discovered/download_pending/validating/upload_pending/uploaded` | 根据现有产物和远端证据选择恢复点，不允许跳过验证 |

“可取消状态”固定指 `discovered`、`deferred`、`download_pending`、`downloading`、`downloaded`、`validating`、`preparing`、`upload_pending`、`uploaded`、`skipped`、`unavailable`、`quarantined` 和 `manual_review`。`uploading` 或 `outdated` 已经开始外部副作用时不进入 `cancel_requested`，必须先完成结果核对或进入 `manual_review`。

明确禁止：

- `discovered/download_pending/downloaded -> uploading/uploaded/completed`：不得跳过下载、验证和上传前置条件。
- `quarantined -> upload_pending/uploading`：隔离产物不得绕过替换和重新验证。
- `uploading -> completed`：必须先持久化经过确认的 LANraragi archive ID。
- `manual_review -> completed/deleted`：人工操作不能替代真实清理或远端删除结果。
- 任意过期 attempt 或 fencing 条件不匹配的进程修改 manga、当前产物或外部系统。

## 7. 配置、账号、Cookie 与代理

### 7.1 配置文件分类

配置固定分成四类。Web 不单独增加第五类配置文件：Web 的非敏感设置很少，放在共享 app 配置中；Web 认证密钥放在 secrets 中。Supervisor 的调度参数较多且只由 Supervisor 消费，因此单独成类。

#### app 配置

建议模板：`config/app.sample.toml`；部署时复制为被 Git 忽略的 `config/app.toml`。

保存所有进程共享的非敏感运行设置：

- 应用时区、日志级别和日志目录。
- 临时目录、torrent 下载根目录、direct 下载根目录、待上传目录、隔离目录和删除目录。
- 各受控产物位置使用独立配置键，包括 `torrent_download`、`hah_download`、`direct_download`、`aria2_download`、`prepared`、`quarantine` 和 `trash`；每个值都必须是运行机器上的绝对目录，不同机器可以使用不同盘符、UNC 路径或 Linux 挂载点。
- `qbit_torrent_path` 单独记录 qBittorrent 主机看到的种子保存根目录；它可以与本机读取完成文件的 `roots.torrent_download` 不同，完成路径只按两者根目录后的相对部分映射。
- qBittorrent、LANraragi、可选 aria2/H@H 的非敏感地址和功能开关。
- 文件大小上限、文件名规则、允许的归档和内容类型。
- Web 监听地址、端口、基础路径和非敏感展示选项。
- browse/archive session 使用哪个账号和网络配置的逻辑名称。

文件路径只存在于 app 配置中。数据库不保存绝对路径，也不保存通用相对路径；数据库只保存 `artifact_location` 配置键、`artifact_filename`、`artifact_kind` 和 `artifact_generation`。统一路径服务按 `configured_root[artifact_location] / artifact_filename` 定位文件或目录。`artifact_filename` 必须是单个安全文件名或目录名，不得包含盘符、路径分隔符、`.`、`..`、UNC 前缀或符号链接跳转。

自动生成的新产物遵守基于安全化 manga_id、artifact generation 和 attempt ID 的确定性命名规则。临时文件名额外包含 attempt ID；数据库只登记当前正式产物的 location 和文件名，不登记临时文件。人工登记的既有文件以及 lrr_409 冲突改名可以保留经过安全校验的文件名；冲突改名使用 `[数字 ID] 原文件名.zip`。旧文件迁移时先扫描各配置根目录，再将能够唯一匹配 manga_id、旧 filename、torrent hash 或 SHA1 的结果回填；无法唯一匹配的记录进入 `manual_review`，不能凭猜测删除或上传。Supervisor 重启后只依赖配置根目录、location 和文件名重新定位，不依赖旧进程内存。

#### supervisor 配置

建议模板：`config/supervisor.sample.toml`；部署时复制为被 Git 忽略的 `config/supervisor.toml`。

保存流程调度和可靠性参数：

- 列表采集周期和首次启动延迟。
- qBittorrent 检查周期。
- 各任务批次大小和单次最长运行时间。
- torrent-download 与 direct-download 控制任务固定各最多一个且允许同时运行；配置只定义单次串行批次大小和运行时限，不提供 qBittorrent 后台下载数量限制。
- qBittorrent 内部并发、队列、带宽和做种限制由 qBittorrent 自身配置管理，EH Archive 只读取状态。
- validate、prepare、upload、cleanup、outdated-delete 的并发和互斥规则。
- 租约时长、续租周期和过期恢复间隔。
- HTTP 超时、重试次数、指数退避和熔断阈值。
- 磁盘剩余空间阈值和组件暂停策略。

#### crawl 配置

建议模板：`config/crawl.sample.toml`；部署时复制为被 Git 忽略的 `config/crawl.toml`。

保存“采集什么”和“如何筛选”的业务规则：

- 需要采集的列表 URL 及其名称。
- 手工或自动采集的范围参数。
- 名称关键词、tag 关键词和筛选规则。
- 观察期长度。
- 排除分类、视频判断规则和其他内容规则。
- 标签翻译数据来源等采集相关 URL。

crawl 配置变化不改变程序运行架构，也不保存 Cookie 或代理凭据。

#### secrets 配置

建议使用环境变量或不进入版本控制的 `config/secrets.toml`；项目提供 `config/secrets.sample.toml`，其他运行配置也统一使用 `*.sample.toml` 模板。

保存：

- PostgreSQL 密码或完整连接凭据。
- qBittorrent 登录凭据。
- LANraragi Authorization。
- browse/archive 账号 Cookie。
- 代理池地址、代理账号和密码。
- SMTP 等通知凭据。
- Web 登录和 session 签名密钥。

配置加载优先级固定为：命令行临时覆盖 > 环境变量 > secrets/app/supervisor/crawl 文件 > 程序默认值。启动时输出生效配置摘要，但所有 secret 必须脱敏。

### 7.2 账号和网络模型

secrets 中的账号和网络信息在逻辑上分成三层：

1. accounts：Cookie 凭据。
2. networks：直连、固定代理或代理池。
3. sessions：将 browse/archive 角色绑定到 account 和 network。

### 7.3 单账号兼容

- 用户只配置一个账号时，browse 和 archive 角色都指向该账号。
- 两个角色可以使用相同或不同网络配置。
- 未配置代理时默认直连。

### 7.4 双账号兼容

- browse 角色使用普通账号和代理池。
- archive 角色使用有点数的账号和固定网络。
- browse 遇到 IP 限额时，将当前代理置于冷却并切换代理。
- archive 遇到账号限额时不切换 IP，暂停 direct_download 组件并报告原因。

### 7.5 Secret 处理

- Cookie、Authorization 和代理密码不写入日志。
- 默认从环境变量或受权限保护的配置文件读取。
- PostgreSQL 只保存会话名称或诊断标识，不明文保存 Cookie。
- Web 初期只显示账号配置是否可用，不返回 Cookie 内容。
- 如果未来允许 Web 修改凭据，必须使用独立主密钥加密保存；这是后续增强，不改变业务架构。

## 8. PostgreSQL 数据库设计

### 8.1 表数量原则

业务数据库保持最少表结构：

1. `manga`：基础业务数据、统一流程状态和当前运行控制。
2. `mangainfo`：详情页完整元数据，与 manga 一对一。
3. `job_attempt`：每一次阶段执行、租约 fencing、外部副作用标记和执行结果。
4. `event_log`：状态变化、当前错误的历史、系统事件和人工操作审计。
5. `system_control`：Supervisor 各组件的运行/暂停状态。
6. `system_health`：PostgreSQL、外部服务和存储根目录的最新健康快照。

Alembic 会另外创建技术表 `alembic_version`，不属于业务表。

不创建通用消息队列、Celery 结果表、GP 表或 random 表。`job_attempt` 只保存有限生命周期任务的执行记录和 fencing 信息，不是通用 jobs 队列表；当前可领取任务仍由 `manga.status`、重试字段和租约字段决定。

### 8.2 manga 表

#### 业务与来源字段

| 字段 | PostgreSQL 类型 | 说明 | 旧字段来源 |
|---|---|---|---|
| `manga_id` | `varchar(100)` PK | 档案业务主键 | `manga.manga_id` |
| `name` | `text` | 原始标题 | `manga.name` |
| `real_name` | `text` | 处理后的真实标题 | `manga.realname` |
| `link` | `text` | 档案详情 URL | `manga.link` |
| `torrent_link` | `text` | torrent 列表 URL | `manga.torrentlink` |
| `posted_at` | `timestamptz` | 统一的发布时间 | 优先由 `manga.postedtimestamp` 转换，并用 `manga.postedtime` 校验 |
| `category` | `text` | 分类 | `manga.category` |
| `tags_raw` | `text` | 列表页原始标签 | `manga.tag` |
| `pages` | `integer` | 页数 | `manga.pages` |
| `rating` | `integer` | 旧评分原值 | `manga.rating` |
| `uploader` | `text` | 上传者 | `manga.uploader` |
| `remark` | `text` | 人工备注；当前唯一由运行时识别的控制标记是大小写不敏感的 `skip video`，用于人工跳过视频种子保护 | `manga.remark` |
| `source_fetched_at` | `timestamptz` | 基础信息最近采集时间 | `manga.fetchtime`，按配置时区迁移 |
| `queue_source` | `varchar(16)` + CHECK | automatic/manual；只表示来源，不改变业务流程 | 由 state/autostate 来源推导 |

#### 流程状态字段

| 字段 | PostgreSQL 类型 | 默认值 | 说明 |
|---|---|---|---|
| `status` | `varchar(32)` + CHECK | `discovered` | 统一流程状态 |
| `screen_pending` | `boolean` | `false` | 旧 `autostate=1` 的显式替代；仅此类 `discovered` 记录进入 screenall |
| `screen_group_id` | `varchar(64)` | NULL | screenall 为同一 `real_name` 版本组生成的稳定关系标识 |
| `priority` | `integer` | `0` | 数值越大越优先 |
| `download_method` | `varchar(16)` + CHECK | NULL | torrent/direct/hah/aria2 |
| `defer_until` | `timestamptz` | NULL | 观察期结束时间 |
| `attempt_count` | `integer` | `0` | 当前操作尝试次数 |
| `next_retry_at` | `timestamptz` | NULL | 下次允许重试时间 |
| `lease_token` | `uuid` | NULL | 当前任务租约 token |
| `lease_owner` | `text` | NULL | 当前任务运行标识 |
| `lease_until` | `timestamptz` | NULL | 当前租约失效时间 |
| `active_attempt_id` | `bigint` | NULL | 当前持有 fencing 权限的 job_attempt；与 manga_id 组成复合外键 |
| `last_error_operation` | `varchar(24)` | NULL | collect/torrent_download/direct_download/validate/prepare/upload/cleanup/delete |
| `last_error_code` | `text` | NULL | 当前未解决错误码 |
| `last_error_detail` | `text` | NULL | 当前未解决错误详情 |
| `last_error_at` | `timestamptz` | NULL | 当前错误时间 |
| `superseded_by_id` | `varchar(100)` FK | NULL | 替代该档案的新档案 ID |
| `row_version` | `bigint` | `0` | Web/Supervisor 乐观并发控制 |

#### 下载、产物和远端标识

| 字段 | PostgreSQL 类型 | 说明 | 旧字段来源 |
|---|---|---|---|
| `external_download_id` | `text` | qBittorrent hash、aria2 GID 或 H@H 标识 | torrent 时来自 `manga.torrenthash` |
| `artifact_location` | `varchar(32)` + CHECK | app 配置中的受控根目录键，不是路径 | 新字段 |
| `artifact_filename` | `text` | 当前 generation 的正式产物文件名；自动生成的新产物包含安全化 manga_id 和 generation，人工登记/冲突改名文件使用安全校验后的名称 | `manga.filename` |
| `rename_target_filename` | `text` | 同名冲突人工确认后，由 validate 使用的待改文件名 | 新字段 |
| `artifact_kind` | `varchar(16)` + CHECK | file/directory/zip | 新字段 |
| `artifact_generation` | `integer` nullable | 产物代次；NULL 表示尚无产物，首个产物为 1，重新下载、打包、隔离或替换时递增 | 新字段 |
| `artifact_size` | `bigint` | 验证时登记的字节数 | 新字段 |
| `artifact_sha1` | `char(40)` | LANraragi `file_checksum` 使用的本地产物 SHA1；不要求等于 archive ID | 新字段 |
| `artifact_checked_at` | `timestamptz` | 当前 generation 最近一次验证和 SHA-1 登记时间 | 新字段 |
| `lrr_archive_id` | `varchar(63)` | LANraragi archive ID | `manga.arcid` |

数据库不保存产物绝对路径、通用相对路径或 attempt 临时文件名。统一路径服务使用 `artifact_location` 对应的 app 配置根目录加 `artifact_filename` 定位当前文件或目录；`artifact_kind` 决定验证和准备方式，`artifact_generation` 与 generation 专属文件名共同防止过期 attempt 覆盖新产物。LANraragi 是永久存储位置。

#### 时间字段

| 字段 | PostgreSQL 类型 | 说明 |
|---|---|---|
| `status_updated_at` | `timestamptz` | 最近状态变化时间 |
| `created_at` | `timestamptz` | 新系统记录创建时间；迁移记录注明迁移来源 |
| `updated_at` | `timestamptz` | 任意字段最近更新时间 |

### 8.3 mangainfo 表

`mangainfo` 与 `manga` 一对一，主键同时是外键。数据库不物理删除 manga，因此业务上不使用级联删除清除历史详情。

| 字段 | PostgreSQL 类型 | 说明 | 旧字段来源 |
|---|---|---|---|
| `manga_id` | `varchar(100)` PK/FK | 档案 ID | `mangainfo.manga_id` |
| `name` | `text` | 详情页标题 | `mangainfo.name` |
| `roman_name` | `text` | 罗马字标题 | `mangainfo.romaname` |
| `real_name` | `text` | 处理后的标题 | `mangainfo.realname` |
| `link` | `text` | 详情页 URL | `mangainfo.link` |
| `category` | `text` | 分类 | `mangainfo.category` |
| `uploader` | `text` | 上传者 | `mangainfo.uploader` |
| `posted_at` | `timestamptz` | 详情页发布时间 | 由 `mangainfo.postedtime` 转换 |
| `language` | `text` | 语言 | `mangainfo.language` |
| `estimated_size_raw` | `text` | 页面预计大小原文 | `mangainfo.estimatedsize` |
| `pages` | `integer` | 页数 | `mangainfo.pages` |
| `favorited` | `integer` | 收藏数 | `mangainfo.favorited` |
| `rating_count` | `integer` | 评分人数 | `mangainfo.ratingcount` |
| `rating` | `integer` | 旧评分原值 | `mangainfo.rating` |
| `fetched_at` | `timestamptz` | MangaInfo 获取时间 | 由 `mangainfo.fetchtime` 转换 |
| `tags_raw` | `text` | 原始标签 | `mangainfo.tag` |
| `tags_translated_raw` | `text` | 翻译标签 | `mangainfo.tagtran` |
| `created_at` | `timestamptz` | 新系统行创建时间 | 新字段 |
| `updated_at` | `timestamptz` | 最近更新时间 | 新字段 |

`mangainfo` 只保存详情业务信息，不保存流程状态、下载方式、错误、租约或其他控制字段。旧 `mangainfo.filename/state/remark/downmethod` 不迁入新表。`postedtime` 和 `fetchtime` 必须在迁移报告中完成可逆核对；无法转换的记录阻止正式切换，不能静默写成 NULL。

### 8.4 job_attempt 表

`job_attempt` 一行表示一次有限生命周期的阶段执行，不替代 `manga` 当前状态，也不作为任务来源。

| 字段 | PostgreSQL 类型 | 说明 |
|---|---|---|
| `id` | `bigserial` PK | attempt ID，同时作为 fencing token 的组成部分 |
| `manga_id` | `varchar(100)` nullable FK ON DELETE RESTRICT | 档案 ID；不属于单个档案的系统 attempt 可为空 |
| `operation` | `varchar(24)` | collect/details/torrent_download/direct_download/validate/prepare/upload/cleanup/delete/reconcile |
| `attempt_no` | `integer` | 同档案、同 operation 的递增尝试序号 |
| `status` | `varchar(16)` + CHECK | running/succeeded/failed/abandoned |
| `trigger_source` | `varchar(16)` | supervisor/web/migration/reconcile/system |
| `actor` | `text` | Supervisor run、任务子进程或经过认证的 Web 用户 |
| `previous_status` | `varchar(32)` nullable | 领取时 manga.status |
| `resulting_status` | `varchar(32)` nullable | attempt 结束时提交的状态 |
| `lease_token` | `uuid` | 与 manga 当前活动租约共同用于 fencing |
| `artifact_generation` | `integer` nullable | 领取时观察到的产物代次；collect/details 等不涉及文件的 attempt 可为空 |
| `external_task_id` | `text` nullable | torrent hash、aria2 GID、H@H 标识或 LRR archive ID；不得保存一次性下载 URL |
| `external_effect_started_at` | `timestamptz` nullable | 可能产生外部副作用的调用开始前持久化 |
| `progress_bytes` | `bigint` nullable | direct 下载已完成字节数 |
| `progress_total_bytes` | `bigint` nullable | direct 下载总字节数；服务器未提供时为空 |
| `progress_speed_bps` | `double precision` nullable | direct 下载瞬时速度 |
| `progress_updated_at` | `timestamptz` nullable | 进度最近更新时间 |
| `started_at` | `timestamptz` | 开始时间 |
| `finished_at` | `timestamptz` nullable | 结束时间 |
| `error_code` | `text` nullable | 稳定错误码 |
| `detail` | `jsonb` | 脱敏诊断、配置哈希和恢复依据 |

约束要求：

- 唯一约束 `(manga_id, operation, attempt_no)`；档案行锁内生成下一序号。
- `job_attempt` 额外提供唯一键 `(manga_id, id)`；`manga(manga_id, active_attempt_id)` 以复合外键引用它，确保 active attempt 必须属于同一个 manga。该反向外键在两个表创建完成后由 Alembic 添加。
- `job_attempt.manga_id -> manga.manga_id` 和活动 attempt 复合外键均使用 `ON DELETE RESTRICT`；业务 manga 不物理删除，当前或历史 attempt 不因误删级联消失。
- running attempt 的 `finished_at/resulting_status` 为空；终态 attempt 必须有 `finished_at`。
- `manga.active_attempt_id` 指向唯一有权写回的 attempt。状态提交使用 `manga_id + active_attempt_id + lease_token + artifact_generation` 条件。
- 条件更新影响 0 行时只能将当前 attempt 标记为 `abandoned`，不能再次修改 manga 或文件。
- 上传 attempt 的 `external_effect_started_at` 已存在但没有明确成功响应时，不允许自动重传；当前实现将其交给 `manual_review`，由人工核对 LANraragi。

### 8.5 event_log 表

| 字段 | PostgreSQL 类型 | 说明 |
|---|---|---|
| `id` | `bigserial` PK | 事件 ID |
| `manga_id` | `varchar(100)` nullable FK | 档案事件关联 ID；系统事件可为空 |
| `attempt_id` | `bigint` nullable FK | 相关 job_attempt；纯人工或系统事件可为空 |
| `component` | `varchar(32)` | supervisor/collect/torrent_download/direct_download/validate/prepare/upload/cleanup/delete/web/migration |
| `event_type` | `varchar(32)` | status_changed/error/retry/lease/system/manual 等 |
| `operation` | `varchar(24)` nullable | 当前业务操作 |
| `from_status` | `varchar(32)` nullable | 旧状态 |
| `to_status` | `varchar(32)` nullable | 新状态 |
| `error_code` | `text` nullable | 错误码 |
| `detail` | `jsonb` | 结构化详情，严禁 Cookie 和密码 |
| `actor` | `text` | supervisor run、子进程、Web 用户或 migration |
| `created_at` | `timestamptz` | 事件时间 |

事件表追加写，不用它替代主表当前状态。保留策略可以按时间归档，但不能影响 manga/mangainfo 数据。

### 8.6 system_control 表

| 字段 | PostgreSQL 类型 | 说明 |
|---|---|---|
| `component` | `varchar(32)` PK | supervisor/collect/details/torrent_download/direct_download/validate/prepare/upload/cleanup/delete |
| `state` | `varchar(16)` | running/paused/draining |
| `reason` | `text` nullable | 暂停原因 |
| `updated_by` | `text` | supervisor 或 Web 用户 |
| `lease_owner` | `text` nullable | Supervisor/周期任务单实例租约持有者 |
| `lease_until` | `timestamptz` nullable | 单实例租约过期时间 |
| `heartbeat_at` | `timestamptz` nullable | Supervisor 或组件最近心跳 |
| `row_version` | `bigint` | 并发控制 |
| `updated_at` | `timestamptz` | 最近更新时间 |

`supervisor=paused` 阻止所有新任务；`supervisor=draining` 停止领取并等待在途任务结束后转为暂停。组件暂停只阻止相应任务，不中断无关任务。`supervisor` scope 使用数据库租约保证同一部署只有一个调度者，第二个 Supervisor 在已有未过期租约时拒绝启动；collect/reconcile 等周期任务也使用独立 scope 防止重复 tick。

### 8.7 system_health 表

| 字段 | PostgreSQL 类型 | 说明 |
|---|---|---|
| `component` | `varchar(64)` PK | 健康检查目标，例如 PostgreSQL、qBittorrent、LANraragi 或存储根目录 |
| `status` | `varchar(16)` | healthy/degraded/unavailable/unknown |
| `checked_at` | `timestamptz` | 本次检查时间 |
| `latency_ms` | `integer` nullable | 检查耗时 |
| `error_code` | `text` nullable | 稳定错误码 |
| `message` | `text` | 脱敏摘要 |
| `detail` | `jsonb` | 脱敏结构化详情 |
| `updated_at` | `timestamptz` | 最近更新时间 |

### 8.8 索引与约束

当前 schema 建立：

- `ix_manga_queue(status, priority, next_retry_at, created_at)`：Supervisor 候选任务查询。
- `ix_manga_lease_until(lease_until)`：租约查询。
- `ix_manga_lrr_archive_id`、`ix_manga_external_download_id` 和 `ix_manga_screen_pending`：远端 ID、下载器 ID 和筛选队列查询。
- `ix_manga_web_posted(posted_at DESC NULLS LAST, manga_id)`：档案队列按发布时间从新到旧游标分页。
- `ix_manga_web_review(status, status_updated_at DESC, manga_id)`：`manual_review`/`quarantined` 局部复核队列。
- `ix_manga_web_error`、`ix_manga_web_retry`：错误和重试页面查询。
- `job_attempt` 的档案时间线和 operation/status 索引。
- `event_log` 的档案、组件、run ID 和 Web 时间线索引。
- `system_health(checked_at)`：健康快照新鲜度查询。
- PostgreSQL `pg_trgm` 为 `name`、`real_name` 和 `artifact_filename` 建立 GIN 索引；migration 会执行 `CREATE EXTENSION IF NOT EXISTS pg_trgm`。

约束要求：

- status、download_method、system state 使用 CHECK 约束；不采用难以修改的 PostgreSQL 原生 ENUM。
- `queue_source` 只能是 automatic/manual；`artifact_location` 只能引用 app 配置允许的键，`artifact_kind` 只能是 file/directory/zip。
- `attempt_count >= 0`。
- `artifact_generation` 为空或大于等于 1；存在 artifact location/filename/kind 时 generation 必须存在。
- 租约 token、owner、until 必须一致为空或一起有效。
- 活动租约存在时 `active_attempt_id` 必须存在，并由复合外键保证它属于当前 manga；涉及文件的执行中状态必须记录当前 `artifact_generation`。
- `artifact_filename` 不得包含路径分隔符、盘符、`.`、`..` 或 NUL；新生成的正式文件名必须包含当前 generation。最终解析路径必须仍位于配置根目录且不能穿过符号链接或 Windows reparse point。
- `upload_pending/uploading` 必须存在 MangaInfo、artifact location、filename、kind、size、SHA-1 和 checked_at。
- 新写入的 `lrr_archive_id` 必须是 40 位十六进制 SHA1；旧迁移值不符合时进入 `manual_review`，不得用于自动删除。
- `uploaded/completed` 应有 `lrr_archive_id`，例外只能通过迁移或人工审查事件说明。
- `outdated/deleted` 原则上应有 `superseded_by_id`。
- 状态迁移的完整业务规则由应用服务保证，数据库约束负责防止明显不可能的数据。

## 9. MySQL 到 PostgreSQL 迁移

### 9.1 迁移原则

- 数据迁移是正式切换前的一次性离线工作，全部实现放在项目根目录 `scripts/`，不放入 `src/eh_archive` 的 domain、service、task、Supervisor 或 Web 运行路径。
- 主程序不导入迁移脚本，不包含 MySQL repository、旧 state/autostate 映射或迁移命令入口；MySQL 驱动只属于可选的 migration 依赖组，正式运行环境无需安装。
- 迁移脚本可以复用主程序稳定的 PostgreSQL model、配置加载、路径安全和 LANraragi/qBittorrent client，但依赖方向只能是 `scripts -> eh_archive`，禁止主程序反向 import `scripts`。
- `scripts/` 同时作为以后一次性迁移、人工校验、诊断和维护脚本的统一目录。每个脚本必须有明确 CLI、用途说明、参数校验和退出码；危险操作默认 dry-run 或要求显式 `--apply`，不得成为 Supervisor 自动调度的一部分。
- 最终迁移窗口内旧 MySQL 停止写入；切换后继续作为只读历史库保留，不由新程序写入。
- 先备份，再迁移，再校验，最后切换新程序。
- 迁移工具可重复运行到空的新 PostgreSQL 数据库。
- 迁移失败时回滚 PostgreSQL 当前事务，不修改 MySQL。
- 字符串按 UTF-8/utf8mb4 无损处理。
- 任何不能确定的控制状态按保守策略进入 `manual_review`，不能丢弃记录或假定完成。

### 9.2 表处理

- `manga`：全部行迁移。
- `mangainfo`：全部行迁移。
- `random`：不进入新业务库；历史数据继续保留在旧 MySQL。
- `GP`：不进入新业务库；历史数据继续保留在旧 MySQL。

### 9.3 旧字段处理

- 业务字段映射到新列。
- `state/autostate` 用于推导初始新状态、`queue_source` 和迁移事件；原始组合写入迁移报告和 `event_log.detail`，不作为新程序运行字段。
- `relatetation` 不迁移，新程序不再使用该关系。
- `manga.remark` 原样迁移到新 `remark`，作为可由 Web 编辑的人工备注。旧的状态控制文本不再解释；当前代码只额外识别显式的 `skip video` 标记，用于允许人工确认后继续处理视频种子，它不直接修改档案状态。
- `postedtimestamp` 转换为统一的 `posted_at`，并使用 `postedtime` 交叉校验；两者冲突时写入迁移报告，由旧 MySQL 保留原值。
- `alias` 不作为新业务字段迁移，但迁移器必须用它和 manga_id 扫描配置中的 H@H 根目录；唯一匹配后回填 `artifact_location/artifact_filename/artifact_kind`，无法匹配则进入人工清单。
- `torrenthash` 映射到 `external_download_id`。
- `arcid` 映射到 `lrr_archive_id`。
- `filename` 映射到 `artifact_filename`；实际文件所在目录通过旧下载方式、alias、qBittorrent 状态和各配置根目录对账后写入 `artifact_location`，不能只凭扩展名猜测。
- mangainfo 的 `filename/state/remark/downmethod` 属于旧控制信息，不迁入新 mangainfo。
- mangainfo 的 `postedtime/fetchtime` 转换为 `posted_at/fetched_at`；转换失败必须在正式切换前解决。

### 9.4 状态迁移策略

迁移不是简单整数替换，需要结合旧状态、文件是否存在、qBittorrent 状态和 LANraragi 查询结果进行一次 reconciliation。

#### 9.4.1 状态来源优先级

1. `state=-1` 且 `remark='deleted'`：映射为 `deleted`；其他 `state=-1` 映射为 `outdated`。该规则优先于 arcid，并固定设置 `queue_source=automatic`。
2. `state=0`：按旧程序语义映射为 `completed`，固定设置 `queue_source=automatic` 并原样迁移 arcid；arcid 缺失时写入 `legacy_completed_without_lrr_id`，禁止因此执行新的自动清理。
3. 其他记录中 `autostate` 非空：使用 autostate 表，`queue_source=automatic`。
4. 否则使用 state 表，`queue_source=manual`；旧 `13/14/15` 额外设置 `priority=100`。
5. 非 `state=0/-1` 却存在 arcid、state/autostate 结论冲突、文件与外部状态冲突或旧值无明确含义时，进入 `manual_review`。

#### 9.4.2 autostate 映射矩阵

| 旧值 | 新状态 | download_method | 迁移处理 |
|---|---|---|---|
| `-1` | `deferred` | NULL | 保留观察期信息；无法计算 defer_until 时进入 discovered 重新筛选 |
| `1` | `discovered` | NULL | 等待统一筛选 |
| `2` | `download_pending` | NULL/torrent | torrent 可以在 MangaInfo 缺失时继续 |
| `3` | `skipped` | NULL | 保存旧筛选原因 |
| `4` | `downloading` | torrent | 按 torrent hash 与 qBittorrent 对账 |
| `5` | `downloaded` | torrent | 定位产物后重新验证；MangaInfo 缺失不重下 torrent |
| `6` | `download_pending` | direct | 旧无种/fallback；direct 开始前执行 ensure_details |
| `7` | `downloading` | hah | 对账 H@H 外部任务和配置目录 |
| `8` | `validating` | torrent | 没有新校验结果，强制重新验证 |
| `9` | `downloaded` | hah | 定位 H@H 目录或文件后重新验证 |
| `10` | `validating` | hah | 旧压缩产物必须重新登记 SHA-1 |
| `11` | `validating` | direct/aria2 | 防止文本错误页进入上传 |
| `12` | `manual_review` | 原值 | 文件名或版本冲突 |
| `-2` | `manual_review` | 原值 | 历史语义不唯一，不猜测 |
| `-3` | `download_pending` | 原值 | 旧下载错误，写入 legacy_download_error 和退避时间 |
| `-4` | `preparing` | 原值 | 有源产物时重做准备；缺失时人工审核 |
| `-5` | `manual_review` | 原值 | 旧上传结果无法证明，禁止自动重传 |
| `-6` | `manual_review` | 原值 | 视频或特殊产物 |

#### 9.4.3 state 映射矩阵

| 旧值 | 新状态 | download_method | 迁移处理 |
|---|---|---|---|
| `1` | `discovered` | NULL | 手工来源，重新筛选或人工确认 |
| `2` | `download_pending` | NULL/torrent | 普通手工任务 |
| `3/4` | `skipped` | NULL | 原因写入迁移事件 |
| `5/14` | `downloading` | torrent | 14 设置 priority=100；按 torrent hash 对账 |
| `6/15` | `download_pending` | direct | fallback；15 设置 priority=100，direct 前 ensure_details |
| `7` | `downloaded` | torrent | 定位后重新验证 |
| `8` | `validating` | torrent | 强制登记当前 generation 的 SHA-1 |
| `9` | `downloading` | hah | 对账 H@H 任务和目录 |
| `10` | `downloaded` | hah | 定位后重新验证/准备 |
| `11/12` | `validating` | direct/hah | 旧直连或压缩产物重新验证 |
| `13` | `download_pending` | NULL/torrent | priority=100 |
| `-2` | `manual_review` | 原值 | 历史语义不唯一，不猜测 |
| `-3` | `download_pending` | 原值 | 旧下载错误，写入 legacy_download_error |
| `-4` | `preparing` | 原值 | 有源产物才允许重试 |
| `-5` | `manual_review` | 原值 | 旧上传结果未知，禁止自动重传 |
| `-6/-7/-8` | `manual_review` | 原值 | 视频或特殊产物 |
| `-1` + `remark='deleted'` | `deleted` | 原值 | 不重新执行远端删除 |
| `-1` 其他 | `outdated` | 原值 | 保留 arcid，按新删除前置条件处理 |
| `0` | `completed` | 原值 | 旧完整完成语义；遗留文件只报告，不自动清理 |

#### 9.4.4 产物和外部系统对账

- 根据 download_method 选择配置根目录集合，用 manga_id、旧 filename、alias、torrent hash 和文件类型扫描候选项。
- 只有唯一候选才能回填 `artifact_location + artifact_filename + artifact_kind`，随后验证 size、计算最终 ZIP 的 SHA-1，并设置初始 `artifact_generation=1`。
- 没有候选时按状态决定重新下载或进入 `manual_review`；多个候选、文件名越界或候选位于未配置目录时一律人工审核。
- 迁移脚本不移动、重命名或删除文件。需要整理到新根目录时由后续受 fencing 保护的 prepare/reconcile attempt 执行。
- 已有 lrr_archive_id 时使用 `GET /api/archives/{id}/metadata` 查询；200 表示存在，400 表示不存在。新程序不能按标题或文件名模糊匹配远端档案。

#### 9.4.5 通用原则

- 旧 `special 13/14/15` 拆成正常状态和高 priority。
- 已有并确认存在的 LANraragi ID：进入 `uploaded`，完成本地清理核对后进入 `completed`。
- 旧成功状态但 LANraragi 无法确认：进入 `manual_review`，不得直接删除本地文件。
- 旧直接下载完成状态：进入 `downloaded`，强制重新验证产物，避免旧 aria2 HTML 文件进入上传。
- 旧 torrent/H@H 下载完成状态：根据文件或目录存在情况进入 `downloaded`、`preparing` 或 `manual_review`。
- 旧上传错误统一进入 `manual_review`；有效产物保留并登记 SHA-1，只有人工核对 LANraragi 后，才允许恢复到 `upload_pending` 或登记已有 archive ID。
- 旧压缩错误：有源目录则进入 `preparing`；无源数据则进入 `manual_review`。
- 旧无种状态：进入 `download_pending`，由新下载选择逻辑处理。
- 明确远端不可用：进入 `unavailable`。
- 明确过时：进入 `outdated` 或 `deleted`，取决于 LANraragi 和本地清理核对结果。
- 无法确定含义的旧记录：进入 `manual_review`，事件中记录旧库 manga_id 和迁移原因；具体旧值继续从只读 MySQL 查询。

### 9.5 迁移校验

必须自动生成迁移报告，至少包含：

- MySQL/PostgreSQL manga 总行数。
- MySQL/PostgreSQL mangainfo 总行数。
- 主键集合差异。
- 每个业务字段的 NULL 数量对比。
- 文本字段规范化后的字段级摘要或哈希对比。
- 超长 Unicode、换行、引号和特殊字符抽样核对。
- mangainfo 孤立记录检查。
- lrr_archive_id 和 external_download_id 重复检查。
- 时间转换失败列表。
- 各新状态数量和映射来源统计。
- 每一种 state/autostate 值和冲突组合的映射数量；每条记录都有 queue_source、映射规则 ID 和旧值审计。
- 每个已回填 artifact 的 location 配置键、文件名、kind、generation、size 和 SHA-1 核对结果。
- 旧 arcid 使用 LANraragi metadata API 的存在性核对结果，以及不符合 40 位 SHA1 规则的人工清单。
- `manual_review` 迁移清单。

如果旧 mangainfo 存在没有对应 manga 的孤立行，为保证业务信息不丢失，迁移器创建最小占位 manga，状态设为 `manual_review`，并在 `event_log` 记录旧库主键和原因。

只有迁移报告通过并完成人工抽样后，才允许新 Supervisor 对迁移数据执行清理任务。

## 10. 项目结构规划

建议采用单一 Python 包，Web、Supervisor 和任务入口由同一包提供：

```text
src/
  eh_archive/
    config/
    db/
    domain/
    repositories/
    services/
      collector/
      downloader/
        torrent/
        direct/
      validator/
      preparer/
      uploader/
      cleanup/
    tasks/
    supervisor/
    web/
    integrations/
      qbittorrent/
      lanraragi/
      aria2/
      hah/
    logging/
config/
  app.sample.toml
  supervisor.sample.toml
  crawl.sample.toml
  secrets.sample.toml
  # local ignored files copied from the samples
  app.toml
  supervisor.toml
  crawl.toml
  secrets.toml
migrations/
tests/
scripts/
  README.md
  migrate_mysql_to_postgresql.py
  verify_migration.py
  reconcile_migration.py
pyproject.toml
dependency lock file
```

职责约束：

- `domain`：新系统状态、错误类型和业务对象，不包含旧 MySQL 字段或 state/autostate 映射，不访问网络。
- `repositories`：PostgreSQL 查询、租约和事务。
- `services/downloader/torrent`：种子选择、qBittorrent 推送和状态检查，不实现 BitTorrent 数据传输。
- `services/downloader/direct`：Python direct 和可选 aria2/H@H adapter 的统一入口。
- 其他 `services`：可测试的采集、验证、准备、上传和清理逻辑。
- `integrations`：第三方 API 适配，不修改业务状态。
- `tasks`：领取记录、调用 service、提交状态迁移并退出。
- `supervisor`：定时和按需调度，不包含页面解析或上传实现。
- `web`：查询和控制，不包含实际任务执行。
- `migrations`：只保存 PostgreSQL schema 的 Alembic revision，不保存旧 MySQL 数据迁移逻辑。
- `scripts`：一次性迁移、校验、诊断和人工维护入口；可以调用 `eh_archive` 的稳定公共接口，但绝不被 `eh_archive`、Web、Supervisor 或任务反向导入。新增独立脚本统一放在此目录，并在 `scripts/README.md` 登记用途、读写范围和运行示例。

旧程序只作为行为参考，不直接在新入口中 import 并运行整份旧脚本。解析和命名等仍需长期运行的已验证逻辑迁移为独立 service，并用旧样本建立回归测试；旧数据库字段转换、state/autostate 映射和一次性对账逻辑只存在于 `scripts/`。

## 11. 跨平台要求

### 11.1 Python 与依赖

- 使用 `pyproject.toml` 描述项目和命令入口。
- 提交依赖锁定文件，固定经过验证的版本。
- `.venv` 不提交、不迁移、不发布。
- Windows 和 Linux 各自在目标机器重新创建 venv。
- Supervisor 使用当前解释器路径启动子进程，不使用写死的 `python.exe`。
- 子进程参数使用参数数组，不按空格拆命令字符串。

### 11.2 文件路径

- 统一使用 `pathlib`。
- 数据库不保存临时文件路径；所有根目录由 app 配置提供，实际路径由 manga_id、download_method 和 artifact_filename 推导。
- Windows 网络存储使用 UNC 路径，不依赖登录用户的映射盘符。
- Linux 网络存储使用系统挂载点，例如 `/mnt/...`。
- 配置层提供存储根目录，业务代码不出现 `V:`、反斜杠或固定 Linux 路径。
- 原子重命名只用于同一文件系统内的临时名到正式名。跨文件系统时先普通复制到目标根目录中的 attempt 临时名，验证后在目标文件系统内重命名；不实现跨盘事务、断点复制或双向回滚。
- 对 Linux 大小写敏感和 Windows 文件名限制都进行测试。

### 11.3 进程与服务

- Windows 可使用任务计划程序、WinSW 或 NSSM 分别托管 Web 和 Supervisor。
- Linux 使用两个 systemd service。
- 两个服务使用同一个项目版本和 venv，但独立启动和重启。
- 服务停止时 Supervisor 不再启动新任务，并给子进程合理的退出期限。
- 进程被强制终止后依靠 PostgreSQL 租约阻止旧进程写回；系统不自动回收，需人工核对并解除过期任务。

### 11.4 发布与迁移

- 发布源码包或 Python wheel，不发布 venv。
- 迁移机器时迁移源码/发布包、配置、secret、PostgreSQL 数据和存储目录。
- 新机器重新创建 venv 并安装锁定依赖。
- 跨平台迁移前暂停任务，避免数据库中的绝对路径和正在写入的文件。

## 12. 日志、监控与安全

### 12.1 结构化日志

每条任务日志至少包含：

- manga_id
- operation
- status
- run ID
- lease token 的短标识
- attempt_count
- duration
- error_code

日志按运行会话和模块写入独立目录；当前仓库不内置按日期/大小轮转，生产环境需由操作系统或日志采集工具配置轮转。进度显示不能污染错误日志和邮件内容。

### 12.2 健康检查

Web 提供只读健康状态：

- PostgreSQL 连通性和迁移版本。
- Supervisor 最近心跳。
- 各组件 paused/running。
- qBittorrent API 状态。
- LANraragi API 状态。
- 存储目录可读写状态和剩余空间。
- 各状态数量、重试数量和 manual_review 数量。

### 12.3 Web 安全

- 默认只监听本机地址。
- 对局域网开放时必须启用认证和防火墙限制。
- 修改状态、删除、恢复、暂停等操作记录 actor 和事件。
- Web 不展示 Cookie、Authorization、数据库密码或代理密码。
- 不允许通过 Web 输入任意文件路径或任意系统命令。

## 13. 测试策略

### 13.1 单元测试

- 旧页面解析样本与新解析服务输出一致。
- 第 6.8 节每一条允许和禁止迁移，以及对应的字段前置条件。
- priority 排序。
- 错误分类和退避时间。
- active_attempt_id、lease token、artifact generation fencing、复合外键和过期写回拒绝。
- generation/attempt 确定性文件命名、名称冲突检测和跨平台路径。
- Cookie/session 角色选择。

### 13.2 下载与验证测试

- 正常 ZIP。
- 截断 ZIP。
- CRC 错误。
- 空 ZIP。
- 14KB HTML 错误页面。
- Content-Type 欺骗。
- 缺少 Content-Length。
- 连接中断和 Range 续传。
- 下载入口过期。
- aria2 报完成但内容无效。
- 同盘临时文件到正式文件的原子重命名。
- 跨盘复制中断只留下 attempt 临时文件，数据库仍引用旧 generation；恢复后可安全重做。

### 13.3 上传测试

- 小文件正常上传。
- 大文件流式上传。
- 连接前超时。
- 服务端接收后响应丢失。
- 413、429、500、502、503、504。
- 返回非 JSON 或缺少 ID。
- LANraragi 已存在重复档案。
- LANraragi 返回 409 时必须进入 manual_review，未经人工核对不得自动登记 archive ID 或进入 uploaded。
- 上传成功但远端验证失败。
- 进程在上传和数据库提交之间崩溃。

### 13.4 清理测试

- 文件已不存在。
- qBittorrent 任务已不存在。
- 一半清理成功后进程崩溃。
- 非预期文件名和额外目录。
- 新旧档案删除顺序。
- 未确认 uploaded 时禁止清理。
- uploaded 清理前 metadata 返回 400 时保留本地产物和下载器记录，并进入 manual_review。

### 13.5 集成与恢复测试

- PostgreSQL 临时不可用。
- torrent-download 同时只有一个提交操作；连续提交 10 个种子后，10 个 qBittorrent 后台任务可以同时存在且互不占用 EH Archive 控制任务槽。
- 单个 torrent-download 子进程按有限批次串行提交和轮询，qBittorrent 的内部排队或并行设置不被 EH Archive 改写。
- Supervisor 重启。
- Web 重启不影响任务。
- 任务进程强制终止和租约恢复。
- 旧 attempt 在新 attempt 接管或 generation 更新后恢复运行，其数据库写回和新产物覆盖均被拒绝。
- Windows/Linux 路径配置。
- MySQL 迁移的重复演练和校验报告。
- 导入边界测试：`src/eh_archive` 不得 import `scripts`、MySQL driver 或旧 state/autostate 映射；未安装 migration 可选依赖时 Web、Supervisor 和任务仍可完整启动。

## 14. 分阶段实施计划

### 阶段 1：项目基础与不可变架构

目标：建立后续不需要推翻的工程基础。

工作内容：

- 建立 `src` 项目结构、pyproject、锁定依赖和测试框架。
- 建立跨平台配置模型、browse/archive session 模型和 secret 加载。
- 固化 app、supervisor、crawl、secrets 四类配置及其覆盖优先级。
- 建立结构化日志、统一错误类型和状态定义。
- 建立 PostgreSQL 开发/测试环境和 Alembic。
- 创建 manga、mangainfo、job_attempt、event_log、system_control、system_health 表，并添加 active attempt 的同 manga 复合外键。
- 实现状态迁移服务、row_version、租约、artifact generation 和 attempt fencing 基础设施。
- 实现 generation/attempt 确定性命名和受控路径服务。
- 固化 Windows/Linux 路径和子进程规则。

验收：

- 新项目可在 Windows 和 Linux venv 中安装。
- 数据库迁移可从空 PostgreSQL 创建完整结构。
- 状态迁移、复合外键、attempt fencing、租约和过期写回拒绝测试通过。
- 正式运行时不使用 SQLite、Redis、Celery 或硬编码平台路径；测试可使用内存 SQLite 做轻量数据层验证，生产数据库固定为 PostgreSQL。

### 阶段 2：MySQL 数据迁移工具

目标：使用与主运行逻辑隔离的一次性脚本，在新功能上线前证明所有旧业务数据可以安全迁移。

工作内容：

- 在 `scripts/migrate_mysql_to_postgresql.py` 实现只读 MySQL 到 PostgreSQL 迁移器。
- 在 `scripts/` 内完成旧字段映射、时间转换、废弃字段排除、旧状态初始映射和 special priority 转换；这些规则不进入主程序 domain/service。
- 在 `scripts/reconcile_migration.py` 实现迁移后 LANraragi/qBittorrent/文件系统 reconciliation 的只读报告。
- 在 `scripts/verify_migration.py` 生成行数、主键、NULL、哈希、Unicode 和状态统计报告。
- 在 `scripts/README.md` 记录三个脚本的依赖、参数、dry-run/apply 语义、执行顺序、读写范围和退出码。
- 将 MySQL driver 放入独立 migration 可选依赖组，验证核心运行依赖不包含 MySQL client。
- 建立脱敏迁移测试样本。

验收：

- manga/mangainfo 行和业务字段无缺失。
- 所有无法确定的记录都进入报告或 manual_review，不静默丢弃。
- 迁移器可对新空库重复执行并得到一致结果。
- GP/random 不进入业务结构，旧 MySQL 保持只读并具有完整备份。
- 删除或不安装 `scripts/` 和 migration 可选依赖后，Web、Supervisor、任务和 PostgreSQL schema migration 仍可正常运行。

### 阶段 3：采集与 MangaInfo 统一

目标：迁移旧采集和解析行为，并统一详情获取时机。

工作内容：

- 将旧列表采集、筛选、命名和标签解析迁入独立 service。
- 建立旧 HTML 样本回归测试。
- 实现 discovered/deferred/skipped/unavailable 流转。
- 使用 browse_session 获取并 upsert MangaInfo。
- 筛选通过后先进入 download_pending，并使用 browse_session 幂等 upsert MangaInfo；获取失败记录 details 错误和退避，首次 torrent 选择前必须成功补齐详情。
- direct/H@H/aria2 开始前以及上传前分别执行 ensure_details 前置检查。
- 支持 Web/命令入口手工加入 URL 和 priority。

验收：

- 同一输入样本的新旧核心解析结果一致。
- 重复采集幂等，不覆盖人工状态或制造重复记录。
- MangaInfo 暂时不可用时 torrent、direct/H@H/aria2 与上传都不会越过各自的详情前置检查。
- browse 单账号和双账号配置均可运行。

### 阶段 4：torrent 与 direct 下载任务

目标：建立 torrent 优先、直接下载回退的统一下载规则，并由两个独立任务子进程执行。

工作内容：

- 迁移 torrent 查找和选择规则。
- 集成单一 qBittorrent 分类。
- 实现独立 torrent-download 任务，按有限批次逐条完成 `.torrent` 查找、下载、校验、qBittorrent 提交、短轮询和失败回退；任意时刻只处理一个提交操作，Python 不负责 BitTorrent 内容传输。
- 实现独立 direct-download 任务，负责 Python direct，并挂接可选 aria2/H@H adapter。
- 实现 Python 流式下载、临时文件、超时、重试和断点续传。
- 下载临时文件使用 generation 和 attempt ID 命名，正式产物使用 generation 专属文件名，禁止覆盖上一 generation。
- torrent-download 与 direct-download 控制任务各自最多一个实例，并允许两者同时运行；已提交的 qBittorrent 后台任务不占 torrent-download 实例名额。
- 定义可选 aria2/H@H adapter 接口。
- 实现 download_pending/downloading/downloaded 状态、租约 fencing 和过期任务人工解除。
- 直接下载时使用 archive_session 再次取得有效入口。
- 所有子进程通过 active_attempt_id、lease_token 和 artifact_generation 条件提交产物和状态。

验收：

- torrent 成功、无种、长期失败和直接下载回退均可恢复。
- torrent-download 与 direct-download 的并发限制互相独立。
- 连续串行提交至少 10 个种子后，数据库允许同时存在 10 个 torrent `downloading` 档案，qBittorrent 可以按自身配置并行下载。
- Python 下载中断后不会产生可上传的假完成文件。
- 过期下载 attempt 不能覆盖新 generation 或把旧文件登记为当前产物。
- qBittorrent 后台运行不持有长期 Python 租约。
- 单账号和双账号的 archive_session 行为正确。

### 阶段 5：验证、准备、上传与清理

目标：完成从下载产物到 LANraragi 完成状态的可靠闭环。

工作内容：

- 实现统一产物验证和隔离目录。
- 实现 attempt 专属临时 ZIP、generation 专属正式文件、CRC 校验和同文件系统内原子重命名。
- 跨文件系统只实现“复制到目标临时名、验证、目标盘内重命名”的简单流程；不增加跨盘事务或断点复制。
- 实现阈值以下文件的流式 LANraragi 上传以及明确的 200/409/400/415/417/422/423/500 分类处理；默认 2 GiB 及以上文件仍停留在 upload_pending，专用大文件传输路径尚未实现。
- 上传响应给出合法 archive ID 时按该 ID 远端核对；未取得 ID 的不确定结果进入 manual_review。
- 实现 409 直接进入 manual_review 和人工登记已有 archive ID 的审计流程。
- 实现 uploaded 到 completed 的幂等清理。
- 实现清理前远端缺失时保留本地数据并进入 manual_review。
- 实现 outdated 到 deleted，并保证替代档案先可用。
- 实现缩略图重建的批次收尾操作。

验收：

- 14KB HTML、截断 ZIP 和错误格式不会进入 upload_pending。
- 被普通 HTTP upload 领取的文件不会完整加载进内存；达到 `large_upload_threshold_bytes` 的文件默认不被领取。
- 上传响应丢失不会导致盲目重复上传。
- 新旧 generation 和两个并发 attempt 的临时/正式文件不会互相覆盖；跨盘中断产物不会进入上传队列。
- 任意清理步骤重复执行不会删除未确认档案或破坏状态。

### 阶段 6：Supervisor 调度闭环

目标：替代旧 main.py 和 run_mode，形成按数据库状态调度的常驻进程。

工作内容：

- 实现 APScheduler 定时采集触发。
- 实现各状态队列的按需子进程启动，torrent-download 与 direct-download 分别判断和调度。
- 实现优先级、串行批次、并发和组件互斥；torrent-download 单实例只限制提交/轮询控制操作，不限制 qBittorrent 后台任务数。
- 实现子进程退出码协议。
- 实现 Supervisor 心跳、过期 attempt 写回拒绝、人工解除过期租约和熔断；不自动回收租约。
- 实现 system_control 暂停/恢复。
- 实现优雅关闭和异常重启恢复。

验收：

- 无任务时不反复启动空子进程。
- 单个档案错误不会停止全部调度。
- 系统级错误会暂停正确组件并留下明确原因。
- Supervisor 崩溃重启后可以从 PostgreSQL 恢复。

### 阶段 7：Web 管理界面

目标：提供查询和人工控制界面，不改变任务执行边界。

工作内容：

- 实现状态总览、队列、人工复核、隔离、事件与错误、配置页面。
- 实现档案详情和状态历史。
- 实现人工备注查看和编辑；修改写入 event_log，但 remark 本身只保留当前内容。
- 实现手工 URL、priority、关键状态人工调整、过期任务解除、409 冲突改名、强制删除和完整操作审计。
- 实现组件暂停/恢复。
- 实现 Supervisor/qBittorrent/LANraragi/存储健康快照和页面。
- 加入认证、权限限制和操作审计。

验收：

- Web 进程停止或重启不影响正在执行的任务。
- Web 不直接访问下载器或执行文件删除。
- 所有人工修改有事件记录和并发冲突保护。
- 页面不泄露 Cookie、token 或代理密码。

### 阶段 8：跨平台部署与运行保障

目标：在不改变业务实现的情况下完成 Windows 和 Linux 的正式运行方式。

工作内容：

- 编写 Windows 服务/任务计划部署说明。
- 编写 Linux systemd 部署说明。
- 编写 PostgreSQL 备份、恢复和 Alembic 升级流程。
- 验证 UNC 与 Linux 挂载路径。
- 验证 wheel/源码发布和目标机重建 venv。
- 实现日志轮转、磁盘检查和基本运维页面。

验收：

- 同一发布版本可在 Windows/Linux 重新创建 venv 并启动。
- 数据库不保存临时文件路径，Windows/Linux 路径差异完全由 app 配置处理。
- 服务异常退出可由操作系统重启；过期租约必须先人工确认旧进程已停止，再从 Web 解除并选择恢复状态。

### 阶段 9：迁移演练与正式切换

目标：把旧 MySQL 和旧运行流程安全切换到新系统。

工作内容：

- 在旧程序继续运行时进行至少一次只读迁移演练。
- 修复迁移报告中的字段、孤立记录和状态问题。
- 冻结旧调度，完成最终 MySQL 备份。
- 人工执行 `scripts/migrate_mysql_to_postgresql.py`、`scripts/verify_migration.py` 和 `scripts/reconcile_migration.py` 完成最终迁移、校验和 reconciliation；Supervisor 不自动调用这些脚本。
- 先以暂停状态启动 Web/Supervisor。
- 人工核对高风险和 manual_review 记录。
- 分组件恢复 collect、download、validate、upload、cleanup。
- 保留旧程序和 MySQL 只读回退窗口。

验收：

- 数据迁移报告通过。
- 随机和重点档案在 PostgreSQL、文件系统和 LANraragi 中一致。
- 新系统稳定完成完整周期后再结束旧系统回退窗口。

### 阶段 10：后续增强

这些增强不得改变核心数据库、状态模型和进程边界：

- 更丰富的通知渠道。
- Web 中的批量人工操作。
- 可选 aria2/H@H 插件完善。
- 更细的指标和趋势统计。
- 配置导入导出。
- 加密的 Web 凭据管理。
- 迁移和清理报告导出。
- torrent-download 与 direct-download 的诊断和限速策略增强；仍保持各自最多一个，不引入 Redis。

## 15. 完成定义

项目达到可替代旧程序的标准时，应同时满足：

- 旧 MySQL 的 manga/mangainfo 业务数据已无损迁移并通过报告校验。
- 一次性迁移和旧库查询逻辑只存在于 `scripts/`；主程序不连接 MySQL、不包含旧状态映射，也不自动执行迁移脚本。
- GP/random 不再参与运行，旧备份仍可追溯。
- 不存在 main/old/special 分支和 state/autostate 双控制。
- 手工优先档案通过 priority 使用同一流程。
- 单账号和双账号配置都能完成采集、下载和上传。
- torrent、Python direct 和可选 adapter 都经过统一验证。
- torrent 文件获取和 qBittorrent 提交保持单实例串行，但多个已提交种子可以同时处于 downloading，实际传输并发由 qBittorrent 管理。
- 无效小文件和错误页面不会上传。
- 上传超时可以核对、恢复，不会简单崩溃或盲目重试。
- 每个正式产物使用 generation 专属文件名，每个临时产物使用 attempt 专属文件名；过期 attempt 无法写回或覆盖当前 generation。
- 同盘使用原子重命名；跨盘中断最多留下不可上传的 attempt 临时文件，不改变数据库当前产物。
- Supervisor 可以从进程崩溃和过期租约恢复。
- Web 只负责管理，不执行实际任务。
- Windows 和 Linux 使用各自重建的 venv 运行同一项目版本。
- 日常使用不需要手工运行内部脚本。
- 系统错误、档案错误和临时错误有清晰、不同的处理结果。
