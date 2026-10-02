# Web v2 使用与迁移说明

迁移日期：2026-10-02。Web v2 已从 `ehtemp/eh_download` 合入本项目，迁移基线为 `70bf77d`（6.4.10.3）。

## 访问与启动

新界面入口为 `/uiv2/`，旧界面入口仍为 `/`，共用现有认证和业务服务。

在 PowerShell 7 中启动本项目源码：

```powershell
conda activate eh
Set-Location 'D:\F\program\program\python\ehentai'
$env:PYTHONPATH = "$PWD\src"
python -m eh_archive.web.app --config-dir config
```

默认访问 `http://127.0.0.1:8787/uiv2/`；实际地址以现有配置为准。已运行的 Web 服务需要重启后才会加载新代码。

## 功能与源码

新界面提供概览、待处理、档案列表与详情、工作流、事件、日志、配置和系统管理。工作流覆盖视频整合、手动种子、全量采集、元数据更新、数据库核对和下载清理，复用现有业务处理器及权限、CSRF、版本和租约校验。

- `src/eh_archive/web/uiv2/`：新界面路由和原处理器适配。
- `src/eh_archive/web/templates/uiv2/`：页面与局部模板。
- `src/eh_archive/web/static/uiv2/`：样式、交互脚本、图标和本地字体。
- `src/eh_archive/web/app.py`：注册新界面并处理登录跳转。
- `src/eh_archive/web/services.py`：待处理列表的“未记录原因”筛选和统计。
- `pyproject.toml`：包含新界面静态资源及字体的打包规则。

此次迁移新增 64 个文件并更新 3 个文件，未引入新的依赖或数据库迁移，也未覆盖本项目的运行配置、数据库、日志和下载数据。界面使用本地资源，无需 Node 构建或外部 CDN。

## 验收与维护

后续修改应从本项目源码继续。检查新旧界面入口、登录跳转、页面操作、局部刷新、桌面与窄屏以及浅色与深色主题；发布包应包含上述静态资源和字体。

隔离 SQLite 和模拟管理对象的验证只能覆盖页面与本地交互；真实下载、LANraragi 远端写入以及 Linux 服务启停仍需在实际环境验收。原 Web v2 回归工具位于 `ehtemp/eh_download/tests/uiv2_tools/`，测试资料应保持为本地文件，不加入 Git。
