# LANraragi 大文件直传目录测试报告与程序修改计划

测试日期：2026-08-31

## 结论

方案可行，生产实现确定使用 Python `smbprotocol` 提供的高层 `smbclient` 接口，不依赖主机挂载 SMB 共享。HTTP upload 与 filesystem upload 设计为两个平级、可替换的 backend，由独立 selector 按配置规则选择；repository、supervisor、任务状态机和任何一个 backend 都不需要知道另一个 backend 的实现。SFTP 和 Python SMB 客户端两种传输方式都已实际跑通，但 SFTP 只保留为本次验证记录，不作为主程序的备用 transport，也不在 SMB 失败时自动降级。filesystem backend 的推荐流程是：通过 SMB 把文件写成 LANraragi 不识别的临时扩展名，完成远端大小和完整 SHA-1 校验后，在同一共享目录内原子改名为最终压缩包名；随后轮询 Archive API，确认 Shinobu 已经入库，再通过 API 覆盖写入业务元数据并读回校验。

不要把大文件直接缓慢写入最终的 `.zip` 文件名，也不要为每个文件调用 `/api/shinobu/rescan`。

前述资源验证阶段只新增了测试脚本和本报告，没有修改主程序文件或项目数据库；后续主程序实施结果记录在文末。

## 测试环境和资源验证

| 资源 | 验证结果 |
| --- | --- |
| LANraragi 网页 | HTTP 200 |
| 网页管理员密码 | 登录后进入 `/index`；同一会话访问 `/config` 返回 200，标题为 `LANraragi - Admin Settings` |
| LANraragi API 令牌 | `GET /api/shinobu` 返回 `success=1`、`is_alive=1` |
| LANraragi 版本 | `0.9.81 Atomica` |
| SSH | 可以登录 `192.168.4.53` |
| 上传目录 | `/home/ubuntu/ptcache/lanraragi_temp/manga` 存在、可读、可写 |
| SMB | `\\192.168.4.32\PTCache\lanraragi_temp\manga` 可访问；专用账号的创建、写入、读取、rename、删除均通过 |
| 本地测试文件 | `C:\Users\12skoko\Desktop\temp` 中 4 个 ZIP 均可读 |
| 本地 API 文档 | `C:\Users\12skoko\Desktop\api-doc` 中 6 份 Markdown 均可读 |

密码和 API 令牌没有写入测试脚本或本报告。

## 实际测试结果

测试前 LANraragi 中有 1 个既有档案，该档案没有被修改。SFTP 和 SMB 测试结束后共有 5 个档案，其中新增以下四个：

| 文件 | 大小 | 完整文件 SHA-1 | LANraragi ID | 结果 |
| --- | ---: | --- | --- | --- |
| `[LFTN] こずえと静かな休憩.zip` | 15,368,794 | `ac14991e98176680a396f225b2063ea6262299ad` | `d11655575c14cb4de5d1d253d3d54ceeb0d76374` | 传输、远端校验、Shinobu 入库、恢复式元数据写入和读回均成功 |
| `(C106) [カドヤ (木戸可動)] 薄明に重ねて (ブルーアーカイブ) [DL版].zip` | 25,957,448 | `ef3607e7cf5d0607ab0a0c6602630410d7bc71bf` | `a2493a937141f790c8fd8885d3f932f4b5711dfa` | 修正后的完整端到端流程一次通过 |
| `(C108) [(蕪)すずしろふぁーむ (だいこん)] ケイと湯けむり温泉紀行 (ブルーアーカイブ) [DL版].zip` | 27,993,897 | `9459de4c394daac23f0b1607af1333680224fe89` | `86e293400c2aabaa55bdf4c38c8974af8cf13c96` | Python SMB 创建、写入、完整回读、rename、Shinobu 入库和元数据确认均成功 |
| `(C108) [色即絶句 (色)] 幽霊少女：続 (オリジナル) [DL版].zip` | 76,523,488 | `0894cf88d8602ace69787ec6250967a567ef3e07` | `13a872cc958663d80c527adcf062b8f4f1e2b436` | Python SMB 完整端到端流程成功 |

最终核对结果：

