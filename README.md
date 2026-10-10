# EH Archive 安装与运行

先安装 Conda，并准备 PostgreSQL 数据库、EH 账号 Cookie、启用 Web API 的 qBittorrent 和 LANraragi。

## 1. 安装环境

```bash
conda create -n eh python=3.11 -y
conda activate eh
python -m pip install -e .
```

已有 `eh` 环境时跳过创建命令。需要 aria2、旧 MySQL 迁移或开发工具时，按需安装：

```bash
python -m pip install -e ".[aria2]"
python -m pip install -e ".[migration]"
python -m pip install -e ".[dev]"
```

## 2. 配置与初始化

首次运行时复制示例配置：

```bash
mkdir -p config
cp config.sample/{app,supervisor,crawl,secrets}.toml config/
cp -r config.sample/special config/special
```

修改 `config/` 中的示例值，填写数据库连接、EH Cookie、qBittorrent 和 LANraragi 的地址与凭据、采集地址，并将日志和存储目录改为实际的绝对路径。数据库和用户需提前创建，存储目录需有读写权限。

默认大文件通过 SMB 上传，需要配置 SMB 地址和凭据；只使用 HTTP 上传时，在 `app.toml` 中设置 `upload_backend = "http"`。使用视频处理时，还需配置 ffmpeg 和相关路径。

生成 Web 登录密码哈希：

```bash
eharchive web-password
```

将输出填入 `config/secrets.toml` 的 `web_password_hash`，设置 `web_username`，并将 `web_secret` 替换为至少 32 个字符的随机字符串。

初始化数据库表并检查连接：

```bash
eharchive --config-dir config db upgrade
eharchive --config-dir config db ping
```

## 3. 直接运行

打开两个终端，都进入项目根目录。

终端一启动 Web：

```bash
conda activate eh
eharchive-web --config-dir config
```

终端二启动后台任务：

```bash
conda activate eh
eharchive-supervisor --config-dir config
```

浏览器打开 [http://127.0.0.1:8787](http://127.0.0.1:8787)，使用配置的用户名和密码登录。两个终端需保持运行，停止时分别按 `Ctrl+C`。以后启动时重复上述命令即可。

## 4. Linux 系统服务运行

也可以通过 systemd 在后台运行。需要 Linux、systemd、Git 和 root 权限；项目必须位于 Git 仓库中。先完成上面的环境安装、配置和数据库初始化，并停止手动运行的 Web 与 Supervisor。

在 root 的 Bash 终端中进入项目目录，激活已安装项目的 `eh` 环境，安装并启动服务：

```bash
conda activate eh
eharchive --config-dir config service install --start
```

安装会记录当前 Python 环境和项目的绝对路径，后续无需保持终端打开。常用管理命令（同样在 root 身份及已激活的 `eh` 环境中执行）：

```bash
eharchive service status
eharchive service stop all
eharchive service start all
eharchive service restart all
eharchive service logs web
eharchive service logs supervisor
```

Web 与 Supervisor 服务默认不会在机器重启后自动启动，重启后执行 `eharchive service start all`。
