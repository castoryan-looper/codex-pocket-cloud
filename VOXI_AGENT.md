# Voxi Agent：Pocket Cloud 首个可用版本

本版 Voxi Agent 继续使用现有 Codex CLI 0.163.0、官方登录、本机 Whisper 和原生语音播报。不需要新的模型 API Key，也不复用旧 Pocket 设备正在执行的任务线程。另支持接入用户已有的 OpenClaw（HTTPS 或家中主动连接器），不替换 Voxi Agent 的 Codex 执行器。

## 已实现

- 语音连接和后台任务分离。FAST 保留现有问答及只读实时查询；AGENT 先转写、持久保存任务，再播报“任务已保存”。
- CH0–CH3 可配置名称和 `fast / agent / openclaw / disabled`，支持公共模板及明确的用户覆盖/恢复继承。`openclaw` 需指定同一用户拥有的 `connectionId`；公共模板不允许跨用户共享此连接。用户通过现有设备—用户绑定确定，多个设备可共享频道。
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

新增 `reset_channels` 恢复公共模板；`connect_openclaw`、`test_openclaw`、`delete_openclaw` 管理用户连接。`relay` 仅供 Voxi 网关固定的 `/api/openclaw/relay` 代理：使用每连接随机密钥的 SHA-256 校验，不能访问管理员配置、其他用户/连接任务或 Pocket 内部凭据。API 返回的家中连接密钥只显示一次；直连 Gateway Token 加密保存、不回显。新 `openclaw_connections` 表沿用同一数据库与主密钥；只有 OpenClaw 密文时也会拒绝在主密钥丢失后重建。

任务新增 `engine` / `connection_id` 列，老任务迁移为 `codex`；提交时在同一事务固定执行器。新 OpenClaw 任务使用独立 `voxi-<user>-<task>` 会话键，“继续”保留原连接与会话。直连验证证书、禁止重定向、拒绝实际解析出的非公网 IP。主动连接器在 OpenClaw 电脑轮询 Voxi HTTPS，不将本机 Gateway Token 上传；领取状态先持久保存再发任务，不重新派发不确定任务。端到端同一结果重传可确认，不重执行。具体使用步骤与下载入口见 Voxi 仓库 `docs/VOXI-AGENT.md` 和后台“频道与助手”。

探测只调用认证的 `GET /v1/models`，不消耗模型推理；依赖 OpenClaw 启用 `gateway.http.endpoints.responses.enabled`。执行接官方 `POST /v1/responses` SSE，只有 `response.completed` 且资源状态 completed 才成功。响应失败/截断不当成成功，缺终态则 delivery_unknown，不自动重试。外部工具批准在 OpenClaw 内处理；停止等待不声明远端停止。家中连接器退出时未回传的结果暂存在内存，可能丢失。后续可增加本机服务安装和可靠结果发件箱。

状态包括 queued、submitting、running、needs_input、awaiting_approval、cancelling、completed、failed、cancelled、interrupted、delivery_unknown。任务最长执行时间包含等待用户操作的时间。停机或状态不明不会被标成成功。

私有运行卷新增：

- `voxi-agent.sqlite3` 及 SQLite WAL/SHM：任务、配置、加密凭据、用量。
- `voxi-credentials.key`：AES-256-GCM 加密密钥，首次保存供应商凭据时创建，POSIX 权限 0600。
- 用户工作区位于已有 workspace 卷。

备份时先停止服务或使用 SQLite 一致性备份；加密密钥和数据库必须一起保留，密钥缺失时拒绝覆盖已有加密记录。不要提交运行卷、数据库、加密密钥或账号文件。

本次任务表会自动增加执行器字段，保留旧任务。不要在新建 OpenClaw 任务后只回滚旧 Agent 镜像并继续派发：旧版不认识外部执行器，且旧 INSERT 不兼容新增列。回退时先停任务服务，再恢复成对的升级前数据库/密钥备份；不得将排队的 OpenClaw 任务交给旧 Codex 执行器。

## 部署与验证

推荐在 Voxi 仓库执行 `sudo python3 deploy/upgrade-pocket-agent.py`，它会区分自动快照发布与普通 Git 部署。Docker Compose 标签里的 working_dir 可能指向不含 `.git` 的发布快照，不能直接把它当成源码仓库。

