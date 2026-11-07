# Voxi 原生实时语音入口

新增的 [Voxi Agent](VOXI_AGENT.md) 在此语音入口之外维护独立后台任务。启用 Agent 频道后，PTT 音频由本地 Whisper 转写再提交，现有语音入口仅播报接收确认及用户主动查询的摘要；FAST 继续使用下述原生语音路径。

此版本为同一服务器上的 [Voxi](https://github.com/castoryan-looper/Voxi) 提供 `/voxi` WebSocket 入口，使用现有官方 Codex 登录建立独立 WebRTC 语音会话，无需新增 API Key。

入口不加入公网 Caddy 路由，只供共享 Docker 网络 `codex-pocket-radio` 使用，主机不新增公开端口；设备 bearer 鉴权与浏览器 Origin 拒绝在协商前执行。网关在该网络中的别名为 `pocket-backend`，桥接端口为 8789。原设备 /device、控制台、任务和登录卷保持原用途。

Voxi 连接脚本从现有 runtime/settings.json 读取设备 bearer，不访问账号登录文件。语音连接内部调用官方 `codex app-server --enable realtime_conversation`，初始化 experimentalApi，校验已有账号，再创建临时只读线程。通过 `thread/realtime/start` 的 WebRTC transport 获取 SDP，原生账号授权由 CLI 处理。直接 WebSocket transport 的 API Key 要求不会被绕过。

WebRTC 收听音频转为 PCM16LE /24kHz 单声道。按住期间由云端缓存音频，松开才使用 v3 RTP 麦克风音轨 提交完整语句及 800ms 静音尾段，借助服务端结束检测触发回复。握手及支持的 session.update 获得确认后宣告 READY；没有原生语音权限或协议不兼容时明确失败，不启动外部任务或 API Key 后备模式。

aiortc==1.14.0 放在 bridge/requirements-voxi.txt，作为独立 Docker 层安装，以便复用原有 Whisper 依赖。无新增 ASR/TTS 模型。Native voice 为实验能力，需 Codex 0.163.0 兼容运行时、有效登录、账号语音权限和可用的 WebRTC 出站链路。仅支持一个实时语音连接。

升级此仓库 gateway/ingress 后，参见 [Voxi 操作说明](https://github.com/castoryan-looper/Voxi/blob/main/docs/POCKET-BACKEND.md)。首次升级会重建镜像，保留既有命名卷，勿删除账号/模型数据来排查语音权限问题。

测试覆盖 PTT/短按/打断、鉴权、原生查询接续配置与本地 SDP 协商；真实账号和语音延迟仍待部署验证。

原生语音兼容性：固定 CLI 0.163.0 的 WebRTC 使用 version=v3、音色 cove。CLI 中 v3 会生成服务端要求的 OpenAI-Alpha: quicksilver=v2；v1 生成旧 quicksilver=v1。CLI 的 version=v2 是另一条协议，不能与 quicksilver=v2 请求头版本混为一谈。marin 不属于 v3 音色列表。

AVAS v3 的 session.update 使用官方 Frameless Bidi 结构：audio.output.voice=cove、delegation.type=client。该接口拒绝公共 Realtime 的 session.type、tools、turn_detection，因此不再尝试关闭 VAD，也不发送 input_audio_buffer.commit / response.create。

实时查询使用 Codex 0.163.0 原生委派，`clientManagedHandoffs=false` 让 app-server 在独立语音线程内执行查询并自动将结果送回语音。该线程启用 live web search、禁用 shell 工具，继续使用只读沙箱及现有登录。天气等实时问题先查询再回答，缺少城市或日期先追问。后续追问沿用同一连接和频道的线程上下文；重连、切台会创建新线程。

过程播报结束后保留查询状态，结果音频可自动开始新一段播放。查询与首次结果等待上限为 180 秒，失败时请求原生语音播报失败说明，超时则发送明确错误并断开以恢复连接。正常结果开始后的语音停滞继续使用 `responseIdleTimeoutSeconds`，完整长回复不受 180 秒总时长限制。已保存提示词中的旧内置句子“不要执行任务或使用工具。”在生成有效提示词时移除，其他用户设置保留。服务端要求客户端执行的工具或批准 RPC 仍被拒绝。

本地回归覆盖查询期间的过程播报、自动续播、同线程追问、失败提示和超时。上线后需用真实账号验证“查某城市天气 → 自动播报结果 → 追问晚上天气”；本地测试无法证明线上账号具备 live search 权限，也无法验证实际天气来源与设备听感。

按住时音频只保存在网关内存（每轮最大 30 秒约 1.44MB），松开才送入模型。尾部静音和服务端结束检测会增加延迟；长语句内的停顿仍可能影响自动分句。回复期间报告 busy，当前模式需等待回复结束再开始下一轮，不支持中途打断。READY 只验证会话建立，不等同真实音频往返测试通过。

WebRTC 音频修正：输入使用 aiortc sendrecv 音轨，输出仍从远端音轨接收；不在数据通道发送任何音频 JSON。松开后以真实时间播放完整录音及 800ms 静音，因此兼容方案的松开后延迟至少包含录音时长；这不是已经优化完成的低延迟模式。数据通道只用于协议支持的 session.update，回复完成仍等待 turn.done。