- Shinobu 仍为运行状态，最终核对 PID 为 111。
- 四个新增档案均可通过各自的 `/api/archives/{id}/metadata` 查询。
- 标题、标签和摘要的 API 写入结果与读回结果完全一致。
- 四个远端文件大小均与本地文件相同；SMB 测试的两个文件还完成了全文件 SHA-1 回读。
- 上传目录内没有遗留 `*.uploading` 或权限探针临时文件。
- 未调用 Shinobu stop、restart 或 rescan。
- 未调用档案删除 API；四个测试档案仍保留在临时 LANraragi 中。

## 关键发现：LANraragi ID 的精确算法

LANraragi 0.9.81 的档案 ID 是文件开头 **精确 512000 字节** 的 SHA-1：

```python
with path.open("rb") as stream:
    archive_id = hashlib.sha1(stream.read(512000)).hexdigest()
```

这里不是 `512 * 1024` 字节，也不是完整文件 SHA-1。本次第一次测试正是因为把它误写成 524288 字节而触发了安全中止；改成 512000 后，计算结果与 LANraragi 实际生成的 ID 完全一致。

项目现有的 `artifact_sha1` 是完整文件 SHA-1，应继续用于传输完整性校验；需要单独计算 LANraragi ID，不能复用 `artifact_sha1`。

官方实现可参考 LANraragi 的 `lib/LANraragi/Utils/Database.pm::compute_id`。Shinobu 对新文件的处理逻辑位于 `lib/Shinobu.pm::add_to_filemap`。

## 正确的生产流程

### 1. 前置检查

1. 验证本地源文件存在、是普通文件，并且大小与项目数据库中的 `artifact_size` 一致。
2. 验证 LANraragi API 可达、API 认证有效，并确认 `GET /api/shinobu` 返回 `success=1`、`is_alive=1`。
3. 建立配置的远端文件协议连接（当前推荐 Python SMB 客户端），验证远端目录存在且是目录，并验证所需权限。
4. 计算两个摘要：
   - 完整文件 SHA-1：用于传输完整性校验。
   - 前 512000 字节 SHA-1：作为预期 LANraragi archive ID。
5. 调用 `GET /api/archives/{expected_id}/metadata` 做重复和恢复检查。
6. 检查远端最终文件名及本次任务专属临时文件名是否存在。

### 2. 分段传输到临时文件

远端最终文件必须与源 artifact 保持原样：

- 文件内容逐字节一致。
- 最终文件名使用实际源路径的 basename，即 `source_path.name`，并应等于数据库中的 `artifact_filename`。
- 不根据 MangaInfo 标题重新生成文件名。
- 不清洗、不截断、不翻译、不改变大小写、不修改扩展名，也不进行 Unicode 归一化。
- 不自动追加 `(1)`、时间戳、随机字符串或 archive ID。
- 如果原文件名不被远端文件系统接受、超过远端文件名长度限制，或者远端已经存在无法确认为同一档案的同名文件，则停止上传并进入人工复核；上传阶段不得擅自改名。

在指定上传目录内使用如下临时文件名：

```text
.<最终文件名>.<attempt_id>.uploading
```

`.uploading` 不是 LANraragi 支持的档案扩展名，Shinobu 会跳过它。临时文件与最终文件在同一目录和同一文件系统，才能在完成后原子改名。

临时文件名只存在于传输期间。远端大小和完整 SHA-1 校验成功后，必须将其原子改名为与源文件 basename 完全一致的最终名称：

```text
.<源文件 basename>.<attempt_id>.uploading
                    |
                    | 同目录原子改名
                    v
<源文件 basename>
```

传输时应：

- 按固定块大小写入，例如 4 MiB。
- 定期记录已传输字节、总字节和速度。
- 定期续租当前任务 lease，防止超大文件传输期间任务被其他 worker 重领。
- 只清理由当前 `attempt_id` 创建的临时文件，不能清理其他任务的临时文件。

### 3. 远端完整性校验

临时文件关闭后，通过同一个远端文件协议重新读取文件并验证：

- 远端大小等于本地 `artifact_size`。
- 远端完整 SHA-1 等于项目现有的 `artifact_sha1`。

任何一项不匹配时，不得发布最终文件；删除本次 attempt 的临时文件并进入重试或人工检查。

### 4. 原子发布

完整性校验成功后，把临时文件在同一目录内原子改名为最终 `.zip` 文件名。只有这个瞬间 Shinobu 才应该看到受支持的档案扩展名。

