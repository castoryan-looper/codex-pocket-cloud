# Docker 部署

宿主机只需已安装的 Docker Engine 与 Compose v2；脚本不安装宿主机 Node、Python、Caddy，不修改 pip、SSH、80/443 或系统信任库。当前支持 Linux Docker，x86_64 / arm64。Docker 本身仍是宿主机依赖；已有的原生安装依赖不会自动卸载。

## 首次部署

Ubuntu 24.04 如果出现 `docker: unknown command: docker compose`，说明缺少 Compose 插件。保留已有 Docker Engine，仅安装 Ubuntu 仓库中的插件：

```bash
sudo apt-get update
sudo apt-get install -y docker-compose-v2
docker compose version
```

若使用的是 Docker 官方 APT 仓库，对应包名为 `docker-compose-plugin`。脚本也支持独立的 `docker-compose` v2+，但不支持旧 Python 版 v1。Compose 是宿主机必需的 Docker 管理工具；Node/Python/Caddy 仍只装在镜像内。

```bash
git pull
bash deploy/docker.sh up --ip 43.160.215.253 --https-port 8448
```

如果当前账号无权访问 Docker，使用 `sudo bash deploy/docker.sh ...`。先用 `docker info` 与 `docker compose version` 确认 Docker 可用；脚本不会自动安装或修改 Docker。首次执行构建镜像、准备持久模型、检查登录并引导官方设备码登录，然后启动网关与证书服务。非交互运行等待登录时退出码 2，随后在交互终端执行 `bash deploy/docker.sh login`，再执行 `bash deploy/docker.sh up`。

默认 Whisper small；可加 `--model base` 减少模型下载及内存需求，通常会降低中文识别准确度。Docker 隔离依赖，不会消除第一次镜像、软件包和模型下载；构建层与模型卷可以在后续部署复用。

腾讯云安全组和主机现有防火墙放行 **TCP 8448**。现有 Node 服务继续使用 80/443。

若原生版本已启动并占用 8448/8787，先确认它确实属于本项目，再执行 `sudo systemctl stop codex-pocket.service codex-pocket-tls.service`；不要停止现有 Node 服务。也可以选择其他空闲端口，例如 `--https-port 8449 --console-port 8788`。Docker 版本使用独立数据卷，不自动迁移原生账号、任务、证书或模型；原生数据保留原处。

## 设备配置与管理

```bash
bash deploy/docker.sh status
bash deploy/docker.sh health
bash deploy/docker.sh export ./connection-private.json
```

健康检查验证容器内的证书链、IP 与网关状态，不代表安全组已经放行公网。将连接 JSON 通过 SSH/SCP 下载到电脑，导入 ESP32 受保护配网页面。该文件包含服务的公开根证书和设备密钥，设备使用 `wss://43.160.215.253:8448/device`，正常执行 TLS 验证。

私有 CA 不自动受普通浏览器信任。无需在系统或浏览器安装根证书，也不要跳过浏览器证书警告。管理控制台只映射到宿主机回环接口：

```bash
# 在自己的电脑终端执行，端口与 --console-port 保持一致
ssh -L 8787:127.0.0.1:8787 root@43.160.215.253
# 在服务器终端显示带本地授权片段的控制台网址
bash deploy/docker.sh console
```

在电脑浏览器打开返回的 `http://127.0.0.1:8787/#...`。授权片段不要公开或贴入日志。公网 Caddy 仅提供 `/device` 与 `/healthz`，不提供管理网页。

## 日常操作

```bash
bash deploy/docker.sh logs        # 查看日志，Ctrl+C 退出查看
bash deploy/docker.sh stop        # 停止服务，保留数据
git pull
bash deploy/docker.sh up          # 复用 .env.docker 中的地址、端口、模型
```

`up` 会同时重建共享网络命名空间的两个容器。不要单独删除重建 gateway 后继续使用原来的 ingress 容器；需要重建时执行此脚本。Compose 项目名固定为 `codex-pocket`，一个 Docker 主机默认部署一个实例。

持久化命名卷：

| 卷 | 内容 |
| --- | --- |
| gateway_state | 官方 Codex 登录凭据、会话、设备密钥 |
| workspace | AI 工作文件、AGENTS.md |
| models | Whisper 模型和下载缓存 |
| caddy_data / caddy_config | CA 私钥、证书、Caddy 状态 |
| public_ca | 供设备导出使用的公开根证书 |

重建容器保留卷。**不要执行 `docker compose down -v`**，这会删除上述数据。迁移需要备份这些卷；只备份源码不能恢复账号、任务或证书。根证书变化后，设备需重新导入配置。

## 执行范围

网关以 UID 10001 运行。容器根文件系统只读，AI 在 workspace 卷工作，不挂载宿主机目录或 Docker socket，不使用 privileged、host 网络或 SYS_ADMIN。网关和 Caddy 共享专用网络命名空间，网关仅监听容器回环 8787，管理转发端口只发布到宿主机 127.0.0.1；外部只发布设备 TLS 端口。

Docker 版通过官方 `externalSandbox` 策略使用容器作为执行边界，避免依赖容器内嵌套 Linux 沙箱；任务可以使用容器网络、访问挂载给该容器的账号和工作数据。保留 `on-request` 审批处理，收到的审批不会自动同意。原生版本仍默认 `workspace-write`。参考 [Codex App Server](https://developers.openai.com/codex/app-server)。

验证范围见 TESTING.md。构建镜像不等于已完成 Codex 账号授权、真实任务执行和 ESP32 联调。