仅在确认使用普通 Git 部署、且当前位于真正源码仓库时，才执行下面命令（保留已有 `.env.docker`）：

```bash
git pull --ff-only
bash deploy/docker.sh up
```

再到 Voxi 仓库运行 `sudo bash deploy/deploy.sh`，随后 `sudo bash deploy/check-agent.sh`。检查脚本只输出版本、执行器类型、启用状态和凭据存在标志，不输出凭据或任务内容。

本地测试：`python -m unittest discover -s bridge -p 'test_*.py'`；需安装 `bridge/requirements.txt` 和 `bridge/requirements-voxi.txt`。测试中的 Codex 为可控模拟，不能代替真实账号、麦克风和设备听感验收。

## 普通用户空间

新增 Docker 内网 `POST /voxi/portal`，网关通过 `/api/user/*` 验证普通账户后固定其 `actorUserId`。此入口不加入公网 Pocket ingress，仍验证内部 bearer 并拒绝浏览器 Origin；普通客户端不能覆盖身份。`snapshot` 仅返回该档案的资料、频道、偏好、连接、设备、记忆、转写与任务；不复用全局管理员快照。任务、OpenClaw、设备操作再次核对所有者，拒绝设置全局参数、凭据、改绑和创建档案。

内网管理员 `/voxi/agent` 新增 `create_profile`，不改变现有设备归属。个人 FAST 偏好保存在 AgentStore 用户配置键中，在 FAST 新会话加载；管理员快照额外返回 `preferencesByUser` 供查看，普通快照只返回本人。`runtime.userPortalVersion=1` 用于部署核对。

登录账户与密码摘要在 Voxi 网关的已有状态卷，Pocket 只存档案和配置。网页开通/停用不等于取消硬件任务，也不升级共享 Codex 试用环境为安全的公众多租户执行器。开通步骤及备份说明见 Voxi `docs/USER-ACCOUNTS.md`。

## 音色与试听

`voxi-config.json` 新增服务默认 `voice`，旧文件迁移为 Cove；用户 preferences 新增 `voice`，空字符串表示继承默认，旧 fastPrompt-only 更新保留已选音色。`VoiceSession` 在新建/切台时按个人覆盖→服务默认选择，传到 realtime/start 和 Frameless session.update；当前会话不因保存而重建。

`voxi_voices.py` 固定 Codex 0.163.0 v3 候选目录和一句话文本。内网 `POST /voxi/voice-preview` 验证 bearer/Origin、固定文本和身份，调用独立临时 WebRTC VoiceSession 与原生 appendSpeech，输出实际 PCM WAV 的 base64；不开麦，不带记忆，不派发任务，不写语音文件。最多一个生成中试听，遵守总语音名额、12秒音频上限和80秒生成截止；取消时停止子进程与临时会话。固定音频最多缓存4音色/10分钟。兼容目录不等于账户实测可用，失败不回退其他声音。

用户 portal 快照包含 `voiceSettings`（目录、默认/个人/生效音色与试听句子），`voice_preference` 只修改本人音色。管理员快照 runtime 添加 `voicePreviewVersion=1`。Voxi 网关新增独立鉴权试听代理，90秒请求截止、客户端断开传递取消，不沿用普通配置接口的8秒截止。

真实浏览器已验证模拟 PCM 试听解码、无自动保存、个人/默认持久化和停止回收。Windows本机0.161登录可用但WebRTC等ACK超时；云端0.163和真实音色需升级后点击试听验收。未采用需要API key认证的WebSocket路径，不要求新增模型密钥。详见 Voxi `docs/VOICE-PREVIEW.md`。

## 运行环境限制

本版面向当前个人／小范围试用环境。每用户目录和应用层所有者校验不构成互不信任租户之间的执行隔离；Docker externalSandbox 沿用现有个人执行环境，CLI 登录也仍是现有账户。**不能把这一版直接作为面向公众收费、共享个人订阅的多租户服务。**

自有 OpenClaw 的本地/云端 HTTP 接入已实现，原生频道插件、服务安装/自动启动、OpenClaw 托管工作器、商业 API 执行和收费账本仍是后续阶段。连接检测、模拟运行和真实用户 OpenClaw 验收分别核对；不生成虚构的 API 费用。