不要直接把网络流写入最终 `.zip` 名称。LANraragi 0.9.81 的 Shinobu 只等待文件达到 512000 字节，最多等待约 5 秒；大文件仍在写入时就可能被提前读取和入库。

### 5. 等待 Shinobu 入库

原子发布后轮询：

```http
GET /api/archives/{expected_id}/metadata
```

- 入库前，本次环境返回 HTTP 400。
- 入库后返回 HTTP 200 和档案元数据。
- 使用有上限的轮询周期，例如每 2～3 秒一次，总超时 4～10 分钟。
- `/api/shinobu` 只能查询 watcher 状态，不能提供某个文件的入库事件。
- 不要对单个上传调用 `/api/shinobu/rescan`；它会清空 filemap 并触发全库重扫。

可以把 `GET /api/archives` 后按新增 ID、文件名和大小匹配作为诊断手段，但生产流程的主键应是按精确 512000 字节计算的 `expected_id`。

### 6. 写入并验证元数据

确认档案存在后调用：

```python
new_metadata = {
    "title": title,
    "tags": tagstr,
    "summary": summary,
}
response = requests.put(
    f"{raragi_url}/api/archives/{expected_id}/metadata",
    data=new_metadata,
    headers=raragi_auth,
    timeout=30,
)
response.raise_for_status()
```

元数据必须通过 `data=` 放入 `application/x-www-form-urlencoded` 请求体，不使用 URL 查询参数 `params=`，也不手工把 `tags` 拼接到 URL。`requests` 会自动对中文、空格、逗号、冒号等特殊字符进行表单编码；业务代码不要预先 URL 编码 `tagstr`，否则可能发生二次编码。虽然查询参数通常也会自动编码特殊字符，但超长标签会受到客户端、反向代理或服务端 URL 长度限制，还更容易出现在访问日志中，因此不适合作为元数据传输方式。

该接口会覆盖原有值，因此必须一次提供最终的标题、标签和摘要。成功条件是 HTTP 200 且 JSON 中 `success=1`。HTTP upload 和 filesystem upload 必须复用 `LANraragiApiGateway.update_metadata()` 的同一套请求体实现，避免不同 backend 对特殊字符和长标签产生不一致行为。

随后再次调用 GET metadata，并逐字段确认：

- `arcid == expected_id`
- `size == artifact_size`
- `title`、`tags`、`summary` 与期望值一致

只有读回确认成功后，才能在项目中写入 `lrr_archive_id` 并把任务状态推进到 `uploaded`。源文件的清理仍由现有 cleanup 阶段负责，上传确认前不要移动或删除源文件。

## 恢复和幂等策略

| 现场状态 | 处理方式 |
| --- | --- |
| 预期 ID 已存在，且大小和文件名匹配 | 视为已入库恢复；跳过再次传输，继续写入并验证元数据 |
| 预期 ID 已存在，但大小或文件名不匹配 | 标记 duplicate/review，不覆盖元数据 |
| 本次 attempt 的 `.uploading` 存在 | 校验后决定续传或删除并重传；不得盲目发布 |
| 最终文件存在但 API 暂时查不到 | 校验远端文件后继续轮询；不要复制第二份 |
| 原子改名完成后轮询超时 | 标记 unknown/review，保留远端最终文件和本地源文件，以预期 ID 恢复 |
| 元数据 PUT 返回 423/429/5xx | 只重试元数据阶段，不重新复制文件 |
| SMB/API 认证失败 | 作为系统配置错误停止该任务，不自动改用其他协议、路径或凭证 |
| 远端完整 SHA-1 不匹配 | 删除本 attempt 的临时文件，保留本地源文件并重试/检查 |

由于 LANraragi 只使用文件前 512000 字节作为 ID，两个文件如果具有完全相同的这段前缀，会被 LANraragi 视为同一档案。程序必须按重复档案处理，不能尝试用改名绕过。

## 主程序修改计划

以下是建议的最小改动方案；保留现有 `upload_pending -> uploading -> uploaded` 状态机，不新增数据库状态，也不需要数据库迁移。

### 1. 配置和依赖

修改：

- `pyproject.toml`
- `src/eh_archive/config/loader.py`
- `config/app.sample.toml`
- `config/secrets.sample.toml`

计划：

