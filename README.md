# Codex Nudge

面向单账号 Linux 服务器的小工具：观测 Codex 额度窗口，并在显式开启后，以低消耗请求启动新的 5h 窗口。无需安装 Codex，也无需服务器上有浏览器。

默认只读。额度显示 100% 不会单独触发请求；工具先区分浮动 reset 和已启动窗口，再在发送前复查。结果不明时停止，重启不会补发。首版仅自动处理普通 5h 窗口，不处理 weekly、额外 bucket 或未经验证的套餐机制。

这是独立社区项目，采用实验性的 Codex 兼容认证和后端协议，不会增加订阅额度。接口支持及适用条款见[兼容性说明](docs/compatibility.md)。许可证为 [MIT](LICENSE)。

## 获取仓库后运行

需要 Linux 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。在仓库根目录运行，uv 会管理 Python 3.10+、本地环境和锁定的依赖：

```sh
uv sync --locked
uv run --locked codex-nudge --help
```

也可以直接执行 `uv run --locked codex-nudge …`，首次运行会自动准备环境。首版发布源码和版本 tag，不要求用户下载 wheel/sdist 或安装全局 Python 包。

## 配置与登录

状态目录应由当前用户独占，并与其他工具分开。配置包含状态路径、client ID、发送开关和查询间隔，不含凭据。先创建默认只读配置，再登录：

```sh
uv run --locked codex-nudge --config ~/.config/codex-nudge/settings.json --state-dir ~/.local/state/codex-nudge setup --client-id YOUR_COMPATIBILITY_CLIENT_ID
uv run --locked codex-nudge --config ~/.config/codex-nudge/settings.json login
uv run --locked codex-nudge --config ~/.config/codex-nudge/settings.json doctor --online
```

client ID 需显式配置，说明见[兼容认证](docs/compatibility.md#兼容认证)。`setup` 不覆盖已有配置，也不登录、发送请求或启用服务。

登录链接可在另一台机器打开。授权后 localhost 页面可能无法访问，将地址栏完整 URL 粘贴到终端的隐藏提示中，不要放进命令或聊天。工具独立保存和续期凭据，不读取日常 Codex 登录。

`status` 查询额度，`status --offline` 只看本地认证；`doctor` 默认离线，`doctor --online` 只增加查询，不续期或推理。

## 观测和自动发送

先只读观测，确认后再选择自动发送。两条命令择一运行：

```sh
uv run --locked codex-nudge --config ~/.config/codex-nudge/settings.json watch --no-send
uv run --locked codex-nudge --config ~/.config/codex-nudge/settings.json watch --send
```

`--send` 只影响本次进程，也允许必要的凭据续期。持久开启可在初次 `setup` 时加 `--enable-send`，或主动修改配置中的 `send`；`--no-send` 强制覆盖为只读。查询和诊断命令始终不会因此发送请求。

普通轮询为 300 秒，已知边界后等待 5 秒，再用至少间隔 2 秒的查询确认浮动 reset；发送后约 5 秒、35 秒复核固定锚点。其他客户端先启动窗口时跳过。`--interval` 可改普通间隔，`--once` 只做一轮观测，最多发送一次，不等待完整复核。

请求采用固定的 `gpt-6-luna` / `low` / `Say OK.` 配方，无历史或工具定义，不回退模型。已实测独立登录、续期和连续两个自然 5h 窗口，单次消耗为 18–19 tokens；这是观测值，不是上限。usage 来自服务端完成事件，缺失时保持未知。

## 后台运行

在不会被清理或移动的目录部署。`service` 仅生成文件，不安装或启动服务：

```sh
uv run --locked codex-nudge --config ~/.config/codex-nudge/settings.json service --no-send > /tmp/codex-nudge.service
systemd-analyze --user --generators=no --man=no verify /tmp/codex-nudge.service
mkdir -p ~/.config/systemd/user
install -m 600 /tmp/codex-nudge.service ~/.config/systemd/user/codex-nudge.service
systemctl --user daemon-reload
systemctl --user enable --now codex-nudge.service
journalctl --user -u codex-nudge.service -f
```

自动发送需以 `service --send` 重新生成、安装文件，再 daemon-reload 并 restart。切换前停止其他自动唤醒服务；不同目录的工具不共享锁。SSH 断开后是否继续运行取决于主机的用户 linger 设置，本工具不修改它。

持续 watch 的普通额度与凭据验证 GET 遇到临时网络/5xx 错误时，依次等待 5、15、45 秒，最多额外尝试 3 次；429 每次至少等待 300 秒。可重试 GET 的 `Retry-After` 支持秒数和 HTTP 日期，等待取服务端要求与本地退避的较大值；最后一次失败仍先等待该响应指定的时间，再由 systemd 等待 300 秒重启。等待不跨手动重启保留。`--once` 不等待重试，发送前预检失败交给下次进程重新观测；认证、协议和状态错误停止。停止并禁用用 `systemctl --user disable --now codex-nudge.service`。更新代码前先停止服务，切换到目标 tag，执行 `uv sync --locked`，重新生成服务文件，再启动；移动仓库或环境也需重新生成。

## 恢复与反馈

凭据到期可显式 `login --refresh`。发送模式的 watcher 会对已保存的 `verification_pending` 凭据自动恢复 GET 验证，也可显式 `login --verify`；只读 watch 仍要求显式验证。等待回调时使用 `login --resume`。授权交换或续期结果不明时，用 `login --reauthorize` 重新授权，保留配置中的同一 client ID。推理结果不明时保留状态，不通过删除记录强制重试；具体阶段见[架构与恢复](docs/architecture.md)。

脱敏诊断可在 watcher 运行时导出，不联网，也不创建或修改状态：

```sh
umask 077
uv run --locked codex-nudge --config ~/.config/codex-nudge/settings.json diagnostics > diagnostics.json
```

报告包含环境、配置开关、认证阶段、窗口和最近一次 usage；不包含凭据、账户标识、路径、attempt ID、原始回复或日志。`watcher.runtime` 提供后台记录的版本、实际发送开关、最后成功查询、下次动作、读重试次数及安全错误分类；其中 `automatic_requests_enabled` 能反映进程的 `--send` 覆盖。配置部分仍只表示配置文件。

runtime 是最后一次本地快照，不证明进程仍活着；超过预定动作时间 60 秒未更新时 `overdue=true`，没有预定时间则从最后更新时间起算。退出原因适用于程序捕获的错误、终端中断及 `--once` 正常完成；SIGTERM、强制结束或主机掉电可能只留下旧快照，服务存活状态以 `systemctl --user status codex-nudge.service` 为准。日志保存在前台标准输出或用户 journal，由主机的 journal 保留策略管理。

开发和发布约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。
