# Codex quota window tool

项目名待定。面向单账号 Linux 服务器的轻量 CLI，用于观测 Codex 额度窗口，并在显式开启后，以低消耗请求启动新的窗口。

当前仓库提供开发态 CLI：独立浏览器登录、显式续期、认证恢复、额度查询与本地诊断。自动请求、watch 和 systemd 服务尚未迁入，暂不提供可安装发行包。它是独立的新仓库，不包含原型的 Git 历史、凭据或运行数据；quota_window 为内部暂用包名，不代表最终项目名。

首版聚焦已经实测的 5h 调度：识别实际 bucket 和时长，区分移动 reset 与已启动窗口，发送后复核固定锚点。其他套餐、weekly 和额外 reset 机制需要独立验证。

目标流程是安装、配置、登录、诊断、观测，再按需开启自动发送和 systemd 用户服务。目标机器无需安装 Codex 或提供桌面会话；工具自行完成授权与续期。浏览器 PKCE 路径已在独立原型验证，新实现目前通过离线回归测试，尚未完成真实账户验收。首版采用 Python，通过 GitHub Release 提供 wheel/sdist 及 uv 安装说明，完整安装成本计入解释器与依赖。

保持单账号、本地运行的范围。默认只读；自动发送采用持久化去重，结果不明时停止并提供恢复信息。账号配对、实际 token 记录及脱敏诊断属于首版范围。

开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。许可为 [MIT](LICENSE)。

## 开发态使用

按 [CONTRIBUTING.md](CONTRIBUTING.md) 安装开发依赖后，在仓库根目录运行。目录必须由当前用户独占，且与原型、Codex 的凭据目录分开；不存在时由登录命令创建。

```sh
PYTHONPATH=src .venv/bin/python -m quota_window --state-dir /your/private/state doctor
PYTHONPATH=src .venv/bin/python -m quota_window --state-dir /your/private/state login --client-id YOUR_COMPATIBILITY_CLIENT_ID
PYTHONPATH=src .venv/bin/python -m quota_window --state-dir /your/private/state status
```

首条 doctor 在未登录时返回 1，表示未具备查询条件。登录链接可在另一台机器的浏览器打开；授权后 localhost 回调页面可能打不开，将地址栏完整 URL 粘贴到隐藏终端提示中。不要把回调放入 shell 命令、日志或聊天。登录和续期会查询额度核对账户，不发送推理请求。

`status --offline` 只看本地状态。`doctor` 默认离线，`doctor --online` 和普通 `status` 仅查询，不隐式续期。查询显示的 reset 不能单独证明窗口已开始计时。

访问令牌到期时，显式运行 `login --refresh`。若状态为 `verification_pending`，运行 `login --verify` 复核已保存的新凭据；仍在等待回调时可用 `login --resume`。交换或续期结果不明时，用 `login --client-id YOUR_COMPATIBILITY_CLIENT_ID --reauthorize` 重新授权，不重发原请求。详细状态约束见 [认证与恢复](docs/architecture.md)。

兼容路径需要显式 client ID，不内置第三方注册默认值。技术验证不代表这条兼容路径具有官方公共 API 支持或分发许可；正式发布前仍需完成相关审查。
