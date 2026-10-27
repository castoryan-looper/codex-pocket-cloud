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

