# EH Archive 特殊处理工作流与视频种子整合计划

## 1. 文档目的

本文档描述 EH Archive 新增“特殊处理工作流”的需求、架构边界、数据库结构、Web 交互、Supervisor 调度、一次性 worker 协议、档案状态流转和扩展方式，并在通用框架之上详细规划第一个专用功能：同一档案的图片 torrent 与视频 torrent 人工选择、分别下载、MP4 转动画 WebP、整合打包并重新接入正常上传流水线。

本文档是设计和实施计划，不包含代码实现。

## 2. 背景与问题

部分 E-Hentai/ExHentai 档案在网站页面中表现为普通图片档案，但 torrent 列表中同时存在：

- 普通图片 ZIP torrent；
- 包含 MP4 文件的视频 ZIP torrent。

期望结果不是二选一，而是同时下载两个版本，将视频中的 MP4 转换为 LANraragi 可以展示的动画 WebP，再与原始图片按确定规则组合成一个最终 ZIP，作为同一个档案上传到 LANraragi。

旧程序通过多个人工脚本完成以下流程：

1. 将 torrent 分成图片与视频两组；
2. 分别提交到 qBittorrent；
3. 等待两个 torrent 完成；
4. 分别解压图片 ZIP 和视频 ZIP；
5. 使用 ffmpeg 将 MP4 转换为动画 WebP；
6. 将 WebP、图片以及可选的原始 MP4 整合打包；
7. 人工把最终 ZIP 交给后续上传流程。

新版目前主动检测视频 torrent。非过时 torrent 中出现视频标记且 remark 不包含 `skip video` 时，torrent 选择任务以 `video_torrent` 结束，档案进入 `manual_review`，避免普通单 torrent 流程误选。这一保护应继续保留。

现有普通流水线不能直接承载双 torrent，原因包括：

- `manga.external_download_id` 只能登记一个外部任务 ID；
- `manga.artifact_*` 只描述一个当前正式产物；
- 普通 torrent worker 只提交和轮询一个 qBittorrent hash；
- validate、prepare 和 cleanup 都围绕一个已登记产物设计；
- 为极少量特殊档案扩展普通状态机和每个普通任务，会增加不成比例的复杂度。

因此，本需求不修改普通 torrent 的单任务业务模型，而是建立一条受控、持久化、可人工交互的特殊处理旁路。旁路最终只向普通流水线交付一个标准 ZIP。

## 3. 目标

### 3.1 通用目标

- Web 可以为一个档案创建特殊处理流程并提交明确的操作请求。
- Supervisor 从数据库领取请求，按白名单启动一次性 worker。
- worker 完成当前阶段后退出，不要求新增常驻服务。
- 一个特殊工作流可以跨越多次 worker 执行、外部下载等待和人工选择。
- Web 能展示工作流阶段、任务状态、模块数据摘要、进度、错误和可用操作。
- 需要用户选择时，worker 先生成选项并退出；用户在 Web 选择后，再排队下一次 worker。
- 特殊流程可以新增、修改、修复、转换或删除档案；完成后按照模块声明的完成契约安全返回现有状态机。
- 普通采集、下载、校验、准备、上传和清理流程不理解特殊模块内部细节。
- 所有外部副作用、人工操作、状态变化和失败都可审计、可恢复。
- 后续特殊需求复用同一套排队、租约、进度、Web 外壳和 Supervisor 调度能力。

### 3.2 视频整合目标

- 展示 torrent 页面中的候选列表，而不是强制依赖复杂自动选择算法。
- 用户分别选择一个图片 torrent 和一个视频 torrent。
- 分别持久化两个 qBittorrent hash 及其进度快照。
- qBittorrent 长时间下载期间不保持数据库事务、Python worker 或 Manga 普通任务租约。
- 两个 torrent 提交后不运行常驻或自动轮询 worker；用户认为下载已接近完成时，再从 Web 的模块管理页或命令行人工启动一次批量检查。
- 批量检查只负责为符合条件的 workflow 分别创建一次性 job；每个 job 独立确认两个下载，未完成的继续等待，已全部完成的直接进入转换整合。
- 安全解压两个源 ZIP，将 MP4 转换为动画 WebP。
- 按确定目录和命名规则创建最终 ZIP。
- 最终 ZIP 校验成功后，将档案恢复为 `downloaded`，交回现有 validate/upload/cleanup 流程。

## 4. 非目标

本计划不做以下内容：

- 不让普通 `torrent_download` 自动处理双 torrent。
- 不把多个 hash 塞入 `manga.external_download_id`。
- 不用 remark 保存 hash、内部路径或可反向驱动程序的机器状态；remark 只允许保存由数据库状态生成的模块名、阶段和进度摘要。
- 不让 Web 直接访问 qBittorrent、运行 ffmpeg、删除文件或启动任意命令。
- 不允许用户从 Web 输入任意 Python 模块名或命令行。
- 不建立自动生成任意动态表单的“万能插件系统”。
- 不要求特殊模块都使用同一套业务阶段；通用框架只统一调度和持久化边界。
- 不增加 `special_resource` 表。模块专属的多个 hash、候选、输入和输出统一保存在 `special_workflow.payload` JSONB 中；特殊模块并不都围绕“资源”工作，独立资源表不属于通用框架。

## 5. 总体架构

```text
用户浏览器
    |
    | 查看状态、选择候选、提交白名单操作
    v
Web 进程
    |
    | 只写 PostgreSQL：special_workflow / special_job / event_log
    v
PostgreSQL
    |
    | Supervisor 轮询 queued job
    v
Supervisor
    |
    | 领取 job、建立租约、启动白名单一次性 worker
    v
Special Worker
    |          |             |
    |          |             +-- 文件系统 / ffmpeg
    |          +-- qBittorrent
    +-- EH torrent 页面
    |
    | 写回阶段、模块数据、进度、错误和处理结果
    v
PostgreSQL
    |
    | 视频整合成功后 manga.status = downloaded
    v
现有 validate -> upload -> cleanup 流水线
```

各组件边界如下：

- Web：接收用户请求、校验当前阶段和并发版本、创建 job、展示数据库快照。
- Supervisor：判断何时启动 worker、领取 job、跟踪子进程、处理退出码和全局暂停。
- Special worker：执行一个白名单 operation，直接处理对应外部系统或文件。
- `special_workflow`：保存跨多次执行的长期业务上下文、模块 JSONB 数据和处理结果。
- `special_job`：保存一次待执行或已经执行的 worker 请求。
- `manga`：保存顶层正式状态和正式业务字段，不保存特殊流程内部的多个中间 hash；模块可以按照完成契约登记、修改或删除相应档案数据。
- `event_log`：保存人工动作、任务领取、阶段变化、失败、恢复和完成审计。

## 6. Manga 状态机变化

### 6.1 新增状态

在 Manga 状态中新增：

```text
special_processing
```

该状态只表达：当前档案由一个活动的特殊工作流控制，普通任务不得领取它。具体是等待用户、下载中、转换中还是失败，由 `special_workflow.phase` 表达。

### 6.2 从 `manual_review` 进入 `special_processing`

`manual_review` 是特殊处理的人工入口和安全闸门。以视频 torrent 为例，普通 torrent worker 发现 `video_torrent` 后不会提交任何 torrent，而是让档案停留在 `manual_review`。此时用户可以先查看错误、Manga 信息和候选的特殊模块，再决定是否进入特殊处理。

本设计采用“程序自动识别和推荐，用户明确授权，程序执行原子迁移”的原则：

