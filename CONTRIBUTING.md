# 开发约定

## 工作方式

main 保持可安装、可运行。使用短期分支，每项修改解决一个具体问题。同一会话完成的修改在审阅 diff 和通过验证后可直接合入 main；需要独立审阅时使用 PR。

日常实现直接推进。改变自动发送语义、认证路径、增加依赖或扩大产品范围时，先说明必要性及取舍。按问题复杂度记录决定，不强制每个修改都建 issue、ADR 或设计文档。

## Agent 开发

AGENTS.md 作为入口，引用本文件；规则只维护一份。环境准备和验证命令维护在这里，保持新 checkout 能复现。

开始任务时查看分支、工作区与相关实现，确认变更范围。收尾时检查完整 diff，报告行为变化、已运行验证及未解决限制。使用多个 agent 时明确文件责任，并保持彼此修改。

实现跨越模块的窗口状态机或认证恢复后，再补一份简短架构说明，记录状态转换、模块边界和关键约束；已有的测试、配置与 CLI 帮助直接引用，不复制成多份规范。只有出现可重复的专门工作流时才增加 skill 或脚本。

## 提交

- 每个 commit 完成一个可独立审阅、可回退的变化；相关实现、测试和文档一起提交。
- 分开提交无关重构、格式修改与功能变化；不把一项行为拆成暂时破坏程序的多个提交。
- 提交前检查暂存 diff，逐项添加文件，保留他人的未提交工作。
- 消息遵循 Conventional Commits：`type(scope): description`，scope 可省略。新增行为用 `feat`，缺陷修复用 `fix`，文档用 `docs`，仓库维护用 `chore`；scope 仅在有助于定位时添加。description 使用简短、平实的英文祈使句，说明具体变化；完整标题以约 50 字符为目标，可为清楚表达适当放宽。
- 标题足够说明变化时仅写标题；必要的正文解释原因、限制或取舍，不重复 diff。破坏性变更用 `!` 标记，并在正文或 `BREAKING CHANGE:` footer 说明影响与迁移。

示例：`fix(storage): reject unsupported state versions`、`fix(service): preserve literal dollars in arguments`、`feat(watch): record token usage for wake requests`。避免 `Update code`、`Various improvements` 或宣传性描述。

## 验证

从仓库根目录准备开发环境并运行测试：

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
```

当前实现要求 Python 3.10 或更新版本，已在 Python 3.10 和 3.13 验证。测试只使用临时目录、合成数据和本地子进程，不访问账户或网络；身份测试使用真实 RSA 签名，不跳过加密依赖。运行依赖只维护在 pyproject.toml，精确解析结果提交为 uv.lock。修改依赖后运行 `uv lock` 并审阅锁文件；普通使用和 CI 用 `--locked`，避免隐式改变依赖。

两项 systemd 原生验收默认跳过，需要 Linux 用户管理器和访问它的权限时单独运行：

```sh
QUOTA_WINDOW_SYSTEMD_TESTS=1 uv run --locked python -m unittest discover -s tests -p test_service.py -v
```

验收先由 `systemd-analyze` 解析服务文件，再临时链接并启动唯一命名的一次性测试单元，核对实际 argv 和 PYTHONPATH，最后停止、删除运行时链接。该单元只运行合成 Python 模块，不调用账户或推理，也不修改现有服务文件；不要把它误认为生产服务启动测试。

修复缺陷先复现，再验证修复。调度、认证、持久状态及恢复逻辑需要自动化回归测试。常规测试使用假凭据和假传输，离线执行；真实请求实验独立记录版本、usage 及窗口证据。

CI 在 Linux/Python 3.10 和 3.13 上从锁文件准备环境，运行离线测试和 CLI 冒烟检查；账号与原生 systemd 实测独立进行。发布前从干净源码副本运行相同流程，并检查服务文件中的绝对路径。

## 数据与依赖

使用独立的开发配置、凭据和状态；运行中的用户服务不作为测试环境。凭据、真实日志和状态不进入 Git 或测试夹具。诊断输出默认去标识。

优先使用现有语言和标准库，新增依赖要有具体收益。体积测量包含解释器和运行依赖；内存测量包含完整服务进程。根据实际问题拆模块，不预建插件框架、通用多后端接口或多账号架构。

## 发布

采用 SemVer，代码版本、tag 与 Release 对应同一源码提交；0.x 的破坏性变化也写迁移说明。首版发布源码与变更说明，用户切换到固定 tag 后用 `uv sync --locked` 准备环境，无需维护 wheel/sdist 发行资产或 PyPI 发布。pyproject.toml 的最小构建声明仅用于 uv 本地安装 CLI。

仓库文档保留使用方式、架构约束、兼容性边界和必要的验证结论。逐轮实验、失败排查、候选方案比较与原始测量归档到仓库外，不提交到本仓库；迭代修改的目的通过原子化 commit 消息说明。
