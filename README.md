# Codex Pocket Cloud

Waveshare ESP32-C6-ePaper-1.54 的 Wi-Fi 语音控制器与个人云端 Codex 网关。

```text
ESP32（按键 / 麦克风 / 墨水屏）
    │ Wi-Fi + WSS
腾讯云新加坡服务器：Caddy → 网关 → Codex app-server
                              └→ 本地 Whisper 中文识别
```

服务器部署完成后，家里的电脑可以关机。默认是 **Codex 在你的腾讯云服务器上执行任务**，模型请求由 Codex 处理；这不会自动创建 OpenAI 托管的 Codex Cloud 任务。此仓库面向个人自用，使用你通过官方流程授权的 Codex 账户。

## 一键部署

支持 Ubuntu 22.04 / 24.04、Debian 12 / 13，x86_64 或 arm64。建议至少 2 核 4GB 内存、8GB 可用磁盘；小内存可选 `--model base`。Whisper small 模型约 484MB，需要服务器可访问 npm、GitHub、Hugging Face、OpenAI 和证书签发服务。

先将一个域名的 A 记录指向服务器公网 IP，并在腾讯云安全组与现有主机防火墙放行 **TCP 80、443**。保留现有 SSH 端口；脚本不会修改 SSH 或防火墙规则。

```bash
git clone https://github.com/castoryan-looper/codex-pocket-cloud.git codex-pocket-cloud
cd codex-pocket-cloud
sudo bash deploy/install.sh --domain pocket.example.com
```

可以把设备服务端口改为 8443，例如：

```bash
sudo bash deploy/install.sh --domain pocket.example.com --https-port 8443
```

此时放行 TCP **80 和 8443**，设备连接 `wss://pocket.example.com:8443/device`；导出的连接配置会包含所选端口。此模式通过 HTTP-01 自动申请与续期证书，公网 80 必须可达且可由此服务监听，443 可以留给已有服务。若 80 也被占用，需要复用现有反向代理或另行配置 DNS 验证；仅改设备端口无法解决证书验证端口问题。升级时继续传入相同的 `--https-port`。

也支持不使用域名，直接使用服务器公网 IPv4：

```bash
sudo bash deploy/install.sh --ip YOUR_PUBLIC_IP --https-port 8443
```

IP 模式使用 Let's Encrypt `shortlived` 公网证书，Caddy 自动续期；仍需放行 TCP **80 和 8443**，且 80 可由此服务监听。无需 DNS 解析。设备使用 `wss://YOUR_PUBLIC_IP:8443/device`，部署后导入新的连接配置。证书签发与 ESP32 的 IP 证书握手需要在实际部署后验证，配置校验通过不代表已经连通。公网只开放设备接口和健康检查；完整管理控制台仍通过 SSH 转发访问。

### 已有服务占用 80 端口

可选择 TLS-ALPN-01 证书验证，保留原有 Node / Nginx 网站：

```bash
sudo bash deploy/install.sh --ip YOUR_PUBLIC_IP --https-port 8448 --acme-challenge tls-alpn-01
```

此模式需要 **443 与 8448** 空闲并对公网开放，以及本机内部 8787 空闲。Caddy 会监听 443 与 8448，不监听 80；证书验证和续期使用 443，设备导出配置仍使用 8448。443 也会提供相同的已鉴权设备接口。若 443 也被其他服务占用，不要终止现有服务：需要先配置现有反向代理或提供自己的证书。升级时保留 `--acme-challenge tls-alpn-01` 和端口参数。

### 80 和 443 均被现有网站占用

可以使用私有 CA 模式，只占用指定的设备端口（以及本机内部 8787）：

```bash
sudo bash deploy/install.sh --ip YOUR_PUBLIC_IP --https-port 8448 --tls-mode private-ca
sudo pocketctl export /root/connection-private.json
```

只需放行 TCP **8448**。Caddy 在服务器生成私有 CA 并自动续期设备服务证书；安装程序只复制公开根证书，CA 私钥保留在服务私有目录。将 connection-private.json 通过可信的 SSH/SCP 下载并导入 ESP32 的受保护配网页面，其中包含公开根证书和设备密钥。设备按该根证书验证服务器，不关闭 TLS 校验。不要删除 `/var/lib/codex-pocket-tls`，否则根证书改变后需重新导入。升级时保留 `--tls-mode private-ca` 和端口参数。

私有证书不会自动被普通浏览器信任；使用 `sudo pocketctl health` 验证，它会显式加载本服务根证书。管理网页继续使用 SSH 转发到 127.0.0.1:8787，不需要在系统或浏览器安装 CA，也无需绕过浏览器证书警告。服务器上的 Node 网站继续使用原来的 80/443。

首次安装会显示官方 OpenAI 设备登录网址与一次性代码，由你在自己的浏览器登录。若账号尚未允许设备码登录，先在账号安全设置启用；也可按官方文档用 SSH 转发 `localhost:1455` 完成浏览器登录。不要把账号令牌、密码或 auth.json 提交到 Git。

无交互安装会在等待登录时退出码 2，随后运行：

```bash
sudo pocketctl login
sudo systemctl start codex-pocket-tls.service
sudo pocketctl health
```

安装脚本会：