- 程序可以根据错误代码、档案状态和模块入口规则自动判断哪些特殊模块适用；
- Web 可以自动展示推荐模块和原因；
- 程序默认不得仅凭检测结果自动创建 workflow 或把 Manga 改成 `special_processing`；
- 用户必须通过明确的模块按钮授权进入；
- 用户不是在通用状态修改表单中直接填写 `special_processing`，而是请求一个经过模块规则校验的业务动作；
- 真正的状态迁移、workflow 创建和审计由程序完成。

视频模块固定采用人工进入，因为它需要用户选择图片 torrent 和视频 torrent，并可能产生额外下载、磁盘占用和长时间转换。即使识别结果非常明确，也只自动推荐，不自动启动。

进入过程不是脚本通过 remark 自动触发，而是用户在 Web 点击明确的模块操作，例如：

```text
[进入视频档案特殊处理]
```

Web 在一个数据库事务中完成：

1. 锁定 Manga，并检查 `row_version`；
2. 确认 Manga 当前为允许进入该模块的状态，视频模块默认要求 `manual_review`；
3. 确认没有活动普通 attempt、未解除租约或其他活动 workflow；
4. 创建 `special_workflow`，写入 `kind=video_archive`、`status=active`、初始 phase 和 `resume_status=manual_review`；
5. 在 workflow payload 中保存进入原因、原错误代码和必要的初始上下文；
6. 将 Manga 状态迁移为 `special_processing`；
7. 在 Manga remark 中写入或更新只读的特殊处理摘要块；
8. 写入 `event_log` 的 `special_start` 审计事件；
9. 提交事务。

任一步失败时整个事务回滚，Manga 继续保持 `manual_review`，不会出现“状态已进入特殊处理但 workflow 不存在”的半完成记录。

进入特殊处理与启动第一个 worker 是两个概念：

- 通用入口只创建 workflow 并切换 Manga 状态，初始 phase 可以等待用户继续操作；
- 专用页面可以提供“进入并加载 Torrent 列表”的组合按钮，在同一事务中额外创建第一个 queued `special_job`；
- 无论采用哪个按钮，Supervisor 只根据 `special_job` 领取 worker，不根据 Manga 状态或 remark 猜测要运行什么。

进入 `special_processing` 后，普通下载、校验、上传和清理 worker 都不再领取该 Manga。Web 的特殊处理面板成为该档案的主要控制入口，直到 workflow 成功、取消或人工退出。

通用强制状态修改功能不得把 Manga 单独改成 `special_processing`，也不得在没有 Manga 状态迁移的情况下单独创建活动 workflow。这两项必须由专用入口在同一事务中完成，否则会产生没有 workflow 的孤立状态或仍被普通 worker 领取的失控 workflow。

未来某个模块如果确实需要自动进入，必须同时满足：

- 模块在 registry 中显式声明支持自动入口；
- 模块配置明确启用 `auto_start`，默认值必须为 `false`；
- 当前 Manga 状态位于模块允许的自动入口白名单；
- 模块不需要用户选择；
- 模块不执行删除、高风险覆盖或不可恢复修改；
- 模块配置和依赖健康检查通过；
- 有明确的取消、失败和恢复路径。

删除类模块和本视频模块均不允许自动进入。

### 6.3 建议迁移事件

建议增加以下稳定事件：

```text
manual_review + special_start -> special_processing
special_processing + special_downloaded -> downloaded
special_processing + special_cancel -> manual_review
special_processing + special_restore -> resume_status
```

`special_downloaded` 是视频整合模块的完成事件，不是所有特殊模块的固定出口。修改、修复或删除类模块必须声明自己的完成事件和目标状态，例如返回 `completed`、`outdated`、`deleted` 或 `manual_review`。

通用框架中的典型完成契约可以是：

| 特殊模块类型 | 对 Manga 的主要作用 | 可能的正常返回状态 |
| --- | --- | --- |
| 视频种子整合 | 生成并登记新的标准 ZIP | `downloaded` |
| 元数据修复 | 修改现有 Manga/MangaInfo 字段 | 进入前状态或 `manual_review` |
| 档案重命名/修复 | 修改现有产物并要求重新校验 | `downloaded` 或 `validating` |
| 远端核对 | 只修改确认信息和审计结果 | 进入前状态、`uploaded` 或 `manual_review` |
| 特殊删除 | 删除远端或本地产物并登记结果 | `deleted` 或 `manual_review` |

每个 kind 必须显式声明允许的入口状态、成功返回状态、取消返回状态以及会修改的业务字段，不能由通用框架根据是否存在最终文件进行猜测。

失败通常不立即离开 `special_processing`，而是将 workflow 标记为 `failed` 或将 phase 标记为相应错误阶段，等待用户重试、取消或退出特殊处理。

### 6.4 进入和退出约束

- 每个 Manga 同一时刻最多存在一个活动 workflow。
- 创建 workflow、写入 `special_start` 事件和把 Manga 改为 `special_processing` 必须在同一个数据库事务中完成。
- Manga 有活动普通 attempt 或未解除的租约时，不允许进入特殊处理。
- 特殊 workflow 有 running job 或有效租约时，不允许强制退出。
- 成功退出时，应用模块声明的数据变更、完成 workflow、完成 job、修改 Manga 状态和写事件必须使用同一事务。视频整合的数据变更是登记最终产物；其他模块可以是修改或删除档案数据。
- 取消时默认回到 `manual_review`；通用框架仍保存 `resume_status`，供未来从其他状态进入的模块使用。
- 普通 `has_work()` 和 `claim_next()` 不领取 `special_processing`，无需了解特殊 phase。
- workflow、job 和 JSONB payload 是唯一控制事实来源；remark 摘要缺失、被人工改动或显示滞后都不能改变调度结果。

## 7. 数据库结构变化

最终新增两个表：`special_workflow` 和 `special_job`。

### 7.1 `special_workflow`

一条记录代表一个 Manga 从进入特殊处理到成功、取消或失败退出的完整过程。

建议字段：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `id` | bigint PK | 工作流 ID |
| `manga_id` | varchar FK | 所属 Manga |
| `kind` | varchar | 特殊流程类型，如 `video_archive` |
| `status` | varchar | `active/completed/failed/cancelled` |
| `phase` | varchar | 当前模块自定义阶段 |
| `resume_status` | varchar | 取消或退出时的恢复目标 |
| `payload` | JSONB | 候选、用户选择、多个 hash、输入输出和模块专属数据 |
| `progress` | JSONB | 当前阶段的有限频率进度快照 |
| `row_version` | bigint | Web 与 worker 乐观并发控制 |
| `error_code` | text nullable | 当前错误代码 |
| `error_detail` | text nullable | 当前错误摘要 |
| `created_by` | text | 创建者 |
| `created_at` | timestamptz | 创建时间 |
| `updated_at` | timestamptz | 更新时间 |
| `completed_at` | timestamptz nullable | 终止时间 |

建议约束和索引：

- `status` 只能为预定义的通用状态。
- `kind` 必须由服务层白名单校验。
- 对 `manga_id` 建立“仅活动记录唯一”的 PostgreSQL 部分唯一索引。
- 为 `(status, updated_at)` 建立管理页面查询索引。
- `payload` 和 `progress` 必须始终是 JSON object，不保存 Cookie、代理密码、Authorization 或 torrent 二进制。

视频模块的 `payload` 示例结构：

