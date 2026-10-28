# 验证记录

2026-10-26，本地 Windows 开发环境：

- 21 项 Python 单元与 HTTP/WebSocket 协议测试通过，覆盖鉴权、设备状态、配置导出、私有状态目录与健康检查。
- deploy/install.sh 和 deploy/pocketctl 通过 Bash 语法检查；安装脚本帮助入口可运行。
- 实际 Caddy 2.11.7 二进制通过生成的 Caddyfile 配置校验；官方发布包 SHA-512 校验通过。
- Python 源码编译检查通过。
- 固件基础版本此前已在实体板验证屏幕、按键及 Wi-Fi 配网页面；此云端包没有重新刷写设备。

尚未在腾讯云 Linux 上实际安装：systemd 隔离、Codex 登录和执行沙箱、证书签发、公网 WSS、云端中文识别延迟，需要部署后现场验证。安装脚本包含本机和公网健康检查，但这些检查不能替代一次真实按键录音与任务执行。

若 Codex 任务因 Linux 沙箱权限失败，先查看 sudo pocketctl logs；不要通过关闭审批或启用 unrestricted 模式绕过诊断。仓库默认工作区是服务器 /var/lib/codex-pocket/workspace。

2026-10-27：HTTP-01 与 TLS-ALPN-01 两种模式均通过实际 Caddy 2.11.7 配置校验。解析生成的 TLS-ALPN-01 配置，确认只监听 443/8448、关闭 HTTP 自动跳转、禁用 HTTP challenge；尚未在服务器签发证书。

2026-10-27：增加 private-ca 模式，Caddy 配置只监听所选设备端口，禁用 HTTP 自动跳转。实际启动独立本地 Caddy，使用隔离目录中的私有根证书验证 TLS 证书链和公网 IP SAN，未发送 DNS SNI，HTTPS 返回预期 404。未修改系统信任库。21 项软件测试通过；腾讯云及 ESP32 实体握手尚待部署验证。

2026-10-27：下载恢复改动通过 Bash 语法及 21 项软件测试。用已安装版本的模拟命令运行实际安装脚本第 2/3 步，确认复用 Node/Codex/Caddy，未调用 curl 或 npm。官方 PyPI 在腾讯云的实际速度需现场验证。

2026-10-27 Docker 版：本地 24 项软件测试通过，3 个入口脚本通过 Bash 语法检查。官方 Docker Compose 2.39.4 CLI（SHA-256 已校验）成功解析 compose.yaml，确认仅发布设备 8448 与回环管理端口、未挂载宿主机目录或 Docker socket，网关只读挂载公开 CA。Codex 0.163.0 生成的协议 schema 包含 externalSandbox 策略。本机无 Docker 引擎，不能声称本地镜像构建/容器联调已通过；新增 GitHub Actions 工作流构建镜像、检查非 root 依赖并在 Linux 镜像运行测试。真实账号登录、模型下载、任务执行和 ESP32 联调仍需服务器验证。

2026-10-27：修复 Compose tmpfs 流式列表中未引用逗号导致 size=256m 被解析为额外挂载项的问题。实际 Compose CLI 输出确认 gateway/ingress 各只有一个 /tmp 挂载。CI 增加真实 compose run 挂载与非 root /tmp 写入检查；尚未本地运行 Docker 引擎。

2026-10-27：修复私有卷 chown 后受限 root 缺少 FOWNER 而无法 chmod 的初始化错误，改由卷所有者 UID 10001 执行 chmod，不增加容器 capability。24 项本地软件测试和入口脚本语法检查通过。CI 新增真实入口 self-test，连续两次验证已有命名卷初始化、700 权限与非 root 写入。

2026-10-28：服务器日志确认 gateway 正常，ingress 在执行 Caddy 二进制时报 EPERM。核对官方 2.11.7-alpine Dockerfile，二进制带 cap_net_bind_service=ep，与运行时 cap_drop=ALL 冲突。派生镜像在构建时删除不需要的文件 capability，保留运行时全部 cap_drop 和 no-new-privileges。CI 增加在相同限制下执行版本、配置校验、真实入口私有 CA/TLS 404 握手检查。本地没有 Docker 引擎，尚未声称此容器运行测试已通过。
