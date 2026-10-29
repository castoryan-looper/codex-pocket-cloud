# Pocket 执行 Harness（第一版）

网关在新建和恢复硬件会话时，向 Codex app-server 提供 `bridge/harness.py` 中的 `developerInstructions`。它要求识别推荐与用户授权的区别、据实报告验证与阻塞、不能假称访问电脑或其他会话。原有工作区的 AGENTS.md 仍保留，由 Codex 按原有工作区规则使用；本功能不覆盖用户文件。文本约束不是程序级权限隔离。

推荐候选仍由本设备会话的近期文本和规则生成；浏览与跳过不提交任务，只有在详情明确执行才启动。任务审批仍由 app-server 事件与现有控制台处理，不自动批准。账户范围的历史同步、远程设备执行、收藏/不感兴趣以及独立推荐评估尚未实现。

私有管理接口 `/api/state` 在现有字段之外返回 `task`，包含 `state`、`turnId`、`source`、`summary` 和 `validation`。状态基于提交与 Codex 事件；`completed` 只表示该轮结束状态，不能证明任务目标完成。当前 `validation` 固定为 `not_reported`，并不会从自然语言猜测测试结果。任务结构仅在网关进程内保存，重启后不会声称恢复旧状态；会话历史仍由 Codex 管理。发送结果不确定时状态为 `delivery_unknown`，不自动重试。

Docker 仍以容器提供外层隔离，网络可用、持久卷可见。工作区权限、外部资源授权和跨设备粒度隔离需要后续设计，指令文件不能代替这些限制。

验证：`python -m unittest discover -s bridge -p 'test_*.py' -q`。包括会话新建/恢复携带指令、任务事件、审批、未知送达、推荐需显式执行及现有网关测试。生产自动部署仍需通过其独立容器测试和健康检查。
