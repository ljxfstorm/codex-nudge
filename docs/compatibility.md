# 兼容性与使用边界

Codex Nudge 是独立社区工具。它使用实验性的 Codex 兼容认证、额度查询和 backend Responses 协议，没有平台对这些第三方用法的公开稳定接口保证。服务变更可能使工具停止工作。

## 兼容认证

工具自行执行浏览器 PKCE 登录，独立保存凭据，不调用或读取 Codex。当前兼容实验使用的公开 client ID 为 `app_EMoamEEZ73f0CkXaXp7hrann`，用户需在 `setup --client-id` 中显式选择；它来自[官方 Codex 登录实现](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/login/src/auth/manager.rs)，不是本项目获发的 OAuth 注册，也不是秘密凭据。

公开 client ID 和开源实现不等于独立应用获得注册或平台许可。另一条有文档的 [Sign in with ChatGPT 开源应用流程](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)使用自己的注册；其[推理说明](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)指定公开 Responses endpoint，不应把该流程的凭据指向 ChatGPT backend-api。两条路径不能混为一谈。

## 额度和自动发送

首版仅对当前实测的普通 18000 秒窗口自动发送。它不增加额度、不绕过耗尽限制、不切换账户或模型重试。额度查询本身不代表窗口开始；工具通过浮动 reset、发送前复查和发送后两次固定 reset 样本判断。

已实测独立登录、一次凭据轮换及连续两个自然窗口；推理单次为 18–19 tokens。更长运行与故障反馈进入正常维护，不作为等待无限期的发布门槛。实测不构成所有套餐、weekly 或永久消耗上限的保证。

## 条款与代码许可

使用仍受账号适用的[个人服务条款](https://openai.com/policies/row-terms-of-use/)或相应地区/订阅协议约束；条款包含自动化、访问限制与保护措施的约束。所核对公开资料没有专门说明本工具的到期唤醒组合用途，不能据此保证符合条款，也不能把技术成功视为许可。

本项目 MIT 许可仅处理本项目代码的使用与分发，不授予 OpenAI 在线服务、OAuth 注册、商标或订阅权益。工具不会用替换端点、账号或重复发送来规避拒绝；结果不明时保留记录并停止。