- 将 `smbprotocol` 加入正式依赖；代码导入其高层 `smbclient` 模块。
- 在普通配置中增加 `lanraragi_smb_server`、`lanraragi_smb_port`（默认 445）、`lanraragi_smb_share`、`lanraragi_smb_relative_dir`、`lanraragi_smb_connection_timeout_seconds`、`lanraragi_smb_encrypt` 和 `lanraragi_import_poll_timeout_seconds`。
- SMB 用户名和密码放入 secrets，例如 `[lanraragi_smb] username/password`；不得进入日志、任务错误详情或命令行。
- 默认要求 SMB signing；生产环境应优先启用 SMB3 encryption，并在部署前确认服务端支持。
- 主程序不引入 `paramiko`，也不实现 SFTP fallback。SMB 认证、权限或连接失败必须显式报错，防止任务静默写入另一个目标。
- 增加 `upload_backend = "http" | "filesystem" | "auto"`：`http` 强制全部走 HTTP，`filesystem` 强制全部走 filesystem，`auto` 才执行 selector 规则。
- 保留 `large_upload_threshold_bytes` 作为默认 selector 规则：`auto` 模式下，值为 0 时全部走 HTTP；大于 0 时，`artifact_size >= threshold` 选择 filesystem backend。该阈值不再传给 repository 或 supervisor。

### 2. 建立公共上传契约和 LANraragi API gateway

建议结构：

- `src/eh_archive/services/uploader/contracts.py`
- `src/eh_archive/services/uploader/lanraragi.py`

`contracts.py` 定义与具体上传方式无关的：

- `UploadRequest`：本地路径、basename、大小、完整 SHA-1、MangaInfo、attempt 上下文，以及进度、续租和取消回调。
- `UploadOutcome`：统一的 success/retry/review/system/unknown 等结果和 archive ID。
- `UploadBackend` Protocol：两个 backend 都实现相同的 `upload(request) -> UploadOutcome` 接口。

把现有 `UploadOutcome` 从 `lanraragi.py` 移到 `contracts.py`，避免 filesystem backend 导入 HTTP backend 的模块。`lanraragi.py` 保留为两个 backend 可以共同组合使用的 API gateway，而不是某一个 backend 的基类；共享 API gateway 属于依赖复用，不构成 backend 之间的依赖。

API gateway 增加：

- `shinobu_status()`：严格验证 `success=1` 和 `is_alive=1`。
- `update_metadata(archive_id, info)`：调用 PUT metadata 并分类 401/403、423/429、5xx 等结果。
- `confirm_metadata(...)`：GET 后验证 ID、大小和业务元数据。
- 现有 HTTP `/api/archives/upload` 调用可以保留为 gateway 的低层操作，由 HTTP backend 使用。

### 3. 实现两个平级 backend

建议新增：

- `src/eh_archive/services/uploader/http.py`：`HttpUploadBackend`
- `src/eh_archive/services/uploader/filesystem.py`：`FilesystemUploadBackend`
- `src/eh_archive/services/uploader/smb_store.py`：封装 Python SMB 文件操作

`HttpUploadBackend` 只负责现有 multipart HTTP 上传及确认。`FilesystemUploadBackend` 只负责 filesystem 导入流程，并组合 `smb_store` 和 LANraragi API gateway；它不得导入或调用 `HttpUploadBackend`，HTTP backend 也不得导入 filesystem/SMB 模块。二者只共同依赖 `contracts.py`，并可以共同使用注入的 API gateway。

filesystem backend 职责：

- 精确计算前 512000 字节 SHA-1。
- 进行 API、SMB、远端目录、权限、重复档案和远端文件预检。
- 通过 `smbclient` 写 attempt 专属 `.uploading` 文件，并在单次导入内复用同一个 SMB session。
- 提供进度回调和取消/失租中止点。
- 校验远端大小和完整 SHA-1。
- 使用 `smbclient.rename(..., replace_if_exists=False)` 做同目录原子改名，禁止覆盖既有最终文件。
- 轮询 metadata，更新元数据并读回确认。
- 返回公共契约定义的 success/retry/review/system/unknown 结果。
- 在成功、失败和取消路径中都关闭文件句柄，并显式清理/重置所用 SMB 连接，避免进程退出时遗留连接关闭告警。

