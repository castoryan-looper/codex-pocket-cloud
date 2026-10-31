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

推荐使用 **[Docker 部署](DOCKER.md)**，宿主机只需 Docker Engine + Compose v2，隔离 Node/Python/Caddy 依赖，保留现有 80/443 网站：

```bash
git pull
bash deploy/docker.sh up --ip 43.160.215.253 --https-port 8448
bash deploy/docker.sh export ./connection-private.json
```

以下为保留的原生 systemd 安装方式。

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

固件 0.9.0 开始支持 [最多 8 组 Wi-Fi 记忆与自动重连](WIFI_MEMORY.md)。已有的一组会自动迁移；页面可选择或忘记网络，已保存网络密码留空会保留。

浏览器 USB 配置页面位于 `web/`，使用 Chrome 或 Edge 通过 Web Serial 直接访问设备，电脑无需安装助手。页面必须通过浏览器信任的 HTTPS（或 localhost）访问；现有 8448 私有 CA 证书在未配置的电脑上不自动受信任，不能把该地址当作通用入口。浏览器需要用户点击连接并选择串口；设备重启后可能需要重新连接。配置内容通过 USB 传给设备，不向网页服务器提交。设备固件需要支持 USB 配置 RPC（0.4.0 及以后）。

配置入口优先使用 USB 主机数据连接：用数据线连接电脑后，在本仓库双击 `USB配置.cmd`，保持工具运行，浏览器自动打开 `http://127.0.0.1:8788/`。本机网页服务由工具提供，不能只插线就直接访问此地址。首次运行需要 Python 3 及联网安装 pyserial；关闭其他占用串口的工具。USB 配置不要求云端或设备 Wi-Fi 已连通。

Windows 可双击 `启用USB自动配置.cmd`，为当前用户安装登录自启动助手。登录后助手在后台等待，通过 USB 数据线发现并确认 Codex Pocket 时自动打开配置网页；设备重插后再次打开。仅充电不会触发；配置页面默认地址仍为 127.0.0.1:8788，8787 是云端管理控制台。停用自启动可从 Windows 启动文件夹删除 `Codex Pocket USB Config` 快捷方式；当前后台进程需注销或结束后停止。移动仓库目录后需重新运行安装脚本。

检测到 USB 主机时，固件关闭配置热点并优先使用 USB 配置；充电器和仅充电线缆不会触发该优先级。无 USB 主机时，双键长按打开配置热点；未配网或 Wi-Fi 连接失败约 30 秒后也会自动开放热点。手动请求的热点被 USB 接入关闭后，拔掉数据线会重新开放。此优先级只影响配置入口，语音和任务仍通过 Wi-Fi 连接云端。

服务器运行：

```bash
sudo pocketctl export /home/ubuntu/connection.json
```

路径改成你自己的用户目录。文件按 0600 权限创建，sudo 用户会成为文件所有者；已存在文件不会被覆盖。用 SCP 下载到本机，勿上传到 Git。

USB 网页中导入 `connection.json`，填写 2.4GHz 热点并保存。使用配置热点时，先断开 USB 主机数据连接（可用充电器供电），同时按住 ESP32 的 BOOT + PWR 约 3 秒，连接屏幕显示的 `CPOCKET-…` 热点，在浏览器打开 `http://192.168.4.1/`。以屏幕 KEY 登录，导入同一文件并保存。设备会使用 `wss://你的域名/device`。

如果设备已连接局域网，也可以在其局域网 IP 打开配置网页。KEY 仅在设备物理屏幕显示；电脑网关不保存这个配置密码。

## 管理与升级

需要 main 更新后自动重新部署时，见 [main 自动部署](AUTODEPLOY.md)：服务器每分钟主动拉取，构建及测试通过后切换，健康检查失败自动回滚；无需从外部 SSH 登录服务器。

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

## 默认待机仪表台

待命页显示当地时间、Codex 订阅窗口的真实剩余百分比、城市天气和一条近期话题任务推荐。PWR 短按切换下一条，BOOT 短按查看详情，PWR 长按进入设备状态页。上下文剩余 token 可在设备状态页查看。订阅额度不能准确换算为固定 token 数；未知或过期数据显示未知，不虚构数值。任务进行中、待确认或有未发送草稿时不会切走重要状态；任务完成或停止 3 分钟后返回待机页。

固件 0.7.0 与同步更新的网关会自动按设备当前 Wi-Fi 的出口 IP 定位城市和时区，待机页显示城市名、当地时间、温度、天气描述与 Codex 终端标识。定位 HTTPS 请求由 ESP32 直接发给 ipwho.is，不会定位到云服务器；随后网关将大致经纬度发给 Open-Meteo 获取天气。不会发送 Wi-Fi 密码或 BSSID。更换 Wi-Fi、网关或设备 IP 时会立即清除旧位置，平时约 30 分钟更新，暂时失败约 2 分钟后重试；位置和天气超过 90 分钟不再作为当前数据展示。定位失败时显示未知，不回退到固定的广州或服务器位置。IP 定位反映网络出口的大致城市，手机热点、运营商出口、VPN 会影响精度；城市名使用定位服务返回的名称。当地时间使用 IANA 时区，缺少时区库时回退到定位服务的 UTC 偏移。建议由本地规则生成，不额外调用模型，也不会自动执行任务。

