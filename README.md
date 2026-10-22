# Codex Nudge

候选项目名为 `codex-nudge`，等待最终确认。面向单账号 Linux 服务器的轻量 CLI，用于观测 Codex 额度窗口，并在显式开启后，以低消耗请求启动新的窗口。

当前仓库提供 CLI 和本地候选 wheel/sdist：配置、独立浏览器登录、显式续期、认证恢复、额度查询、本地诊断、5h watch 和 systemd 用户服务文件生成。尚未发布公开 Release。它是独立的新仓库，不包含原型的 Git 历史、凭据或运行数据；`quota_window` 为内部 Python 包名，安装后的候选命令为 `codex-nudge`。

首版聚焦已经实测的 5h 调度：识别实际 bucket 和时长，区分移动 reset 与已启动窗口，发送后复核固定锚点。其他套餐、weekly 和额外 reset 机制需要独立验证。

目标流程是安装、配置、登录、诊断、观测，再按需开启自动发送和 systemd 用户服务。目标机器无需安装 Codex 或提供桌面会话；工具自行完成授权与续期。新实现已完成真实浏览器 PKCE 登录、一次续期及轮换、额度查询和只读 watch 重启验收；第二次独立推理诊断取得 18 tokens，定位并修复了缺少 Content-Type 导致的误拒绝，同一真实响应已通过修复后解析器的离线回放。修复后的版本已完成首次整夜验收：连续两个自然 5h 边界自动发送并确认固定 reset，分别消耗 19、18 tokens，进程没有重启；早期技术验证至此完成，更长运行和真实故障通过正式使用反馈持续改进，不作为首版发布门槛。首版采用 Python，通过 GitHub Release 提供 wheel/sdist 及 uv 安装说明，完整安装成本计入解释器与依赖。

保持单账号、本地运行的范围。默认只读；自动发送采用持久化去重，结果不明时停止并提供恢复信息。账号配对、实际 token 记录及脱敏诊断属于首版范围。

开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。许可为 [MIT](LICENSE)。

## 安装候选发行包

Linux，Python 3.10 或更新版本。将下载或本地构建的固定版本 wheel 交给 uv 安装；无需安装 Codex：

```sh
uv tool install /path/to/codex_nudge-0.1.0-py3-none-any.whl
codex-nudge --version
```

单次 Linux x86_64 安装测量：wheel 约 33 KiB；独立 Python 3.10 工具环境含依赖约 16.9 MiB，解释器与标准库目录约 88.8 MiB。目录占用不等于新增磁盘成本，且不包含 uv 安装器、下载缓存和主机系统库；不同平台与依赖版本会变化。

后面的示例使用开发态 `PYTHONPATH=src .venv/bin/python -m quota_window`，安装后将该前缀替换为 `codex-nudge` 即可，其他参数一致。不要把工具安装到会被清理的临时目录；服务引用安装环境的绝对路径。卸载前先停止并禁用已安装的用户服务，再执行 `uv tool uninstall codex-nudge`；卸载不会删除私有凭据和状态。

## 开发态使用

按 [CONTRIBUTING.md](CONTRIBUTING.md) 安装开发依赖后，在仓库根目录运行。状态目录必须由当前用户独占，且与原型、Codex 的凭据目录分开；不存在时由登录命令创建。先创建配置，不会登录、发送请求或启用服务：

```sh
PYTHONPATH=src .venv/bin/python -m quota_window --config /your/private/config/settings.json --state-dir /your/private/state setup --client-id YOUR_COMPATIBILITY_CLIENT_ID
PYTHONPATH=src .venv/bin/python -m quota_window --config /your/private/config/settings.json doctor
PYTHONPATH=src .venv/bin/python -m quota_window --config /your/private/config/settings.json login
PYTHONPATH=src .venv/bin/python -m quota_window --config /your/private/config/settings.json status
```

首条 doctor 在未登录时返回 1，表示未具备查询条件。登录链接可在另一台机器的浏览器打开；授权后 localhost 回调页面可能打不开，将地址栏完整 URL 粘贴到隐藏终端提示中。不要把回调放入 shell 命令、日志或聊天。登录和续期会查询额度核对账户，不发送推理请求。

配置是当前用户拥有的私有 JSON 文件（0600），`setup` 不覆盖已有文件。字段为 `schema: 1`、`state_dir`、可选 `client_id`、`send`（默认 false）、`interval`（默认 300）；不接受凭据字段或未知字段。配置内相对状态路径以配置文件所在目录为基准，不展开 `$变量`。命令行参数覆盖配置，未提供配置时仍可直接用 `--state-dir`。

`status --offline` 只看本地状态。`doctor` 默认离线，`doctor --online` 和普通 `status` 仅查询，不隐式续期。查询显示的 reset 不能单独证明窗口已开始计时。

访问令牌到期时，显式运行 `login --refresh`。若状态为 `verification_pending`，运行 `login --verify` 复核已保存的新凭据；仍在等待回调时可用 `login --resume`。交换或续期结果不明时，用 `login --client-id YOUR_COMPATIBILITY_CLIENT_ID --reauthorize` 重新授权，不重发原请求。详细状态约束见 [认证与恢复](docs/architecture.md)。