```json
{
  "torrent_snapshot": {
    "fetched_at": "2026-08-25T12:00:00Z",
    "choices": [
      {
        "choice_id": "stable-id",
        "site_id": "gtid-or-derived-id",
        "label": "gallery video.zip",
        "size": "1.7 GiB",
        "size_bytes": 1825361100,
        "seeds": 4,
        "posted_at": "2026-08-24T00:00:00Z",
        "outdated": false,
        "resampled": false,
        "suggested_role": "video"
      }
    ]
  },
  "selection": {
    "image_choice_id": "image-id",
    "video_choice_id": "video-id",
    "confirmed_warnings": []
  },
  "torrents": [
    {
      "role": "image",
      "provider": "qbittorrent",
      "external_id": "image-hash",
      "status": "completed",
      "progress": 1.0,
      "speed_bps": 0,
      "content_path": "server-side-path",
      "updated_at": "2026-08-25T13:00:00Z"
    },
    {
      "role": "video",
      "provider": "qbittorrent",
      "external_id": "video-hash",
      "status": "downloading",
      "progress": 0.63,
      "speed_bps": 8388608,
      "content_path": null,
      "updated_at": "2026-08-25T13:00:00Z"
    }
  ],
  "final_artifact": null
}
```

上述结构是模块数据契约，不要求用户或 Web 直接编辑 JSON。

### 7.2 `special_job`

一条记录代表一次等待 Supervisor 启动或已经完成的一次性 worker。

建议字段：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `id` | bigint PK | Job ID |
| `workflow_id` | bigint FK | 所属 workflow |
| `operation` | varchar | 白名单 operation |
| `status` | varchar | `queued/running/succeeded/failed/abandoned/cancelled` |
| `trigger_source` | varchar | `web/cli/system`；批量入口仍记录真实发起来源 |
| `requested_by` | text | 用户或系统身份 |
| `attempt_no` | integer | 同 operation 的第几次尝试 |
| `next_run_at` | timestamptz | 最早领取时间 |
| `lease_token` | uuid nullable | worker fencing token |
| `lease_owner` | text nullable | Supervisor/worker 所有者 |
| `lease_until` | timestamptz nullable | 租约到期时间 |
| `progress` | JSONB | 本次 worker 的当前进度 |
| `external_effect_started_at` | timestamptz nullable | 外部副作用开始标记 |
| `error_code` | text nullable | 失败代码 |
| `error_detail` | text nullable | 失败摘要 |
| `created_at` | timestamptz | 排队时间 |
| `started_at` | timestamptz nullable | 启动时间 |
| `finished_at` | timestamptz nullable | 完成时间 |

建议约束和索引：

- `status` 和 `trigger_source` 使用 CHECK 约束。
- 对 `(status, next_run_at, created_at)` 建立领取索引。
- 同一个 workflow 同一时间最多一个 `running` job。
- 同一个 workflow、operation 可以有多个历史 job，但 `attempt_no` 唯一。
- 已过期的 running job 不自动接管；与普通任务一致，需要确认旧进程已经停止后人工解除或重试。
- 删除 workflow 时不级联删除审计历史；正常业务不提供删除 workflow 的操作。

### 7.3 为什么不增加 `special_resource`

`special_resource` 不属于本设计。特殊模块的通用对象是“一个需要多次执行、可能等待用户或外部系统的处理过程”，并不一定包含新增资源。例如特殊模块可能只修改元数据、修复状态、重命名档案、删除档案或执行一次核对。

因此数据库只抽象：

- `special_workflow`：长期处理过程和模块专属 JSONB；
- `special_job`：一次性 worker 请求。

视频流程中的图片 hash、视频 hash、候选列表、下载快照和最终文件信息保存在 `special_workflow.payload`。不同 kind 可以定义完全不同的 payload schema，不要求把所有模块的数据强行映射成资源行。

如果未来某一个专用模块的数据量很大，应由该模块基于真实需求设计自己的专用表，而不是把 `special_resource` 预设为所有特殊模块的通用依赖。

## 8. Web 变化

### 8.1 Web 的职责

Web 只执行以下动作：

- 创建特殊 workflow；
- 创建白名单 special job；
- 在模块级批量操作中按固定条件筛选 workflow，并为每个 workflow 创建独立白名单 job；
- 保存用户选择；
- 请求暂停、重试、检查、取消或退出，以及模块显式声明的其他白名单动作；
- 展示数据库中的 workflow、job、模块数据摘要和进度快照；
- 将 workflow 中适合用户阅读的模块名、阶段和进度同步为 Manga remark 摘要；
- 使用 `row_version` 阻止覆盖并发修改；
- 写入完整人工审计事件。

Web 不执行以下动作：

- 不直接启动 Python 子进程；
- 不直接访问 qBittorrent；
- 不直接访问 EH torrent 页面；
- 不运行 ffmpeg；
- 不解压、打包或删除文件；
- 不接受任意 module 名、Python 路径或 shell 参数。

### 8.2 档案详情页特殊处理面板

当 Manga 为 `manual_review` 且错误为 `video_torrent` 时，显示：

```text
检测到视频 Torrent
[进入视频档案特殊处理]
[进入并加载 Torrent 列表]
[继续普通流程并跳过视频]
```

按钮语义：

- “进入视频档案特殊处理”只创建 workflow 并进入等待用户操作的初始 phase；
- “进入并加载 Torrent 列表”在同一事务中创建 workflow 和第一个 queued `load_torrent_options` job；
- “继续普通流程并跳过视频”沿用现有显式人工确认语义，不创建特殊 workflow；
- 所有按钮都通过业务 action 执行，不能退化为任意状态覆盖。

进入后，普通人工状态操作应根据风险适当收紧，并显示：

- workflow 类型与整体状态；
- 当前 phase；
- 最近一次 job；
- 最近错误；
- 模块专属数据摘要；视频模块显示图片和视频 torrent 列表；
- 当前进度和更新时间；
- 允许的下一步按钮；
- 工作流事件记录。

### 8.3 页面刷新方式

特殊处理面板使用 HTMX partial 定期读取数据库。建议：

- running/转换阶段：每 2～5 秒刷新；
- downloading 阶段：页面可以低频刷新数据库，但不因此访问 qBittorrent；只有用户触发批量检查或“立即检查这个档案”时才更新下载快照；
- awaiting user/ready/failed 阶段：停止自动刷新或降低频率；
- 页面明确显示“上次检查时间”和“最近一次已知进度”，避免把旧快照误认为实时数据。

Web 的刷新只读取 PostgreSQL，不会导致对 EH、qBittorrent 或文件系统的额外访问。

### 8.4 Remark 状态摘要

Manga remark 可以保存特殊模块的用户可读摘要，但它不是控制字段。真实状态始终来自 `special_workflow`、`special_job` 和 workflow payload。Supervisor、Web action 校验和 worker 都不得通过解析 remark 决定是否排队、领取、重试、取消或执行模块。

建议在 remark 中维护一个有明确边界的系统生成区块，例如：

```text
这里是用户原有的人工备注。

[special-processing]
模块：视频种子下载与整合
阶段：下载中
进度：图片 100%，视频 63%
最后更新：2026-08-26 00:15:00
[/special-processing]
```

同步规则：

- 进入特殊处理时创建摘要块；
- workflow phase、重要进度、失败或完成结果变化时更新摘要块；
- 只更新边界标记内部内容，保留用户在区块之外的人工备注；
- 不在摘要中记录 hash、Cookie、私有 torrent URL、Authorization、内部绝对路径或完整错误堆栈；
- 下载百分比只在人工批量检查或单档案检查得到新快照后同步，不能把页面刷新伪装成实时进度；
- remark 摘要更新不作为新的控制事件，重要 phase 变化仍写 `event_log`；
- Web 编辑普通 remark 时必须保留系统摘要块，或者将摘要块作为只读区域与人工备注输入框分开呈现；
- 摘要块被人工删除、损坏或显示滞后时，可以根据 workflow 重新生成，但不能影响实际任务；
- workflow 完成、取消或失败退出后保留最终摘要，记录模块名、结果和返回状态，方便用户从普通档案页面了解历史流转。