旧固件仍可使用显式经纬度 `POCKET_WEATHER_LAT` / `POCKET_WEATHER_LON`（Docker 首次部署使用 `--weather-lat LAT --weather-lon LON`）以及 `POCKET_UTC_OFFSET`。定位功能需要固件和网关一并更新。

0.8.0 的推荐以本设备云端会话最近 7 天、最多 12 轮用户消息为依据，提供推进、检查、理解、实验四种角度，A/B 尽量兼顾话题和角度的差异。没有足够聊天时会明确给出起步建议，不编造兴趣。选择与跳过记录保存在私有 runtime/recommendation-feedback.json，影响后续排序与去重；不会保存聊天原文到反馈文件，也不会因为浏览/换组就执行任务。原 /api/dashboard-context 接口保留兼容，外部日程/待办不会覆盖当前的聊天 A/B 卡片。其他电脑的桌面聊天尚未自动同步。完整交互见 [推荐与分页阅读](RECOMMENDATIONS.md)。

## 设备功能

- PWR 短按选菜单，BOOT 短按执行。
- BOOT 按住 1 秒开始录音，松开立即停止；最长 20 秒。录音状态与秒数在设备本地绘制，网络处理不会阻塞按键轮询。
- 语音识别完成后自动发给 Codex；任务进行中时作为补充指令发送。
- 录音中按 PWR 取消；双键按住 3 秒进入配网。空闲时 PWR 长按停止任务，配置模式下关闭配置热点。
- 网页可设置 Wi-Fi、云端地址/密钥/证书、录音参数和维护全刷间隔。
- Wi-Fi / USB 图标由设备本地更新。USB 可用于烧录、调试日志及本地网页配置；任务与语音仍只通过 Wi-Fi。
- 订阅剩余额度来自 Codex 真实窗口百分比；不是虚构的剩余 token 数。
- 电量是 ADC 电压估算；充电时可能偏高。

## 执行权限与边界

硬件会话的执行指令和事件状态模型见 [Pocket Harness](HARNESS.md)。它不覆盖持久工作区中的用户 `AGENTS.md`；`completed` 是 Codex 轮次状态，验证情况不会根据回复文本自动推断。

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

## 下载缓慢或 PyPI 镜像哈希错误

Python 依赖默认显式使用官方 `https://pypi.org/simple`，仅对安装进程忽略系统预配置的镜像，不修改系统 pip 配置或关闭 TLS/哈希校验。内网镜像出现哈希不一致时，更新仓库并重跑即可改用官方源；具体提速效果取决于服务器网络。重跑会复用已安装的 Node.js 22、相同版本 Codex CLI、已有稳定版 Caddy，避免反复下载。可显式设置 CADDY_VERSION 选择新版本。

若官方源仍慢，可以指定经过确认的 HTTPS PyPI 源：`sudo env POCKET_PIP_INDEX_URL=https://YOUR_TRUSTED_INDEX/simple bash deploy/install.sh ...`。该变量不包含账号凭据时才适合直接写入命令。不要用关闭哈希或证书校验的方法跳过失败。

语音模型尚未下载时，`--model base` 可减少模型下载量和内存占用，但通常会降低中文识别准确度；默认仍为 small。

固件 0.3.0 提供本地按住提示、录音计时、上传/识别和异常页面，顶部明确显示云端在线/离线。墨水屏存在物理刷新时间，不能保证像 LCD 一样瞬时变化；录音控制不等待屏幕或云端完成刷新。建议网关同步更新，使用结构化语音状态及时切回识别结果；旧网关保留兼容回退。

固件 0.3.3 将录音暂存在设备 FAT 分区，松开 BOOT 后分块通过 Wi-Fi 上传并显示进度，避免网络发送停顿挤满实时录音队列。按住满 1 秒开始，松开停止；PWR 长按取消。缓存只保留当前一段（最长 20 秒），上传结束、取消、失败或设备重启时删除；不恢复未提交录音。只自动初始化完全擦除的 FAT 分区，不格式化已有文件系统。无需修改云端音频协议。


## USB 配置与实时设备状态（固件 0.4.0）

Windows 接上 USB 数据线后，双击仓库中的 `USB配置.cmd`，会自动打开本机配置网页；默认地址为 `http://127.0.0.1:8788/`。第一次会在本项目的独立环境安装 pyserial，不修改全局 Python。保持工具运行，即可拔插设备并自动重连，无需把电脑切到设备热点，也不需要云端正常才能配网。关闭工具后，下次重新双击启动；重复启动会打开已有页面。