兼容路径需要显式 client ID，不内置第三方注册默认值。这是独立社区工具，采用实验性的 Codex 兼容认证及后端协议，不会增加订阅额度，也没有平台对该路径或定时唤醒用途的公开支持承诺；使用仍受账号适用条款约束。

## 观测与自动发送

下面第一条持续只读观测；第二条才允许发送推理请求及必要的凭据续期。不要同时运行两条命令，同一目录只允许一个 watcher。

```sh
PYTHONPATH=src .venv/bin/python -m quota_window --config /your/private/config/settings.json watch --no-send
PYTHONPATH=src .venv/bin/python -m quota_window --config /your/private/config/settings.json watch --send
```

默认普通轮询 300 秒，临近已知边界时等待 reset 后 5 秒，再用至少间隔 2 秒的查询区分浮动与固定 reset。只有已确认账户、普通额度允许使用且 5h 窗口完整时才可能发送。额度显示 100% 不单独触发请求；已有活跃窗口会等待其边界。weekly 和额外 bucket 不触发发送。

`watch` 未指定模式时采用配置的 `send`；`--no-send` 强制只读，`--send` 只影响本次进程。持久开启需要在初次 `setup` 时显式使用 `--enable-send`，或主动将私有配置的 `send` 改为 true。配置自动发送也不会改变 `status` 和 `doctor` 的只读行为。

请求固定使用原型测过的 `gpt-6-luna`、`low`、`Say OK.` 配方，无历史和工具定义，不回退模型。这条兼容路径使用固定版本的 Codex 请求头及内部 lite 字段，尚无稳定公共接口保证。独立诊断曾测得 18 tokens；后续两轮自然边界分别测得输入 13/输出 6/合计 19，以及输入 13/输出 5/合计 18，缓存和推理 token 均为 0。这些是实测值，不是消耗上限。首次请求的 usage 仍未知，不能用第二次结果补填。usage 取自服务端完成事件，缺失值保持未知，输出不保存回复正文。

发送后在完成约 5 秒、35 秒处查询，要求 reset 固定在请求附近，才记录验证成功。进程中断、响应丢失或 usage 不完整都会保留发送意图并停止；重启不补发。网络或认证错误会退出，前台进程不自行重试。保留状态，不要通过删除文件来强制重试。

`--once` 只执行一轮观测，最多发送一次，不等待整个复核流程。初次观测通常不会发送；后续运行会继续分类或仅复核已完成请求。持续运行不需要这个选项。`--interval` 可调整普通轮询间隔，范围 2–3600 秒。

观测事件以脱敏 JSON 行写到标准输出，最近一次请求的 token 数及复核样本保存在私有 `watch/schedule.json`。可用 `diagnostics` 导出脱敏诊断；当前不提供完整历史导出或推理不确定状态的自动解除，登录恢复也不会清除推理发送标记。

## 诊断报告

`diagnostics` 只读取本地状态，可在 watcher 运行时执行，不联网、不续期、不发送请求，也不创建或修改状态。输出采用允许字段列表：运行环境、依赖版本、配置开关、认证阶段、窗口和最近一次请求的 usage。不会导出凭据、client/account/subject 标识、路径、attempt ID、任意错误消息或原始日志。配置开关不表示运行进程是否通过 `--send` 覆盖；报告也不是多文件一致性事务。

```sh
umask 077
PYTHONPATH=src .venv/bin/python -m quota_window --config /your/private/config/settings.json diagnostics > diagnostics.json
```

状态无法读取或格式不支持时，报告保留固定错误分类，便于分享，不复制损坏文件正文。诊断不会解除任何发送阻断。

## systemd 用户服务

`service` 只向标准输出生成用户服务文件，不安装或启动服务。服务引用当前解释器与 Python 包的绝对路径；开发态引用 checkout，安装态引用工具环境。移动 checkout 或工具环境后需要重新生成，临时目录不适合长期部署。解释器路径不能含引号、反斜杠，开发态源码路径不能含 `:`。

先生成强制只读的服务并检查。以下 `quota-window-dev.service` 是开发态示例文件名：

```sh
PYTHONPATH=src .venv/bin/python -m quota_window --config /your/private/config/settings.json service --no-send > /tmp/quota-window-dev.service
systemd-analyze --user --generators=no --man=no verify /tmp/quota-window-dev.service
```

核对文件、确认配置已登录且 `doctor --online` 通过后，可自行安装并启动这个只读服务：

```sh
mkdir -p ~/.config/systemd/user
install -m 600 /tmp/quota-window-dev.service ~/.config/systemd/user/quota-window-dev.service
systemctl --user daemon-reload
systemctl --user enable --now quota-window-dev.service
journalctl --user -u quota-window-dev.service -f
```

自动发送需重新以 `service --send` 生成文件并安装，再执行 `systemctl --user daemon-reload` 和 `systemctl --user restart quota-window-dev.service`。不传模式参数则服务每次启动采用配置中的 `send`。停止并禁用使用 `systemctl --user disable --now quota-window-dev.service`。服务器无活跃登录会话时，用户管理器是否持续运行由主机的 linger 设置决定；工具不修改该设置。

服务在可重试的传输错误后等待 300 秒重启，认证、状态和协议错误以退出码 2 停止，不进入重启循环。若传输错误发生在推理或续期期间，重启仍会检查持久状态；结果不明时停止，不重发原请求。日志保存在用户 journal，不写入凭据。
