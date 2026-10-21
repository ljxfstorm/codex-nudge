# Codex quota window tool

项目名待定。面向单账号 Linux 服务器的轻量 CLI，用于观测 Codex 额度窗口，并在显式开启后，以低消耗请求启动新的窗口。

当前仓库提供开发态 CLI：独立浏览器登录、显式续期、认证恢复、额度查询、本地诊断及 5h watch。systemd 服务、安装配置和发行包尚未完成。它是独立的新仓库，不包含原型的 Git 历史、凭据或运行数据；quota_window 为内部暂用包名，不代表最终项目名。

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

## 观测与自动发送

下面第一条持续只读观测；第二条才允许发送推理请求及必要的凭据续期。不要同时运行两条命令，同一目录只允许一个 watcher。

```sh
PYTHONPATH=src .venv/bin/python -m quota_window --state-dir /your/private/state watch
PYTHONPATH=src .venv/bin/python -m quota_window --state-dir /your/private/state watch --send
```

默认普通轮询 300 秒，临近已知边界时等待 reset 后 5 秒，再用至少间隔 2 秒的查询区分浮动与固定 reset。只有已确认账户、普通额度允许使用且 5h 窗口完整时才可能发送。额度显示 100% 不单独触发请求；已有活跃窗口会等待其边界。weekly 和额外 bucket 不触发发送。

请求固定使用原型测过的 `gpt-6-luna`、`low`、`Say OK.` 配方，无历史和工具定义，不回退模型。这条兼容路径使用固定版本的 Codex 请求头及内部 lite 字段，尚无稳定公共接口保证。原型曾测得 18 tokens，这是一次实测值，不是消耗上限；新实现尚未进行真实推理验收。usage 取自服务端完成事件，缺失值保持未知，输出不保存回复正文。

发送后在完成约 5 秒、35 秒处查询，要求 reset 固定在请求附近，才记录验证成功。进程中断、响应丢失或 usage 不完整都会保留发送意图并停止；重启不补发。网络或认证错误也会退出，当前没有后台重试守护。保留状态，不要通过删除文件来强制重试。

`--once` 只执行一轮观测，最多发送一次，不等待整个复核流程。初次观测通常不会发送；后续运行会继续分类或仅复核已完成请求。持续运行不需要这个选项。`--interval` 可调整普通轮询间隔，范围 2–3600 秒。

观测事件以脱敏 JSON 行写到标准输出，最近一次请求的 token 数及复核样本保存在私有 `watch/schedule.json`。当前不提供完整历史导出或推理不确定状态的自动解除；登录恢复也不会清除推理发送标记。