这使用户即使不展开特殊工作流面板，也能在现有档案详情和 remark 展示位置看到档案曾经或正在经过哪个特殊模块，同时不会重新引入“remark 隐式控制状态”的旧式设计。

### 8.5 Torrent 人工选择界面

`load_torrent_options` 成功后，workflow 进入 `awaiting_torrent_selection`。页面列出所有解析到的候选，至少显示：

- 名称；
- 大小；
- Seeder；
- 发布时间；
- 是否位于 outdated 分区或红色时间；
- 是否命中视频标记；
- 是否命中重采样标记；
- 系统建议角色；
- 可能的风险警告。

每一行允许选择：

```text
作为图片 torrent
作为视频 torrent
```

提交约束：

- 必须恰好选择一个图片 torrent 和一个视频 torrent；
- 两个角色不能选择同一候选；
- 选择无 Seeder、过时或重采样候选时必须明确二次确认；
- Web 只提交内部 `choice_id`，不提交原始下载 URL；
- 保存选择时检查 workflow `row_version` 和 phase；
- 保存成功后创建 `submit_selected_torrents` job。

视频模块采用逐档案交互：用户从档案详情点击“进入并加载 Torrent 列表”后跳转到该 workflow 的候选选择页；选择并确认后，页面显示提交排队、qBittorrent 已接收或错误结果。若有 10 个待处理档案，用户就分别进入 10 个选择页完成 10 次人工选择。这个步骤本来就需要人的判断，因此不强行为了减少点击而引入复杂的自动选种算法或多档案大表单。

候选确认后的 worker 只负责下载所选 `.torrent` 文件并提交给 qBittorrent。它不会在此时上传 LANraragi，也不会等待大文件下载完成。成功保存两个 hash 后页面进入 `downloading`，用户可以关闭该页面并继续选择下一个档案。

### 8.6 阶段按钮

建议根据 phase 仅显示有效操作：

| Phase | Web 操作 |
| --- | --- |
| `awaiting_torrent_load` | 加载 Torrent 列表、退出特殊处理 |
| `loading_torrent_options` | 查看进度、请求取消 |
| `awaiting_torrent_selection` | 确认选择、重新加载、退出 |
| `torrent_submit_queued` | 查看排队状态、取消未启动 job |
| `downloading` | 立即检查这个档案、取消并清理 |
| `checking_downloads` | 查看本次检查状态 |
| `extracting` / `converting` / `packing` | 查看转换进度、请求暂停或取消 |
| `failed` | 查看错误、重试当前阶段、退出或清理 |
| `ready` | 查看最终产物及回归正常流水线结果 |

按钮请求必须幂等。重复点击不能创建多个等价 queued job。

### 8.7 Web API 与安全

建议使用明确路由，不提供任意 operation 参数的通用执行入口。例如可以在服务层维护允许动作与 phase 的映射，路由仍提交受控 action。

所有请求必须：

- 使用现有认证；
- 校验 `row_version`；
- 校验 Manga、workflow、phase 和 job 归属；
- 校验 operation 是否属于 workflow kind 的白名单；
- 写 `event_log`；
- 不把 Cookie、代理、Authorization、torrent 私有 URL 和本地敏感绝对路径输出到页面。

## 9. Supervisor 变化

### 9.1 特殊任务调度位置

特殊任务不加入现有 `TASK_OPERATIONS` 和 `ArchiveRepository.claim_next()`。普通任务领取依赖 Manga 正式状态机，而 special job 有自己的状态、租约和操作白名单。

Supervisor 每次 tick 在普通调度之外执行特殊 job 检查：

1. 确认不在维护窗口；
2. 确认 Supervisor 未 paused/draining；
3. 确认特殊模块整体未暂停；
4. 查询是否有到期的 queued special job；
5. 确认对应 operation 当前没有运行中的子进程；
6. 领取一条 job 并提交事务；
7. 启动特殊 worker 子进程；
8. 在现有子进程回收机制中处理退出码。

### 9.2 领取条件

概念查询条件：

```text
special_job.status = queued
special_job.next_run_at <= now
special_job.lease_until IS NULL
special_workflow.status = active
manga.status = special_processing
```

领取时使用 `FOR UPDATE SKIP LOCKED`，写入：

- `status=running`；
- `lease_token`；
- `lease_owner`；
- `lease_until`；
- `started_at`；
- workflow `row_version` 或必要的活动 job 关联；
- `event_log` 领取事件。

### 9.3 Worker 启动协议

Supervisor 只能从固定 registry 解析 operation：

```text
load_torrent_options
submit_selected_torrents
check_and_compose_if_ready
cancel_video_archive
```

registry 决定：

- Python 模块；
- 是否允许 Web 触发；
- 是否允许 system 触发；
- 默认租约；
- 并发上限；
- 严重错误处理规则。

禁止将数据库中的字符串直接拼成 shell 命令。Supervisor 使用参数数组和当前 Python 解释器启动固定模块，并传入 job ID、workflow ID、lease token、config directory、run ID 和日志路径。

### 9.4 并发策略

默认并发建议：

- 所有视频整合 operation 总并发默认为 1；
- torrent 列表加载、torrent 提交和只读完成检查可与普通任务并行；
- `check_and_compose_if_ready` 在确认两个下载完成后会继续执行 CPU、磁盘密集型转换，默认单实例；
- 同一 workflow 同时只能运行一个 job；
- 同一个 Manga 只能有一个活动 workflow；
- ffmpeg 内部并行数单独配置，不能等同于 Supervisor worker 并发。

### 9.5 人工批量检查与独立排队

`submit_selected_torrents` 的职责到此为止：下载两个 `.torrent` 文件、提交给 qBittorrent、分别保存 hash，将 workflow phase 改为 `downloading`，然后退出。这里的“提交”指提交给 qBittorrent，不是上传到 LANraragi。它不会创建定时检查 job，也不会保持 worker 常驻。

用户可以在独立的特殊模块管理页面点击“批量检查已下载的视频档案”，也可以人工运行等价的受控命令，例如：

```text
eharchive special video-archive collect-ready
```

这次批量动作本身只是一个短暂的数据库 dispatcher。Web 按钮在请求事务中调用固定服务层；CLI 命令调用同一服务层。两者都不创建无归属的“批量 job”，也不启动子进程，而是查询：

```text
special_workflow.kind = video_archive
special_workflow.status = active
special_workflow.phase = downloading
不存在同 workflow、同 operation 的 queued/running job
```

然后为每个符合条件的 workflow 各创建一个 `check_and_compose_if_ready` job，并立即返回“找到多少、排队多少、跳过多少”。这样 `special_job.workflow_id` 仍然始终指向一个具体 workflow，不需要第三张表或一个模块级常驻任务。dispatcher 不能只根据 `manga.status=special_processing` 扫描，也不能直接读取 qBittorrent、转换或删除任何档案。

Supervisor 不读取 Web 请求、按钮状态或 remark；它只会在下一轮数据库调度时看到新产生的 queued jobs，逐条领取并启动固定 registry 中的 worker。因此从用户角度是在 Web 运行了一次“批量收集模块”，从执行边界看仍然是 Web 只排队、Supervisor 才启动一次性处理进程。

Supervisor 之后逐个领取这些独立 job，默认转换并发为 1。每个 job：