两个 backend 都不直接修改项目数据库，也不自行选择上传方式。当前没有为多种远端文件协议预先设计抽象 transport 层；如果未来确实增加第二种生产文件协议，再从已经稳定的 SMB 文件操作中抽象。

### 4. 让大文件进入 upload worker

修改：`src/eh_archive/db/repository.py`

当前 `has_work()` 和 `claim_next()` 会过滤掉 `artifact_size >= large_upload_threshold_bytes` 的记录。实现新路径后必须移除这两处过滤，并从两个方法的参数中删除 `large_upload_threshold_bytes`。repository 只根据通用 upload 状态、retry 和 lease 条件领取任务，不读取 backend 配置，也不判断文件大小。

`src/eh_archive/supervisor/app.py` 也不再把阈值传给 `has_work()`；supervisor 只判断是否存在 upload 工作。这样修改筛选规则不会影响数据库查询或进程调度。

### 5. 使用独立 selector 选择 backend

建议新增：`src/eh_archive/services/uploader/selector.py`

selector 是无外部副作用的纯策略组件，输入 `UploadRequest` 中可筛选的属性和上传配置，输出 backend key。默认规则：

```python
if upload_backend == "http":
    return "lanraragi_http"
if upload_backend == "filesystem":
    return "lanraragi_filesystem"
if artifact_size >= large_upload_threshold_bytes > 0:
    return "lanraragi_filesystem"
return "lanraragi_http"
```

未来需要按扩展名、来源、目录或其他档案属性选择时，只修改/注入 selector 规则，不修改两个 backend、repository、supervisor、状态机或数据库。selector 不执行上传，不访问数据库，也不处理上传结果。

两种方式继续共用：

```text
job_attempt.operation = "upload"
upload_pending -> uploading -> uploaded
```

不要为了区分上传方式增加 `uploading_http`、`uploading_filesystem` 等主状态，也不要在通用的 `job_attempt` 表中增加上传专用的 `upload_method` 字段。

当前最小方案使用已有的 `job_attempt.detail` JSON 保存执行变体和内部阶段：

```json
{
  "variant": "lanraragi_filesystem",
  "phase": "transferring",
  "expected_archive_id": "a2493a937141f790c8fd8885d3f932f4b5711dfa",
  "remote_filename": "example.zip",
  "staging_filename": ".example.zip.123.uploading"
}
```

普通上传对应：

```json
{
  "variant": "lanraragi_http",
  "phase": "requesting"
}
```

字段职责：

| 位置 | 内容 |
| --- | --- |
| `job_attempt.operation` | 两种方式都为 `upload` |
| `job_attempt.detail.variant` | `lanraragi_http` 或 `lanraragi_filesystem` |
| `job_attempt.detail.phase` | 本次上传的内部阶段 |
| `job_attempt.external_task_id` | 预期或实际 LANraragi archive ID |
| `job_attempt.progress_*` | HTTP 或 SMB 上传进度 |
| `manga.lrr_archive_id` | 最终确认成功的 LANraragi archive ID |

文件系统上传建议使用以下内部阶段，但它们只属于 attempt detail，不进入 `manga.status`：

```text
preflight
transferring
verifying
published
waiting_for_shinobu
metadata_updating
confirming
confirmed
```

如果以后多个 operation 都出现大量实现变体，并且确实需要高频 SQL 筛选或建立索引，可以再考虑为 `job_attempt` 增加通用的 `variant` 字段。当前实现不需要该迁移。

### 6. runner 只负责编排公共契约

修改：`src/eh_archive/tasks/runner.py::_upload`

建议顺序：

1. 保留现有 MangaInfo 和 artifact 指纹校验。
2. 构造统一 `UploadRequest`，通过 selector 得到 backend key，再从显式 registry 取出对应 `UploadBackend`。
3. 在 `job_attempt.detail.variant` 记录 selector 的实际结果；该字段用于观测和恢复，不反向参与选择。
4. 在外部副作用前完成 fencing；filesystem backend 还应预先计算 archive ID，并把它写入当前 attempt 的 `external_task_id` 作为崩溃恢复键。
5. runner 只调用统一的 `backend.upload(request)`，不包含 HTTP multipart、SMB、staging、Shinobu 或元数据实现细节。
6. 两个 backend 都通过请求中的公共回调更新现有进度并周期性续租。
7. runner 使用同一套 outcome 分支处理 retry/review/system/unknown；只有 success 后才设置 `record.lrr_archive_id` 并 `finish(..., event="uploaded")`。
8. filesystem 已发布后发生未知错误时，不自动重传；先以 `external_task_id` 查询 LANraragi 并进入恢复分支。

