# Voxi Agent：Pocket Cloud 首个可用版本

本版继续使用现有 Codex CLI 0.163.0、官方登录、本机 Whisper 和原生语音播报。不需要新的模型 API Key，不安装 OpenClaw，也不复用旧 Pocket 设备正在执行的任务线程。

## 已实现

- 语音连接和后台任务分离。FAST 保留现有问答及只读实时查询；AGENT 先转写、持久保存任务，再播报“任务已保存”。
- CH0–CH3 可配置名称和 `fast / agent / disabled`，支持默认配置及用户覆盖。用户通过现有设备—用户绑定确定，多个设备可共享频道。
- 独立 Codex app-server 任务执行器、每用户工作目录 `/workspace/voxi-users/<userId>`、每任务线程，以及明确的继续操作。不会把其他 Pocket 任务线程当作当前任务。
- SQLite 保存任务、请求编号、线程/轮次引用、简短状态、结果、批准请求和实际报告的 token。相同用户的相同请求编号不会重复派发；同一用户同一频道串行，整体并发有上限。
- 成功只认 Codex `turn/completed` 事件。取消先标记“正在停止”，收到执行器终态后才确认；取消不会撤销此前的文件或外部修改。
- 设备断开不停止已接收任务。重启后在途任务标记中断，提交状态未知的任务保留未知状态，不自动重放；尚未派发的排队任务可继续执行。
- 任务状态通过设备协议通知；用户主动说“读结果”才播报摘要，不在另一段语音中自动插播。
- OpenAI / Anthropic 凭据可在 Voxi 管理后台填写、加密保存和删除。**当前执行器仍使用 Codex 登录；已保存不等于已验证，API 凭据暂未参与推理。**

## 启用与使用

1. 升级本仓库的 Pocket 后端，再升级 Voxi 网关；保留所有现有命名卷、账号登录、模型、设备密钥和 CA。
2. 打开 Voxi 网关的 `/admin/agent`，使用已有管理员账户登录。设备至少连接一次后，会出现绑定用户。
3. 勾选“启用 Voxi Agent 频道路由”。默认 CH0 快问、CH1 Voxi Agent、CH2/CH3 未启用。初始关闭以兼容现有部署。
4. 在 `/admin/agent/tasks` 先提交一个小任务验证现有 Codex 登录。可以查看结果、取消、批准/拒绝单个请求、回答问题、继续任务。
5. 设备切到 CH1 按键派发任务。可说“查看进度”“读结果”“取消任务”“继续任务，补充要求”。“同意执行／批准操作／拒绝操作”只作用于当前频道最新任务的实际待批准请求；建议先在网页核对完整请求。

配套新版 MiniCam 固件显示 FAST/AGENT 和任务标记，并在同一录音的重连重传中保留请求编号。旧固件可使用语音任务，但没有新标记，跨连接录音重传也没有相同的去重保证。线协议频道编号为 0–3；旧屏幕的第 2 频道对应 CH1。

## 接口与存储

`GET/POST /voxi/agent` 仅 Docker 内网可用，沿用 Pocket 专用设备 bearer，拒绝浏览器 Origin；不加入公网 Pocket Caddy 路由。Voxi 网关只在通过现有管理员登录及 CSRF 校验后代理此固定接口。

POST 的 `action`：`settings`、`channels`、`credential`、`submit`、`detail`、`cancel`、`approve`、`decline`、`answer`、`continue`。任务操作均指定 `userId`；详情、批准、中断和继续核对任务所有者。提交/继续必须有稳定的 `requestId`。

状态包括 queued、submitting、running、needs_input、awaiting_approval、cancelling、completed、failed、cancelled、interrupted、delivery_unknown。任务最长执行时间包含等待用户操作的时间。停机或状态不明不会被标成成功。

私有运行卷新增：

- `voxi-agent.sqlite3` 及 SQLite WAL/SHM：任务、配置、加密凭据、用量。
- `voxi-credentials.key`：AES-256-GCM 加密密钥，首次保存供应商凭据时创建，POSIX 权限 0600。
- 用户工作区位于已有 workspace 卷。

备份时先停止服务或使用 SQLite 一致性备份；加密密钥和数据库必须一起保留，密钥缺失时拒绝覆盖已有加密记录。不要提交运行卷、数据库、加密密钥或账号文件。

## 部署与验证

推荐在 Voxi 仓库执行 `sudo python3 deploy/upgrade-pocket-agent.py`，它会区分自动快照发布与普通 Git 部署。Docker Compose 标签里的 working_dir 可能指向不含 `.git` 的发布快照，不能直接把它当成源码仓库。

仅在确认使用普通 Git 部署、且当前位于真正源码仓库时，才执行下面命令（保留已有 `.env.docker`）：

```bash
git pull --ff-only
bash deploy/docker.sh up
```

再到 Voxi 仓库运行 `sudo bash deploy/deploy.sh`，随后 `sudo bash deploy/check-agent.sh`。检查脚本只输出版本、执行器类型、启用状态和凭据存在标志，不输出凭据或任务内容。

本地测试：`python -m unittest discover -s bridge -p 'test_*.py'`；需安装 `bridge/requirements.txt` 和 `bridge/requirements-voxi.txt`。测试中的 Codex 为可控模拟，不能代替真实账号、麦克风和设备听感验收。

## 本版边界及后续方向

本版面向当前个人／小范围试用环境。每用户目录和应用层所有者校验不构成互不信任租户之间的执行隔离；Docker externalSandbox 沿用现有个人执行环境，CLI 登录也仍是现有账户。**不能把这一版直接作为面向公众收费、共享个人订阅的多租户服务。**

自有 OpenClaw 本地/云端插件接入、OpenClaw 托管工作器、商业 API 执行和收费账本仍是后续阶段；当前没有把占位连接显示为可用，也不生成虚构的 API 费用。后台凭据入口先完成，后续启用 API 执行时可复用。
