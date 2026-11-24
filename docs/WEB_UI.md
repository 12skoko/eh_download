# Web 界面说明

Web 控制台入口为 `/`，API 入口为 `/api/`。

页面模块位于 `src/eh_archive/web/`，模板位于 `templates/`，静态资源位于 `static/`。公共认证和渲染辅助函数位于 `web/shared.py`。

在 PowerShell 7 中先执行 `conda activate eh`，再执行 `python -m eh_archive.web.app --config-dir config`。实际监听地址以配置为准。已运行的服务需重启才会加载新的路由。

主界面涵盖概览、档案、待处理、工作流、事件、日志、配置和系统管理。修改界面时同时验证根路径、登录返回、表单提交和局部刷新。发布包须包含页面模板、静态资源和本地字体。