### 7. 错误和日志

- 日志记录 attempt ID、manga ID、预期 archive ID、阶段、字节数和耗时。
- 不记录密码、私钥、Authorization header 或完整异常请求对象。
- 明确区分 `staging`、`published_waiting`、`metadata_updating`、`confirmed` 四个阶段。
- 对 SMB 连接、认证、权限、目标冲突、远端校验失败分别使用稳定的错误代码，便于判断可重试错误和配置错误。
- 远端最终文件一旦发布，错误默认归类为 `unknown/review`，不能像普通传输失败一样无条件重传。

### 8. 测试计划

单元测试至少覆盖：

- ID 使用 512000 字节，而不是 524288 字节。
- 小于 512000 字节的文件使用全部内容计算 ID。
- Unicode 和组合字符文件名。
- SMB 分段写入、连接复用、进度、取消和 lease 续租。
- SMB 认证失败、目录无权限、连接中断、同名目标冲突及 `replace_if_exists=False` 行为。
- 所有退出路径正确关闭文件句柄和 SMB 连接。
- 远端大小/SHA-1 不匹配时只清理本 attempt 的 staging。
- 最终文件已存在、预期 ID 已存在、元数据阶段恢复。
- Shinobu 超时、423/429、5xx、401/403 的分类。
- metadata PUT 后逐字段读回校验。
- `http`、`filesystem`、`auto` 三种 selector 模式，以及阈值边界和非法配置。
- selector 规则变化只改变 backend 选择，不改变 repository 的领取结果。
- 两个 backend 分别满足同一个 contract 测试，返回相同结构的 `UploadOutcome`。
- HTTP backend 不导入 filesystem/SMB 模块，filesystem backend 不导入 HTTP backend。
- runner 对两个 backend 使用同一个调用入口和 outcome 处理分支。
- SMB/filesystem 失败时不得转走 HTTP 或 SFTP；跨 backend fallback 必须是未来明确设计的策略，不能由 backend 自行决定。

保留一个默认跳过的手工集成测试，用环境变量或交互输入提供凭证，禁止硬编码真实凭证。

### 9. 推荐实施顺序和验收标准

实施顺序：

1. 增加公共 contracts、依赖、配置模型和 sample 配置，不写入真实凭证。
2. 整理 LANraragi API gateway，把现有 HTTP 上传迁入 `HttpUploadBackend`，先用回归测试保证行为不变。
3. 实现无数据库副作用的 `FilesystemUploadBackend`、SMB store 及模拟 SMB/API 测试。
4. 实现纯 selector 和 backend registry，覆盖强制选择、自动规则及阈值边界测试。
5. 删除 repository/supervisor 的大小过滤和阈值参数，让 runner 只通过公共 contract 调用选中的 backend。
6. 补齐崩溃恢复、错误分类、日志脱敏、连接清理及 backend 解耦测试。
7. 在临时 LANraragi/SMB 环境分别执行 HTTP 和 filesystem 手工集成测试。

验收时必须确认：只修改 `upload_backend` 或 selector 规则即可在 HTTP/filesystem 之间切换；repository、supervisor、状态机和 backend 实现不需要随筛选规则改变；两个 backend 不互相导入或调用；源文件内容和 basename 未被修改；远端完整 SHA-1 一致；LANraragi ID 与前 512000 字节 SHA-1 一致；元数据写入后读回一致；没有遗留 `.uploading` 文件；既有同名文件不会被覆盖；日志与数据库中没有 SMB 密码或 API 令牌；现有 HTTP 路径和测试没有回归。整个方案复用现有字段，因此不需要数据库迁移。

## 本次新增的测试工具

- `tests/manual_lanraragi_filesystem_import.py`：本次用于验证 SFTP 对照路径；仅保留为历史测试工具，不属于主程序实施计划。
- `tests/manual_lanraragi_smb_probe.py`：SMB 目录只读核对及创建、写入、读回、rename、删除权限探针。
- `tests/manual_lanraragi_smb_import.py`：Python SMB staging、全文件 SHA-1 回读、服务端 rename、Shinobu 轮询和元数据确认测试。
- `tests/manual_lanraragi_path_probe.py`：此前用于验证 Windows 映射盘可见性的只读探针。