页面每 2 秒读取设备的真实状态：当前及已保存 Wi-Fi、设备 IP、信号强度、外网验证、云端连接、USB、固件/芯片/Flash/内存、电量估算、录音缓存及上传进度。外网“已验证”表示设备实际连接上云端加密通道；仅关联 Wi-Fi 时显示“待验证”，不把 Wi-Fi 图标当作外网连通。修改配置后点“保存并重启设备”，同一 Wi-Fi 密码及已有设备密钥留空会保留；可直接导入云端连接 JSON。

USB 通道仅支持读取状态和设备配置、保存配置及关闭配置热点，不提供任务指令或语音转发。网页只监听电脑回环地址，校验本机访问凭据、Host 与 Origin；凭据由本机页面自动初始化。设备密码/密钥不会通过查询接口返回，也不写入调试日志。Wi-Fi 配网页面仍可通过双键长按进入，并要求屏幕上的 KEY。

测试：`python -m unittest discover -s tools -p test_usb_config.py`。Linux/macOS 可在安装 pyserial 后运行 `python tools/usb_config.py --open`；Windows 双击入口为主要验证路径。

固件 0.5.2 支持在设备或 USB 配置网页扫描并选择附近的 2.4 GHz Wi-Fi，隐藏网络仍可手动输入。扫描期间设备会短暂断开 Wi-Fi 并在完成后恢复；设备主屏会显示当前连接的 Wi-Fi 名称，网页保存时为包含中文的名称生成屏幕字形。USB 配置长消息分段发送，保存期间显示进度提示。

服务器更新后，设备与服务器会协商 8 kHz G.711 μ-law 语音编码，音频数据约为原始 16 kHz、16 位 PCM 的四分之一；这是一种有损语音编码，实际识别效果需要现场验证。旧服务器未公布该能力时仍使用原格式。上传过程中断后会保留设备缓存，最多自动重试 3 次，并显示重试次数；取消、完成、最终失败或重启时清除缓存。识别后的文字会自动提交；只有发送失败时保留文字供处理。Codex 的权限确认仍按现有规则执行。

## 硬件会话无法恢复

当 gateway 日志明确显示某个会话 `no rollout found` 时，可运行 `bash deploy/recover-session.sh <日志中的会话ID>`。工具先停止网关，确认配置中的 ID 仍与日志一致，并检查 Codex 的 sessions 和 archived_sessions 中是否有相应记录；如果发现记录或 ID 不一致，停止并保留所有数据。确认无记录后，工具将 settings.json 以 600 权限备份在同一状态卷中，仅移除当前 thread_id，然后启动网关创建新的硬件会话。它不会修改设备/控制台密钥、Codex 登录、工作区或模型。恢复后用 `bash deploy/docker.sh health` 验证。

固件 0.6.0 与更新后的网关默认直接提交识别结果：空闲时启动一轮任务，工作中使用 turn/steer 将语音作为当前任务的补充指令。失败或不确定是否已送达时保留识别文字且不自动重试，避免重复提交；Codex 命令权限确认不受此设置影响。网关启动后后台预热 Whisper 模型，短指令使用 beam size 1 并跳过不必要的临时 WAV 写入，以缩短等待；速度与识别准确率存在取舍。日志记录识别及提交耗时，但不记录语音文本。旧网关未声明自动提交时，设备屏幕保留旧版确认提示。


## 0.7.0 配置画面

双键长按进入配置后，USB 主机连接时显示 USB 配置画面与默认电脑地址 `http://127.0.0.1:8788/`，提示先启动 `USB配置.cmd`；没有主机 USB 数据连接时显示 AP 配置画面，包含热点名、KEY 与 `http://192.168.4.1:80/`。两种画面都支持 PWR 长按退出。配置中插拔 USB 会切换入口与画面，不需要退出再进入。USB 网页也提供“显示设备配置画面”和“退出设备配置画面”，进入配置后不执行 AI 按键动作。仅插入 USB 在正常待机时不会强制弹出配置画面。8788 为本机工具默认端口，工具必须保持运行；AP 的 80 端口由 ESP32 提供。

蓝牙 ANTICATER 旋钮的配对、默认键位和实机验证边界见 [BLUETOOTH_KNOB.md](BLUETOOTH_KNOB.md)。

设备提示音、音量开关和录音互斥说明见 [SOUND_FEEDBACK.md](SOUND_FEEDBACK.md)。

目标连续推进：说“新目标是……”定义目标，“验收标准是……”补充标准；每轮结果后立即给出同目标A/B，详情菜单可回顾和确认收尾。见 [RECOMMENDATIONS.md](RECOMMENDATIONS.md)。

Taiji 圆屏首页使用 A/B 短选项与左滑换组，顶部 CODEX 标识和底部扩大语音按钮；0.13.0 S3 固件提供 RGB565 彩色主题，旧 C6 布局与消息上限保持兼容。

Taiji 0.14.0 新增[纵向任务流与偏好排序](docs/task-stream.md)：上下滑浏览、双击执行/决策，独立分析最多 2 个并行，修改排队；旧设备继续使用 A/B 页面。