1. 按两个 hash 查询 qBittorrent，并验证精确 category 和路径所有权；
2. 更新 workflow payload 中两个 torrent 的最近快照和 `last_checked_at`；
3. 任一 torrent 未完成时，保留 phase=`downloading`，把本次 job 正常标记为 succeeded，不安排下一次检查；
4. 两个 torrent 均完成时，在同一个 job 中进入安全解压、MP4 转 WebP、整合打包和最终产物登记；
5. 单个 workflow 失败只影响自己的 job，不能阻塞同批次的其他档案。

用户稍后再次执行同一批量动作，仍未完成的 workflow 才会获得新的检查 job。档案专属页面还可以提供“立即检查这个档案”，它创建完全相同的 operation，只是范围限制为当前 workflow。两种入口都必须幂等，重复点击不能生成等价的活动 job。

### 9.6 维护、暂停与关闭

- 维护窗口内不启动新的 special worker。
- 已运行的转换 worker按现有优雅关闭策略结束或在超时后被终止。
- 被终止的 job 不得直接由新 worker覆盖；租约过期后必须确认旧进程停止，再人工解除或重试。
- 特殊模块可以有独立 `system_control` component，例如 `special_video_archive`。
- 暂停模块不取消 qBittorrent 下载，只停止新的提交、人工检查和转换 job。

## 10. 一次性 Worker 通用协议

### 10.1 启动和验证

worker 启动后必须：

1. 加载配置；
2. 按 ID 获取 job 和 workflow；
3. 验证 job 为 running；
4. 验证 lease token、owner、workflow kind、operation 和 Manga 状态；
5. 确认 operation 在本地白名单中；
6. 配置结构化日志；
7. 执行对应 handler。

### 10.2 写回保护

- 每次关键写回都检查 job lease token。
- 最终产物提升还必须检查 Manga 当前 artifact generation，防止旧 worker 覆盖新产物。
- 外部副作用开始前记录 `external_effect_started_at`。
- qBittorrent 每成功提交一个 torrent 后立即持久化该 hash，不等待另一个提交完成。
- worker 崩溃后重试必须先根据 category、确定性名称和 save path 与 qBittorrent reconciliation，不能盲目再次提交。

### 10.3 进度更新

- `special_job.progress` 表示本次执行进度。
- `special_workflow.progress` 表示用户关心的整体阶段快照。
- 网络下载不由常驻 worker 实时推送，也不自动轮询；人工批量检查或单档案检查 job 才更新最近快照。
- 转换阶段按文件完成或至少间隔 2 秒更新，避免高频数据库写入。
- progress 中保存计数、总数、当前文件、字节、速度和摘要，不保存超长 ffmpeg stderr。

### 10.4 退出结果

一次性 worker 可以产生：

- `succeeded`：本 operation 完成；
- `failed`：该 operation 失败，workflow 等待人工重试；
- `abandoned`：旧 worker 已确认不再有效；
- `cancelled`：尚未产生或已安全清理副作用；
- 进程级严重退出码：沿用现有 Supervisor 对系统错误和 EH 全站不可用的分类策略。

## 11. 通用特殊处理扩展机制

### 11.1 固定 Registry

通用框架提供固定 registry，每个 kind 显式注册：

- 允许的 phases；
- 允许的 operations；
- 每个 phase 的 Web actions；
- operation handler；
- 输入校验；
- 成功和失败后的 phase；
- 最终可以返回的 Manga 状态；
- 默认租约与并发限制。

Web 和 Supervisor 都使用同一套服务层定义，但 Web 永远不能通过请求提供模块路径。

### 11.2 通用部分

以下能力由框架统一提供：

- workflow 创建、唯一活动约束和退出；
- job 排队、领取、租约、重试和取消；
- 单 workflow 交互动作，以及按 kind、status、phase 筛选并为多个 workflow 分别排队的模块级批量动作；
- Supervisor 子进程管理；
- phase/status/progress/error 展示；
- optimistic concurrency；
- event audit；
- 维护窗口、组件暂停和健康状态；
- 安全的 handler registry；
- 最终产物回归普通流水线的事务边界。

### 11.3 专用部分

每种特殊需求单独实现：

- 页面上的专属选择控件；
- payload schema；
- operation handler；
- 外部资源检查；
- 文件处理规则；
- phase 迁移规则；
- 完成条件和清理策略。

本设计不提供通用动态表单 DSL。新增特殊类型时，应增加明确的 kind、白名单 operation、校验器和模板组件。

通用框架允许两种操作范围：

- workflow-scoped：由某个档案详情页发起，只为当前 workflow 创建 job，例如加载候选、提交所选 torrent、立即检查当前档案；
- module-scoped batch enqueue：由模块管理页或受控命令发起，按模块声明的 kind、status 和 phase 选择 workflow，并为每个 workflow 创建独立 job。

模块级批量动作只负责筛选和排队，不把多个档案的文件处理塞进同一个长进程。这样既支持本次视频需求，也能复用到未来的批量格式修复、远端核对等模块；各模块仍必须自行声明筛选条件、幂等键和单 workflow operation。

### 11.4 可能的后续模块

框架未来可以承载但不在本需求中实现：

- 人工选择多个源档案并合并；
- 损坏 ZIP 修复后重新校验；
- 特殊格式转码；
- 人工导入外部本地档案；
- 多卷归档合并；
- 特殊元数据重建；
- 需要用户从多个远端匹配结果中选择的修复任务。

## 12. 视频种子下载专用模块

### 12.1 Workflow Kind 与 Operations

Kind：

```text
video_archive
```

视频模块 operations：

```text
load_torrent_options
submit_selected_torrents
check_and_compose_if_ready
cancel_video_archive
```

可选的人工 `refresh_torrent_options` 可以复用 `load_torrent_options`，不必增加新的 handler。

### 12.2 Phase 定义

建议 phases：

```text
awaiting_torrent_load
loading_torrent_options
awaiting_torrent_selection
torrent_submit_queued
submitting_torrents
downloading
checking_downloads
extracting
converting
packing
ready
failed
cancelling
cancelled
```

phase 是模块级字符串，不加入 Manga status CHECK。handler 必须验证允许的迁移，不能任意写字符串。

### 12.3 Torrent 列表解析

加载 worker 继续使用结构化 HTML 解析，并展示所有可识别候选，包括：

- 活动候选；
- outdated 分区候选；
- 红色时间候选；
- 无 Seeder 候选；
- 重采样候选；
- 命中 `video_markers` 的候选。

系统只提供建议和风险标记，不替用户作最终选择。无法安全解析的页面仍作为系统或档案错误处理，不能把未知 HTML 当成候选。

候选 ID 应基于站点稳定 ID；若页面没有合适稳定 ID，则根据规范化后的站点标识、名称、时间、大小生成摘要 ID。原始私有 URL 不返回浏览器。

### 12.4 用户选择后的重新确认

用户确认后，提交 worker重新获取 torrent 页面，而不是无条件使用可能过期的旧 URL。它根据稳定 ID 或候选特征重新匹配：

- 匹配成功：下载当前有效 `.torrent`；
- 候选已变化：job 失败为 `torrent_selection_stale`，workflow 回到等待重新加载；
- 被选候选已变成 outdated 或无 Seeder：要求用户重新确认；
- 下载内容不是 bencode dictionary：作为无效 torrent 处理。

### 12.5 qBittorrent 所有权与命名

特殊任务使用独立精确 category，例如：

```text
eharchive-video-special
```

普通 `eharchive` category 的所有权规则不变，普通 worker 和 cleanup 不处理特殊 category。

建议确定性名称和路径：

```text
显示名称：{id}-image / {id}-video
保存路径：special_video_download/{safe_manga_id}/image
          special_video_download/{safe_manga_id}/video
```

