# Web 界面说明

主界面入口为 `/`，原版备用界面入口为 `/v1/`。两套界面共用登录会话、数据库、业务服务和 `/api/` 接口；导航中的“进入 V1”和“进入主界面”可跳转到对应页面。

主界面页面模块位于 `src/eh_archive/web/`，模板位于 `templates/`，资源位于 `static/`；V1 页面模块、模板和专属资源分别位于 `web/v1/`、`templates/v1/`、`static/v1/`。公共认证和渲染辅助函数位于 `web/shared.py`。两套界面共用本地 HTMX 资源。

在 PowerShell 7 中先执行 `conda activate eh`，再执行 `python -m eh_archive.web.app --config-dir config`。实际监听地址以配置为准。已运行的服务需重启才会加载新的路由。

主界面涵盖概览、档案、待处理、工作流、事件、日志、配置和系统管理。修改界面时同时验证根路径、V1、登录返回、表单提交和局部刷新。发布包须包含两套模板、静态资源以及主界面的本地字体。