注意：当前 `.gitignore` 忽略整个 `tests/` 目录，因此上述手工脚本目前只存在于本地工作区，不会出现在普通 `git status` 中。如果之后需要纳入版本控制，应先调整忽略规则或使用项目约定的其他测试工具目录。

## 主程序实施结果（2026-09-01）

本报告中的主程序方案已经实施，未新增数据库字段、状态或 migration，也没有提交或推送 Git。

已完成：

- 新增公共 `UploadRequest`、`UploadOutcome` 和 `UploadBackend` 契约。
- 将 LANraragi API 访问整理为共享 gateway；保留 `LANraragiClient` 兼容别名供健康检查、cleanup 和 delete 使用。
- HTTP 与 filesystem 是平级 backend；二者只共享契约与 API gateway，不互相导入或调用。
- selector 支持 `http`、`filesystem`、`auto`；repository 和 supervisor 已移除文件大小筛选。
- 两种 backend 都预先计算精确前 512000 字节 SHA-1，并写入当前 attempt 的 `external_task_id`；已存在且 size/filename 匹配时只恢复元数据阶段，不重新传文件。
- filesystem backend 通过 `smbprotocol` 1.17.0 的 `smbclient` 直接连接 SMB，固定要求 signing，可配置 SMB3 encryption，不依赖主机挂载。
- filesystem backend 已实现原 basename、attempt 专属 `.uploading`、4 MiB 分段写入、进度/租约回调、远端完整大小与 SHA-1 回读、同目录不覆盖 rename、Shinobu 轮询、元数据更新和读回确认。
- `smbclient.rename` 的公开函数内部固定构造 `replace_if_exists=False`，因此生产封装不传入一个会造成重复参数错误的同名 kwarg，但实际协议行为仍为禁止覆盖。
- 元数据 PUT 统一使用 `data=` 表单请求体；同一份 title/tags 映射用于 PUT 和读回确认，避免 `date_added` 在跨秒调用时不一致。
- 当前 `MangaInfo` 没有 summary 字段，因此主程序不会凭空生成或清空 summary，只严格写入和确认现有业务字段 title/tags；gateway 的底层表单机制可以在以后有正式 summary 来源时扩展。
- `job_attempt.operation` 仍为 `upload`；`detail.variant` 和 `detail.phase` 记录 backend 与内部阶段；`external_task_id` 保存恢复 ID。
- 配置模板、Web 配置页面和中文使用文档已同步更新，真实凭据没有写入仓库文件。

验证结果：

- `python -m pytest -q`：254 passed，只有一条既有的 Starlette/httpx2 deprecation warning。
- `python -m ruff check ...`：通过。
- `python -m compileall -q src`：通过。
- 新增单元测试覆盖 512000/524288 边界、短文件、Unicode/组合字符 basename、源字节保持、SMB session/签名/encryption 参数、staging 清理隔离、租约丢失、完整性失败、最终名称冲突、HTTP/filesystem 恢复、表单元数据长标签、selector 边界、backend 解耦、runner 公共入口以及 repository 领取大文件。

当前本机实际 `config/app.toml` 尚未填写新的 SMB server/share/relative_dir，`config/secrets.toml` 也尚未填写 `[lanraragi_smb]`。程序不会自行使用报告或对话中的凭据，也不会 fallback 到 SFTP/HTTP；在启动 filesystem/auto 大文件上传前，必须先按 sample 配置这些值。本轮没有重复向临时 LANraragi 写入新档案，外部端到端依据本报告前半部分已经完成的四个实际导入测试。

单元测试新增在 `tests/test_upload_backends.py` 和 `tests/test_upload_repository_selector.py`。项目当前仍忽略整个 `tests/` 目录，所以它们可在本地运行，但普通 `git status` 不会列出；如果需要随代码提交，需按项目决定调整 `.gitignore` 或使用 `git add -f`。

## 参考接口

- [Shinobu API](https://sugoi.gitbook.io/lanraragi/api-documentation/shinobu-api)
- [Archive API](https://sugoi.gitbook.io/lanraragi/api-documentation/archive-api)
- [LANraragi 官方源码](https://github.com/Difegue/LANraragi)