如果 qBittorrent 位于其他主机，需要像现有 torrent root 一样区分 qBittorrent 可见根路径和本机读取根路径，并安全映射相对路径。

提交顺序不能假设原子性：

1. 提交图片 torrent；
2. 成功后立即保存 image hash；
3. 提交视频 torrent；
4. 成功后立即保存 video hash；
5. 任一步崩溃后，重试先按 category、名称和 save path 查找已存在任务。

### 12.6 下载进度、人工检查与异常

每次人工批量检查或单档案检查 job 保存每个资源的：

- hash；
- qBittorrent state；
- progress；
- total size；
- downloaded bytes；
- speed；
- ETA（若可计算）；
- completion time；
- last checked time；
- content path；
- error/tag 摘要。

这些字段是最近一次人工检查得到的快照，不是实时数据。Web 必须同时显示 `last checked time`；普通页面刷新只读数据库，不触发 qBittorrent 查询。

规则建议：

- 一个完成、一个未完成：继续等待，不重新提交完成项；
- 任务暂时无速度：只显示状态，不自动删除；
- qBittorrent 丢失 hash：workflow 失败，等待人工确认；
- category 被人工移走：暂停管理该资源并提示，不读取或删除；
- 用户取消：由 `cancel_video_archive` 重新确认 category 后删除两个任务及文件；
- 两个任务都完成：当前 `check_and_compose_if_ready` job 直接进入解压、转换和打包阶段；
- 任一任务未完成：phase 保持 `downloading`，当前检查 job 正常结束且不安排自动重试，等待用户下次批量检查；
- 同一批次中一个档案检查或转换失败，不影响其他 workflow 的独立 job。

### 12.7 转换整合工作区

增加受控根目录，例如：

```text
special_video_work
```

每次 compose 使用 generation 和 job ID 专属目录：

```text
special_video_work/{safe_manga_id}/g{generation}/j{job_id}/
    source_image/
    source_video/
    output/
        1_webp/
        2_pic/
        3_video/
```

不得直接修改 qBittorrent 正在做种的源文件。

### 12.8 安全解压

在真正解压前必须：

- 确认两个内容路径位于配置根目录；
- 确认每个选中产物是唯一、可识别的 ZIP；
- 验证 ZIP 结构与 CRC；
- 拒绝绝对路径、盘符路径和 `..` 穿越；
- 拒绝符号链接或重解析点成员；
- 设置最大成员数、最大单文件大小和最大展开总量；
- 检查磁盘可用空间；
- 只解压到当前 job 专属目录。

源 ZIP 可能包含额外单层根目录，专用模块可以规范化 payload root，但不能凭文件名猜测并静默丢弃其他内容。

### 12.9 MP4 转动画 WebP

ffmpeg 调用由专门适配层负责：

- ffmpeg 可执行文件来自配置；
- 启动前检查版本和编码器能力；
- 参数使用列表传递，不通过 shell 拼接；
- 设置单文件超时；
- 捕获并截断 stderr 作为错误摘要；
- 输出先写临时文件；
- 成功后验证 WebP 文件头、大小和必要的动画信息；
- 输出有效后再提升为正式 WebP；
- 已存在且校验通过的输出允许在重试时跳过；
- 任一必需 MP4 转换失败时，不生成最终 ZIP。

旧脚本参数可以作为初始参考，但应改为配置项并经过样本验证：

- quality；
- compression level；
- loop；
- 是否去除音频；
- 最大并行 worker 数；
- 单文件和整体超时；
- 最大输出大小。

### 12.10 整合目录与顺序

默认可以延续旧版可理解的目录结构：

```text
1_webp/
2_pic/
3_video/
```

转换后的文件使用确定性名称，例如保留相对目录并为文件增加统一前缀。必须处理：

- 不同目录中的同名 MP4；
- Unicode 和 Windows 保留名；
- 超长路径与超长 ZIP member；
- 大小写不敏感文件系统中的冲突；
- 多层目录的稳定排序；
- 转换输出与已有图片同名。

最终排序规则必须依赖 ZIP member 名称，而不是依赖文件系统遍历顺序。

是否包含原始 MP4 是需要实施前确认的产品决策：

- `include_original_mp4=true`：复现旧脚本，最终 ZIP 更大；
- `include_original_mp4=false`：只保留 WebP 和图片，更适合 LANraragi 展示。

### 12.11 最终 ZIP 与回归普通流水线

最终 ZIP 使用现有 generation/attempt 思路：

1. 写入 job 专属临时 ZIP；
2. 使用 `ZIP_STORED` 或明确配置的压缩策略；
3. 校验成员、CRC、文件大小和 SHA-1；
4. 再次检查 job lease、workflow row version 和 Manga artifact generation；
5. 原子提升到受控正式位置；
6. 在同一数据库事务中登记 Manga 最终 artifact；
7. workflow 标记 completed，job 标记 succeeded；
8. Manga 从 `special_processing` 迁移为 `downloaded`；
9. 写入事件，记录源资源、用户选择、生成文件和 generation；
10. 现有 validate worker重新验证正式 ZIP，再进入上传。

建议最终产物登记在 `prepared` 或专门明确允许的标准位置，但状态仍回到 `downloaded`，强制经过现有 validate，而不是直接跳到 `upload_pending`。

### 12.12 源资源清理

建议采用保守策略：

- 最终 ZIP 未校验和提升前，不删除任何源 torrent 或解压内容；
- 最终 ZIP 成功后，可以停止并删除两个特殊 qBittorrent 任务及其源文件；
- 如果希望保留到 LANraragi 上传成功，应增加明确的后续清理动作，不能指望普通 cleanup 理解特殊 hash；
- 任意清理必须检查精确 category、hash 和受控根目录；
- 清理重复执行应幂等；
- category 已被用户移走时不删除任务，转人工确认；
- 工作目录只删除当前 workflow/generation/job 对应路径。

实施前需在“组合成功即清理源文件”和“上传成功后再清理源文件”之间做最终选择。后者更保守，但需要 workflow 在普通上传完成后仍保留待清理资源，或增加一个特殊 cleanup job。

## 13. 配置变化

### 13.1 配置分层

特殊处理配置分为三层：

1. 通用调度配置：继续放在 `supervisor.toml`；
2. 模块专属非敏感配置：放在 `config/special/` 下与 kind 同名的独立 TOML；
3. 敏感信息：继续放在现有 `secrets.toml`，不得复制到模块配置或 workflow payload。

目录结构：

```text
config/
    supervisor.toml
    secrets.toml
    special/
        video_archive.sample.toml
        video_archive.toml
```

- `*.sample.toml` 作为模板提交到 Git；
- 实际模块配置 `*.toml` 为本地文件并由 Git 忽略；
- 文件名必须与 workflow kind 一致；
- worker 只加载自己 operation 所属 kind 的模块配置；
- Web、Supervisor 和 worker 必须使用同一个 config directory；
- registry 中已启用的模块缺少配置或配置无效时，不允许创建新 workflow，并在 Web 显示配置错误。

### 13.2 通用调度配置

Supervisor 只保存与所有特殊模块共同相关的调度参数：

```toml
[special_processing]
enabled = true
poll_seconds = 5
default_job_lease_seconds = 900
max_concurrency = 1

[modules]
special_processing = true
```

这些配置控制：

- 是否调度 special job；
- Supervisor 查询 queued job 的频率；
- 默认租约；
- 所有特殊 worker 的总并发；
- 整体组件暂停和恢复。

具体 kind 的 ffmpeg、目录、输出规则等不能放入 `supervisor.toml`。

### 13.3 视频模块配置

`config/special/video_archive.toml` 保存视频模块自己的非敏感执行参数：

