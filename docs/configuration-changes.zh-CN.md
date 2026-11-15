# 配置变更与升级兼容约定

修改配置字段、默认值、校验规则、Web 配置页面或升级流程时，必须遵守本文档。完成标准是：现有配置能够经过预检、备份和迁移，原来运行的服务恢复并通过健康检查，原来停止的服务保持停止；其他用户设置和凭据得到保留。

## 配置文件的职责

| 位置 | 职责 | 修改约定 |
| --- | --- | --- |
| `config.sample/` | Git 跟踪的模板，同时参与默认值合并和字段结构校验 | 修改字段时同时检查加载、校验、迁移和升级兼容，不能只改示例 |
| `config/` | Git 忽略的用户实际配置 | 不用模板覆盖已有文件，不手工提高版本号绕过迁移；需要转换时使用现有备份迁移机制 |
| `/etc/eharchive/management.toml` | Linux 部署路径、Python 环境、服务管理等设置 | 与业务配置的版本分开处理，不能因业务字段变化重新安装或重建部署登记 |

`config_version` 是每个配置文件自己的结构版本，与程序发布版本无关。各文件当前支持版本以 [迁移步骤与版本登记](../src/eh_archive/management/config_migrations/steps.py) 为准。

## 修改字段时必须同时处理的事项

1. 明确新增、改名、移动、删除、类型或单位变化，以及默认值变化对已有用户的影响。新增可选字段如果无需转换旧数据，可以沿用补齐默认值的机制；需要转换已有结构或保留旧语义时，必须增加版本迁移。
2. 在对应配置文件的 `MIGRATIONS` 序列末尾追加步骤，并同步模板的 `config_version`。`CURRENT_VERSIONS` 由迁移序列推导，不另设不一致的版本值；已发布步骤保持固定，不能通过修改历史步骤改变旧版本的含义。
3. 迁移只转换明确涉及的字段，保留其他值、注释、账号、连接和自定义配置。默认值发生变化时，明确旧用户应保留原行为还是采用新行为，并通过迁移及文档表达决定。
4. 同步检查模板、[默认值合并](../src/eh_archive/config/defaults.py)、[配置加载器](../src/eh_archive/config/loader.py)、[结构校验](../src/eh_archive/config/validation.py)、[Web 配置页面](../src/eh_archive/web/configuration.py) 和使用文档。
5. 处理迁移前的读取：普通 CLI 和 Worker 不应因读取配置就改写文件。退役字段可按旧版本在内存中的有效配置里忽略，再由正式迁移备份并移除；新版本配置继续严格校验，不能放开全部未知字段。

尤其注意：结构校验会读取 `config.sample/`。从模板中删除一个字段，也可能立即使现有用户配置变成“未知配置项”。只删除模板字段、只删除 Web 输入项或只在新校验器里加白名单，都不能证明升级兼容。

## 必须覆盖仍在运行的旧升级进程

升级时切换 Git 提交并同步 Python 环境，不会替换升级器已经加载到内存里的函数。新启动的预检子进程可能已使用新代码，而原升级进程仍用旧加载器读取磁盘上的新模板。

因此，必须保持以下顺序：

1. 停止领取任务，等待需要停止的服务退场。
2. 切换代码、同步 Python 环境。
3. 在新进程和临时配置副本中预检迁移及新建结果；预检不发布实际配置。
4. 执行数据库升级。
5. 在新版本子进程中，使用现有配置迁移机制备份并发布实际配置。
6. 恢复原来运行的服务，检查 Web 健康状态和适用的 Supervisor 新心跳，再将操作标记为成功。

目前第 5 步由 [installer.refresh_after_update](../src/eh_archive/management/installer.py) 承担。旧升级器本来就会在数据库升级后调用该模块，因此过渡逻辑必须在旧升级器实际会进入的入口生效，不能只修改新版本父进程的方法。

Web / Supervisor 启动继续调用同一套幂等迁移，但不能单独依赖服务启动来处理字段退役：`systemctl start` 返回后，原升级器可能先于新服务完成迁移就再次读取配置。不能通过重试碰运气，也不能假定切换代码后旧进程已自动加载新校验器。

配置锁、临时预检、原文件备份、版本记录和原子发布复用 [现有迁移实现](../src/eh_archive/management/config_migrations/__init__.py)。数据库迁移开始前的失败与开始后的失败边界应保持明确；开始后不要擅自回退旧代码或旧配置来掩盖结果不确定的迁移。

## 验证与交付要求

至少覆盖以下情形：

- 无版本号及各受支持旧版本的实际配置能够读取、预检和顺序迁移；预检前后实际文件字节不变。
- 字段转换正确，维护时间、其他用户设置和注释保留，备份与原文件一致；再次执行迁移不重复改写或备份。
- 新版本仍拒绝未知字段和不合法配置；退役字段的兼容只作用于明确的旧版本。
- **加载历史版本的升级器、配置加载器和校验器，让它们在切换模板后继续执行，再由新版本子进程完成迁移，直到服务健康检查通过。** 不能用只运行新代码、只验证预检或替换掉整个 `start()` 的测试代替此项。
- 原来停止的服务保持停止；需要恢复的 Supervisor 验证新心跳及原控制状态。
- 迁移未完成或服务不健康时不能报告升级成功；关闭关键兼容步骤的负例应能复现对应故障。

相关本地测试包括 `tests/test_config_migrations.py`、`tests/test_legacy_maintenance_config.py`、`tests/test_upgrade_maintenance_transition.py`、`tests/test_management.py` 和 `tests/test_configuration_editor.py`。按项目约定，`tests/` 保持 Git 忽略，不得强制加入 Git。

在已初始化 Conda 的 PowerShell 7 中运行相关验证，例如：

```powershell
conda activate eh
$env:PYTHONUTF8 = '1'
python -B -m pytest -q -p no:cacheprovider tests/test_config_migrations.py tests/test_legacy_maintenance_config.py tests/test_upgrade_maintenance_transition.py tests/test_management.py tests/test_configuration_editor.py
```

交付时分别说明修改了哪些模板和迁移代码、何时会修改实际配置、配置备份位置、验证过的旧版本，以及测试使用的替身、跳过项和真实 Linux 验证情况。测试数量不能代替对升级关键路径的说明；本地代码完成也不能表述为服务器已经部署成功。

## 2026-10-28 维护字段退役案例

当时从 `config.sample/supervisor.toml` 删除 `maintenance_retry_seconds` 和 `maintenance_recovery_timeout_seconds`，但用户实际配置仍有这两个字段，最初导致升级预检拒绝配置。随后只在新校验器增加兼容，新预检进程虽然通过，原升级进程却仍持有旧校验器，在 `start_web` 后准备健康检查时再次报错。

最终处理是追加 `supervisor_v3_to_v4`：旧文件读取时忽略这两个字段，正式迁移备份并移除它们，保留 `maintenance_start` / `maintenance_end`；同时让新版本刷新子进程在恢复服务前发布迁移。跨版本测试保留旧升级器继续执行，并验证关闭该迁移步骤时能够复现原错误。

以后遇到配置字段变化，必须同时检查“旧配置如何迁移”和“仍在运行的旧升级进程如何完成这次升级”。
