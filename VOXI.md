# Voxi 原生实时语音入口

此版本为同一服务器上的 [Voxi](https://github.com/castoryan-looper/Voxi) 提供 `/voxi` WebSocket 入口，使用现有官方 Codex 登录建立独立 WebRTC 语音会话，无需新增 API Key。

入口不加入公网 Caddy 路由，只供共享 Docker 网络 `codex-pocket-radio` 使用，主机不新增公开端口；设备 bearer 鉴权与浏览器 Origin 拒绝在协商前执行。网关在该网络中的别名为 `pocket-backend`，桥接端口为 8789。原设备 /device、控制台、任务和登录卷保持原用途。

Voxi 连接脚本从现有 runtime/settings.json 读取设备 bearer，不访问账号登录文件。语音连接内部调用官方 `codex app-server --enable realtime_conversation`，初始化 experimentalApi，校验已有账号，再创建临时只读线程。通过 `thread/realtime/start` 的 WebRTC transport 获取 SDP，原生账号授权由 CLI 处理。直接 WebSocket transport 的 API Key 要求不会被绕过。

WebRTC 收听音频转为 PCM16LE /24kHz 单声道。按住期间由云端缓存音频，松开才使用 v3 RTP 麦克风音轨 提交完整语句及 800ms 静音尾段，借助服务端结束检测触发回复。握手及支持的 session.update 获得确认后宣告 READY；没有原生语音权限或协议不兼容时明确失败，不启动外部任务或 API Key 后备模式。

aiortc==1.14.0 放在 bridge/requirements-voxi.txt，作为独立 Docker 层安装，以便复用原有 Whisper 依赖。无新增 ASR/TTS 模型。Native voice 为实验能力，需 Codex 0.163.0 兼容运行时、有效登录、账号语音权限和可用的 WebRTC 出站链路。仅支持一个实时语音连接。

升级此仓库 gateway/ingress 后，参见 [Voxi 操作说明](https://github.com/castoryan-looper/Voxi/blob/main/docs/POCKET-BACKEND.md)。首次升级会重建镜像，保留既有命名卷，勿删除账号/模型数据来排查语音权限问题。

测试覆盖 PTT/短按/打断、鉴权、空工具配置确认与本地 SDP 协商；真实账号和语音延迟仍待部署验证。

原生语音兼容性：固定 CLI 0.163.0 的 WebRTC 使用 version=v3、音色 cove。CLI 中 v3 会生成服务端要求的 OpenAI-Alpha: quicksilver=v2；v1 生成旧 quicksilver=v1。CLI 的 version=v2 是另一条协议，不能与 quicksilver=v2 请求头版本混为一谈。marin 不属于 v3 音色列表。

AVAS v3 的 session.update 使用官方 Frameless Bidi 结构：audio.output.voice=cove、delegation.type=client。该接口拒绝公共 Realtime 的 session.type、tools、turn_detection，因此不再尝试关闭 VAD，也不发送 input_audio_buffer.commit / response.create。任务委派仍通过 clientManagedHandoffs=true 交给本客户端管理，本客户端不执行委派，并拒绝服务端 RPC 工具/批准请求。

按住时音频只保存在网关内存（每轮最大 30 秒约 1.44MB），松开才送入模型。尾部静音和服务端结束检测会增加延迟；长语句内的停顿仍可能影响自动分句。回复期间报告 busy，当前模式需等待回复结束再开始下一轮，不支持中途打断。READY 只验证会话建立，不等同真实音频往返测试通过。

WebRTC 音频修正：输入使用 aiortc sendrecv 音轨，输出仍从远端音轨接收；不在数据通道发送任何音频 JSON。松开后以真实时间播放完整录音及 800ms 静音，因此兼容方案的松开后延迟至少包含录音时长；这不是已经优化完成的低延迟模式。数据通道只用于协议支持的 session.update，回复完成仍等待 turn.done。