- 安装 Node.js 22 LTS、固定协议基线 Codex CLI `0.163.0`、Python 依赖和中文字体。
- 从官方发布地址下载 Node/Caddy，并校验发布方提供的 SHA-256 / SHA-512。
- 下载本地 Whisper 模型，不把录音发给外部语音服务。
- 创建独立的低权限账户、私有运行目录和 systemd 开机自启服务。
- 自动申请与续期 Let's Encrypt 证书，只对公网代理 `/device` 与 `/healthz`。
- 将管理控制台保留在 `127.0.0.1:8787`，设备密钥单独生成并持久保存。
- 检查本机与公网就绪状态。公网未验证时以退出码 3 报告，不会把“本机启动”冒充“外网可用”。

如果 80、所选 HTTPS 端口或 8787 已由其他服务占用，首次安装会停止，不覆盖已有网站。TencentOS/CentOS 不支持此 apt 安装脚本；请使用受支持系统，或自行迁移部署配置。

## 将设备连到服务器

服务器运行：

```bash
sudo pocketctl export /home/ubuntu/connection.json
```

路径改成你自己的用户目录。文件按 0600 权限创建，sudo 用户会成为文件所有者；已存在文件不会被覆盖。用 SCP 下载到本机，勿上传到 Git。

ESP32 同时按住 BOOT + PWR 约 3 秒，连接屏幕显示的 `CPOCKET-…` 配置热点，在浏览器打开 `http://192.168.4.1/`。以屏幕 KEY 登录，导入 `connection.json`，填写 2.4GHz 热点并保存。设备会使用 `wss://你的域名/device`，无需 USB 任务桥接。

如果设备已连接局域网，也可以在其局域网 IP 打开配置网页。KEY 仅在设备物理屏幕显示；电脑网关不保存这个配置密码。

## 管理与升级

```bash
sudo pocketctl status
sudo pocketctl logs
sudo pocketctl restart
sudo pocketctl health
```

管理控制台通过 SSH 转发访问：

```bash
ssh -L 8787:127.0.0.1:8787 ubuntu@你的服务器
```

服务器运行 `sudo pocketctl console`，将输出的本机控制台链接复制到本机浏览器。该链接含私人访问令牌，不要分享。

升级：

```bash
git pull
sudo bash deploy/install.sh --domain pocket.example.com
```

运行数据在 `/var/lib/codex-pocket`，与源码发布目录分离。重跑部署保留登录、设备密钥、会话、模型和工作目录。旧发布保留在 `/opt/codex-pocket/releases` 便于人工回退；升级 Python 依赖后，回退代码时也需检查依赖兼容性。

## 设备功能

- PWR 短按选菜单，BOOT 短按执行。
- BOOT 按住达到设定门槛后开始录音，松开结束；默认门槛 1.2 秒，最长 20 秒。
- 语音转写先显示草稿，确认后才发送给 Codex。
- PWR 长按停止任务；配置模式下关闭配置热点。
- 网页可设置 Wi-Fi、云端地址/密钥/证书、录音参数和维护全刷间隔。
- Wi-Fi / USB 图标由设备本地更新。USB 仅烧录与启动日志，不承担任务或配网通信。
- 订阅剩余额度来自 Codex 真实窗口百分比；不是虚构的剩余 token 数。
- 电量是 ADC 电压估算；充电时可能偏高。

## 执行权限与边界

默认工作目录是服务器的 `/var/lib/codex-pocket/workspace`，使用 workspace-write 与 on-request 审批。任务不能访问你家电脑的本地项目，除非你另行同步或挂载项目。批准命令需要通过私有管理控制台；设备支持拒绝和停止。此版本不提供任意公网文件浏览或免审批准。

此项目并未实现 OpenAI 托管 `codex cloud exec --env ...` 任务适配器。如果需要该模式，需要另外配置 Codex Cloud 项目环境；不能把当前服务器上的 app-server 误认为官方托管环境。

## 固件编译

`firmware/` 包含已经在实体板验证过的源码与厂商驱动；本仓库不包含个人设备备份或预填凭据。Arduino ESP32 3.3.0，安装 ArduinoJson、WebSockets，OneButton 已在 firmware/libraries。

```bash
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.0 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install ArduinoJson WebSockets
arduino-cli compile --fqbn 'esp32:esp32:esp32c6:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB' --libraries firmware/libraries --output-dir build firmware/Pocket
```

Windows 也可使用 build.ps1。只更新应用到 0x10000 可保留 NVS；刷完整合并固件会清空设备配置。

## 开发测试

```bash
python -m venv .venv
.venv/bin/pip install -r bridge/requirements.txt
POCKET_FONT=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc .venv/bin/python -m unittest discover -s bridge -p 'test_*.py' -v
bash -n deploy/install.sh
bash -n deploy/pocketctl
```

安装脚本需要真实 Linux/systemd、域名和账户授权。本地静态检查和软件测试不能替代你服务器上的公网就绪验证。验证范围见 TESTING.md。

## 仓库分发

若收到的是 Git bundle：`git clone codex-pocket-cloud.bundle codex-pocket-cloud`。它包含完整 Git 提交记录；随后可设置你自己的远程仓库并推送，再在服务器正常 git clone / git pull。仅本地仓库包不等于已创建 GitHub 远程仓库。

## 官方参考与第三方代码

- [Codex headless authentication](https://learn.chatgpt.com/docs/auth)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Caddy TLS](https://caddyserver.com/docs/caddyfile/directives/tls)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)

保留第三方源码的各自许可证，见 THIRD_PARTY.md。Waveshare 上游没有顶层许可证，公开分发前应核实相关授权；个人部署不应公开任何运行凭据。