```toml
enabled = true
auto_start = false

[download]
category = "eharchive-video-special"
external_root = "D:/special-video-download"
local_root = "D:/special-video-download"

[work]
workspace_root = "D:/special-video-work"
max_concurrency = 1

[ffmpeg]
executable = "D:/path/to/ffmpeg.exe"
max_workers = 2
quality = 75
compression_level = 6
file_timeout_seconds = 3600

[output]
include_original_mp4 = false
layout = "legacy_folders"
```

视频模块继续复用现有公共配置和敏感配置：

- qBittorrent URL：现有 AppConfig；
- qBittorrent 用户名和密码：现有 `secrets.toml`；
- EH Cookie、代理和 browse session：现有配置；
- 数据库、日志目录和时区：现有配置。

不得在模块 TOML 中重复保存账号、Cookie、代理密码、Authorization 或数据库密码。

`download.external_root` 是 qBittorrent 主机看到的绝对根路径，`download.local_root` 是 EH Archive 本机读取同一内容的绝对根路径。两者可以相同，也可以通过相对后缀安全映射。`work.workspace_root` 是本机转换和打包工作目录，不能与 qBittorrent 做种目录混用。

### 13.4 配置生效与 Workflow 参数快照

配置按影响范围分成两类。

需要重启 Supervisor 才生效：

- 特殊处理整体启用或禁用；
- 总并发和调度轮询；
- operation registry 或组件控制变化。

worker 每次启动时重新加载：

- 外部服务地址和敏感凭据；
- 当前超时和健康配置；
- ffmpeg 可执行文件位置；
- 当前本机和外部根路径。

会改变同一个工作流最终业务结果的参数，在创建 workflow 时复制到 `special_workflow.payload.config_snapshot`：

- 是否包含原始 MP4；
- 输出目录布局；
- WebP 质量和 compression level；
- 命名、排列和冲突处理规则；
- 其他影响最终产物内容的模块参数。

后续 retry 和 compose job 使用 workflow 快照，而不是静默使用已经改变的新配置，保证同一个 workflow 前后一致。密码、Cookie、token 和完整服务凭据不得进入快照，始终在 worker 执行时从 `secrets.toml` 读取。

### 13.5 模块启用、禁用与 Web 配置

- `enabled=false` 时不能创建新的该 kind workflow，也不能领取新的该 kind job；
- 已存在 workflow 不删除、不改状态，Web 仍可只读查看并提供受控退出；
- 模块重新启用后可以继续排队恢复；
- `auto_start` 属于每个 kind 的显式能力开关，默认必须为 `false`；
- 视频模块无论配置如何都不启用自动入口，`auto_start=false` 作为固定约束；
- Web 配置页面只开放 allowlist 中的非敏感字段；
- ffmpeg 路径、并发和质量可以开放；
- category、下载根目录、工作根目录和输出布局修改应显示明显警告；
- 配置更新必须先构造候选配置并完整校验，再原子替换文件。

所有本地目录必须是绝对路径。模块配置加载失败、ffmpeg 不可用或根目录健康检查失败时，Web 可以展示推荐模块，但必须禁用“进入特殊处理”按钮并说明原因。

## 14. 错误、恢复与人工处理

错误至少分为：

- 系统错误：数据库、qBittorrent、ffmpeg 不可用或配置错误；
- 临时错误：网络超时、torrent 页面暂时获取失败；
- 档案错误：候选失效、ZIP 损坏、文件冲突、视频转换失败；
- 用户等待：需要选择、需要确认风险、需要人工发起批量检查；
- 外部等待：qBittorrent 尚未完成。

“需要用户输入”和“外部下载未完成”不是失败，不应制造 traceback 或不断自动重试。

恢复规则：

- queued job 可在未启动前取消；
- running job 只能请求取消，worker 在安全点响应；
- 租约过期不自动接管；
- 提交 torrent 后崩溃必须 reconciliation；
- 转换中崩溃允许复用已验证输出；
- 打包中崩溃只留下 job 专属临时 ZIP；
- 最终提升前旧 worker必须通过 fencing；
- workflow failed 后由 Web 创建新的 retry job，历史 job 不覆盖；
- 用户退出特殊处理时必须明确是否清理外部任务和工作目录。

## 15. 日志、审计与健康检查

### 15.1 Event Log

至少记录：

- workflow 创建和退出；
- Web 加载候选请求；
- 候选快照更新；
- 用户选择及风险确认；
- 人工批量检查的发起来源、筛选数量、成功排队数量和跳过数量；
- job 排队、领取、成功、失败和人工解除；
- 每个 torrent 提交及 hash；
- 下载阶段变化；
- 转换开始、完成和失败摘要；
- 最终 ZIP 提升；
- Manga 返回正式状态；
- 外部任务与工作目录清理。

进度百分比不必每次写 event，只更新 progress snapshot。

### 15.2 日志文件

每个 special job 使用独立 session log，并关联：

- Supervisor run ID；
- workflow ID；
- job ID；
- manga ID；
- operation；
- PID。

Web 只展示清理后的错误摘要，不直接展示完整路径、私有 URL 或 ffmpeg 全量 stderr。

### 15.3 健康状态

可增加：

- ffmpeg 可执行与编码器健康；
- special download root 可读写和可用空间；
- special work root 可读写和可用空间；
- 视频特殊模块 paused/running 状态；
- 活动 workflow 数、queued/running/failed job 数。

## 16. 测试计划

### 16.1 数据库与并发

- 每个 Manga 只能有一个活动 workflow。
- 创建 workflow 与 Manga 状态迁移原子完成。
- 从 `manual_review` 进入失败时，workflow、Manga 状态、remark 摘要和事件全部回滚。
- 有活动普通 attempt、有效租约或活动 workflow 时拒绝进入特殊处理。
- “进入并加载”组合操作只能原子创建一个 workflow 和一个初始 queued job。
- 仅检测到推荐条件时不自动创建 workflow，也不自动修改 Manga 状态。
- 通用状态覆盖拒绝把 Manga 单独改为 `special_processing`。
- special job 使用 `SKIP LOCKED`，不会被两个 Supervisor 同时领取。
- lease token 不匹配时写回被拒绝。
- 旧 worker不能覆盖新 generation。
- stale Web `row_version` 请求被拒绝。
- 重复按钮请求不创建重复等价 queued job。

### 16.2 Web

- 只有允许状态可以进入特殊处理。
- 推荐模块只展示入口，不自动执行；用户点击后才进入。
- 模块被禁用、配置缺失或健康检查失败时，入口按钮禁用并显示原因。
- 特殊模块必须声明自己的入口状态和完成契约，修改或删除类模块不被强制要求生成最终产物。
- Torrent 候选正确显示风险标记。
- 必须选择两个不同候选。
- 无 Seeder、过时、重采样选择需要确认。
- Web 表单不包含私有 torrent URL。
- 不允许提交任意 operation。
- 不同 phase 只显示合法按钮。
- progress partial 只访问数据库。
- Remark 摘要只镜像 workflow 数据，不会创建、领取或取消 job。
- 更新 Remark 摘要保留用户人工备注，人工编辑也不能破坏系统摘要或反向改变 workflow。
- Remark 摘要缺失、被改动或显示滞后时，Supervisor 调度结果保持不变。
- 所有人工操作产生审计事件。

### 16.3 配置

