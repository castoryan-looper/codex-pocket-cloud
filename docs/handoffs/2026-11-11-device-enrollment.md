# 设备归属管理交接 · 2026-11-11

配套 Voxi 的 feat/device-enrollment-flow 分支。新增管理员内网 transfer_device / retire_device；转移轮换令牌摘要、事务校验 sourceUserId，移除登记保留 retired_devices 标记。两个操作均关闭该设备会话，保留用户、记忆、转写、任务和其他设备。

先部署 Pocket，再部署 Voxi 网关。registry 返回 deviceLifecycleVersion=1 供新页面启用操作；旧后端时新页面禁用转移/移除。公网旧 rebind 在 Voxi 已拒绝，Pocket 内网旧接口保留兼容，不作为新页面入口。

本地 SQLite 验证旧 key 失效、归属竞态拒绝、历史保留、移除后禁止 legacy 重建、重新登记可恢复。API 验证鉴权与关闭正确会话。生产部署和真实设备 READY 尚未完成；本机浏览器证书不受信任且 SSH 主机密钥未核验，不绕过。没有变更生产设备归属或令牌。OTA 仍撤回。
