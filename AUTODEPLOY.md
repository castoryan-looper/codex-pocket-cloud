# main 自动部署

适用于已经运行的 Linux / systemd / Docker Compose v2 部署。服务器每分钟主动检查 `origin/main`，不需要从 GitHub SSH 进入服务器，也不需要增加公网端口。检测到新提交后构建新镜像，通过隔离测试，再一起重建 gateway 与 ingress；HTTPS 健康检查通过后才记录成功。

这会更新云端服务，不会自动刷写 ESP32。云端重建期间会短暂断连，设备沿用现有重连逻辑。

## 首次启用

先将希望上线的修改提交并推送到 GitHub 的 **main**。自动部署以远程 main 为准，不包含电脑或服务器上的未提交修改，也不需要服务器当前工作分支就是 main。本工具不会 checkout、reset 或覆盖服务器原仓库。

在服务器 Web 控制台进入现有仓库（有 `.git`、`compose.yaml`、`.env.docker`），确认它已正常启动：

```bash
cd /实际路径/codex-pocket-cloud
sudo bash deploy/docker.sh health
git ls-remote --exit-code --heads origin refs/heads/main
```

服务器需要 Python 3.10+、Git、Docker、Compose v2、systemd，以及读取远程仓库的能力。公开仓库可使用 HTTPS origin；私有仓库需提前配置供 root 使用的只读凭据，后台不会弹出密码输入框。不要把个人访问令牌写进命令、仓库或日志。

代码已在服务器仓库时，安装：

```bash
sudo python3 deploy/autodeploy.py install --repo "$PWD"
```

如果尚未把自动部署文件同步到仓库，可先上传单文件 `codex-pocket-autodeploy.py` 到服务器 `/tmp/`，在同一仓库目录执行：

```bash
sudo python3 /tmp/codex-pocket-autodeploy.py install --repo "$PWD"
```

安装会确认远程 main 存在且原来的 gateway、ingress 已在运行，然后安装服务并启动首次检查。没有进行中的部署时约每 60–65 秒检查一次；检测到更新后还需要实际构建和启动时间。构建进行中会串行等待，不会并发部署。多个提交到来时，下次检查会部署那时 main 最新的提交。

## 查看和管理

```bash
# 查看自动部署日志，Ctrl+C 只退出查看
sudo journalctl -u codex-pocket-autodeploy.service -f

# 查看定时检查状态
sudo systemctl status codex-pocket-autodeploy.timer

# 查看已部署的 Git 提交及最近失败状态
sudo python3 /usr/local/lib/codex-pocket-autodeploy/autodeploy.py status

# 立即检查；同一失败版本不受 15 分钟冷却限制
sudo python3 /usr/local/lib/codex-pocket-autodeploy/autodeploy.py run --retry

# 暂停后续检查；不会停止已在运行的云服务
sudo systemctl disable --now codex-pocket-autodeploy.timer

# 恢复检查
sudo systemctl enable --now codex-pocket-autodeploy.timer
```

暂停 timer 不会中断已经开始的构建/部署。部署器使用文件锁防止手动检查与 timer 重叠。

## 更新与失败处理

- 每次拉取远程 main 的准确 Git SHA，在 `/var/lib/codex-pocket-autodeploy/release-*/source/` 导出独立源码，原仓库和 `.env.docker` 不改动。环境文件变化也会触发重新部署。
- 显式沿用原 Compose 项目名称，并把新版本的数据卷名称和挂载位置与现有容器逐一比较。不一致时停止，不会悄悄创建一个空账号/工作区。
- 测试容器没有生产数据卷、网络访问或可写根文件系统；只在临时 `/tmp` 写测试数据。
- 构建或测试失败时旧容器继续运行。启动或 HTTPS 健康检查失败时，使用保留的旧镜像标签和旧 Compose 配置自动恢复，既有登录、密钥、工作区、证书与模型卷保留。
- 部署启动前写入中断恢复记录。如果进程被终止或机器重启，下次执行先恢复旧版本，再检查 main。
- 失败的同一提交/环境组合默认 15 分钟后重试；有新提交则不受旧失败冷却影响。手动 `run --retry` 可立即重试。
- 如果连旧版都恢复失败，会记录 `rollback_failed` 并停止后续自动部署。先暂停 timer、查看 journal 和 `/var/lib/codex-pocket-autodeploy/state.json` 的 `pending` 记录。确认旧服务恢复后，删除该 JSON 中的 `rollback_failed` 标记，再运行一次检查；保留 `pending` 让工具再次核验恢复。不要通过清空数据卷解决故障。

源码快照、私有环境副本及回滚镜像会保留用于恢复。不要公开该状态目录；不要删除正在使用的快照或执行 `docker compose down -v`。这个初版部署器支持现有 gateway/ingress 两服务拓扑；若 main 将来更改服务拓扑、数据卷或部署器自身逻辑，需先审阅并重新运行安装器，避免旧部署器错误处理基础设施变化。

本工具在服务器独立执行构建、测试和健康检查，不以 GitHub CI 成功作为前提。仓库原 Docker CI 已加入 main，另有自动部署逻辑的 Linux 测试。主机端不需要 GitHub 管理员凭据或远程执行入口。

## 验证范围

本地 14 项部署测试中 13 项通过，1 项 Linux 文件锁测试在 Windows 跳过、由 Linux CI 执行；包括真实 Git bundle 拉取 main、保留 dirty main 工作区、重复提交不重建、失败冷却、构建/测试失败、启动和健康失败回滚、中断恢复及数据卷变化拒绝。Docker 调用使用测试替身验证状态机；尚未在你的真实服务器安装，也未宣称真实 Docker/systemd 集成已通过。首次安装后请查看日志中的 `Deployed and verified main commit ...`，再检查设备连接和实际语音功能。

远程仓库实际默认分支为 `main`，安装器默认监听 `origin/main`。如果将来确实使用 `master`，可在安装时显式添加 `--branch master`。