- 模块 sample 配置可加载，实际配置缺失时给出明确错误。
- 模块文件名与 registry kind 不一致时拒绝启动。
- 本地路径必须绝对，外部根与本地根映射不能逃逸。
- 模块配置不能包含或输出敏感字段。
- Workflow 创建时正确保存影响业务结果的 `config_snapshot`。
- Retry 使用 workflow 快照，不受之后修改的质量、布局或保留 MP4 配置影响。
- Worker 每次执行从 secrets 读取当前凭据，不把凭据写入 payload、progress、remark 或 event。
- 禁用模块后不创建或领取新 job，已有 workflow 数据保持可查看。
- 视频模块始终拒绝自动入口。

### 16.4 Supervisor

- 无 queued job 时不启动空 worker。
- 到达 `next_run_at` 后才领取。
- 维护窗口和 paused 状态下不启动。
- 同 operation 和同 workflow 并发受限。
- 子进程退出码正确映射为 job 和组件结果。
- Supervisor 重启后 queued job 可继续，running 过期 job 不被自动接管。

### 16.5 Torrent 专用模块

- 活动、过时、红色时间、无 Seeder、重采样和视频候选均正确列出。
- 候选 stale 时拒绝提交并要求刷新。
- 图片成功、视频提交失败后重试不重复提交图片。
- 两个 hash 分别持久化，并只在人工批量检查或单档案检查时更新快照。
- 一个完成、一个未完成时保持 waiting。
- category 不匹配时不删除或读取产物。
- 用户选择同一 torrent 为两个角色时拒绝。
- 批量 dispatcher 只选择 active、`video_archive`、`downloading` workflow，不根据 Manga 状态单独推断。
- 已存在等价 queued/running job 时跳过，重复运行批量动作不产生重复活动 job。
- dispatcher 为每个 workflow 创建独立 job 后立即退出，不直接做文件处理。
- 两个下载未完成时检查 job 正常结束、保持 `downloading`，且不自动排下一次检查。
- 两个下载完成时同一 job 继续整合；一个 workflow 失败不阻断同批次其他 job。
- “立即检查这个档案”和批量入口创建相同 operation，只是选择范围不同。

### 16.6 解压、转换与打包

- ZIP traversal、盘符路径、符号链接和解压炸弹被拒绝。
- 嵌套目录和多余单层根目录行为确定。
- 只转换 MP4。
- 同名文件、大小写冲突和长路径有明确错误。
- ffmpeg 失败、超时、部分输出不生成正式 ZIP。
- 重试能跳过已验证 WebP。
- 最终 ZIP 排序确定、CRC 正确、SHA-1 正确。
- `include_original_mp4` 两种配置行为正确。
- 临时 ZIP 不会被普通 upload 领取。

### 16.7 回归普通流水线

- 特殊完成后 Manga 正确进入 `downloaded`。
- 现有 validate 对最终 ZIP 再次校验。
- upload 和 cleanup 不需要理解两个特殊 hash。
- 特殊失败不会被普通任务领取。
- 普通非视频档案行为和性能不变。

## 17. 实施阶段

### 阶段 1：通用持久化基础

- 增加 `special_processing` 状态和迁移。
- 增加 `special_workflow`、`special_job` 和约束索引。
- 实现 workflow/job repository、租约、fencing 和审计。
- 增加 `config/special/` 加载约定、kind 对应关系和参数快照规则。
- 完成数据库并发测试。

### 阶段 2：Supervisor 特殊任务调度

- 增加固定 registry。
- 增加 special job 领取、子进程启动和回收。
- 接入维护窗口、暂停、退出码和结构化日志。
- 建立无空任务启动和崩溃恢复测试。

### 阶段 3：Web 通用特殊处理框架

- 增加特殊工作流面板、状态、进度和事件显示。
- 增加创建、重试、取消、退出和过期租约解除。
- 增加自动推荐但人工确认进入的模块入口，禁止直接覆盖为 `special_processing`。
- 增加模块配置健康提示和禁用原因。
- 使用 HTMX partial 展示数据库快照。
- 完成权限、白名单和并发测试。

### 阶段 4：Torrent 候选与人工选择

- 实现 `video_archive` kind。
- 实现 torrent 候选加载 worker。
- 实现候选选择页面、警告和 stale 检查。
- 实现两个 torrent 的幂等提交。

### 阶段 5：人工批量检查与排队

- 实现特殊模块管理页和受控 CLI 的批量检查入口。
- 按 kind、status、phase 筛选，并为每个 workflow 幂等创建独立 `check_and_compose_if_ready` job。
- 保存两个 torrent 的最近快照和上次检查时间，Web 明确显示它们不是实时进度。
- 未完成的 workflow 保持 `downloading` 且不自动重排；已完成的 job 继续进入转换整合。
- 处理 hash 丢失、category 改变、单档案错误与取消，并确保同批次故障隔离。

### 阶段 6：视频转换整合

- 实现安全解压。
- 实现 ffmpeg 适配与断点恢复。
- 实现目录布局、确定性命名和最终 ZIP。
- 实现 generation/fencing 和标准产物登记。

### 阶段 7：回归、清理与运维

- 接回正常 validate/upload/cleanup。
- 完成特殊源资源清理策略。
- 增加健康检查、配置页面、部署说明和完整回归测试。

## 18. 实施前必须确认的产品决策

以下决策不影响通用框架，但会影响视频模块最终行为：

1. 最终 ZIP 是否包含原始 MP4。
2. WebP 放在图片前、图片后，还是按文件名映射插入。
3. 是否保留 `1_webp/2_pic/3_video` 目录结构。
4. 无 Seeder、过时、重采样 torrent 是否允许人工强制选择。
5. 批量检查入口同时提供 Web 模块管理页和受控 CLI，还是只提供其中一种；建议两者调用同一服务层。
6. 最终 ZIP 成功后立即清理源 torrent，还是等 LANraragi 上传成功后再清理。
7. 视频转换失败一个文件时是否整档失败，建议整档失败并人工复核。
8. ffmpeg 默认并发数、质量、超时和最大输出限制。
9. qBittorrent 远端根路径与本机共享目录的最终配置方式。
10. 下载快照在 Web 中的展示保留时间，以及人工检查 job 的历史保留和归档策略。

## 19. 完成定义

通用框架完成时应满足：

- Web 能创建白名单特殊流程和一次性 job。
- 程序能自动推荐适用模块，但只有用户明确授权后才原子进入 `special_processing`。
- 通用状态覆盖不能制造没有 workflow 的 `special_processing` 档案。
- Supervisor 能可靠领取、启动、回收和恢复 special worker。
- 用户等待和外部等待不需要常驻 worker。
- Web 能只通过数据库展示阶段、进度、错误和可用操作。
- 任务租约、外部副作用、旧 worker fencing 和事件审计完整。
- Manga 在特殊处理期间不会被普通流水线领取。
- 特殊模块不能形成任意命令执行入口。
- 每个模块使用独立非敏感配置，敏感凭据继续由现有 secrets 管理，影响最终结果的参数在 workflow 中固化。

视频整合模块完成时应满足：

- 用户能够在 Web 查看完整 torrent 候选并分别选择图片和视频。
- 两个 torrent 可以部分成功、长时间下载和独立恢复，不会重复提交。
- 用户能从模块管理页或受控 CLI 人工发起批量检查，系统为每个等待中的 workflow 独立排队。
- Web 能显示两个下载资源的最近进度和上次检查时间，并明确不是实时状态。
- 两个资源未完成时转换任务不会破坏状态或源文件。
- 两个资源完成后对应检查 job 能直接进入整合，单档案失败不阻断同批次其他档案。
- 解压、MP4 转 WebP、目录整理和打包均可恢复且受路径约束。
- 最终 ZIP 只有在完整校验和 fencing 成功后才登记。
- Manga 能从 `special_processing` 回到 `downloaded`，并由现有流水线完成校验、上传和清理。
- 普通档案的现有行为不发生变化。
