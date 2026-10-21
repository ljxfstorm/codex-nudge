# 认证与恢复

工具拥有自己的私有目录，不读取 Codex、SIWC 或其他程序的凭据，也不调用它们的进程。每次认证操作由 `StateStore` 持有进程锁，覆盖读取、写入意图、网络请求和结果落盘，避免并发消费一次性授权码或 refresh token。存储使用原子替换和 fsync；HTTP 不自动重试或跟随重定向。

`identity.py` 用 PyJWT 及其 crypto 依赖验证 ID token 的签名、issuer、audience、有效期和初次登录的 nonce。密钥地址限于可信 issuer。使用成熟验签库，避免自行实现密码学；其余 HTTP、存储、CLI 使用标准库。access token 不作为身份声明来源，其中未验证的 exp 仅作到期提示。

`auth.py` 管理浏览器 PKCE 和凭据状态。授权链接可在另一台机器打开，完整 localhost 回调通过隐藏终端输入交回；工具不监听端口。授权只申请 openid、profile、email、offline_access。用户显式传入兼容 client ID；该路径的技术验证不代表官方公共接口承诺或注册许可。

登录在 `login.json` 中从 `waiting` 进入 `exchange_pending`，随后才发送一次授权码交换。成功保存经验证的新凭据后记录 `complete`，清除临时 verifier、state、nonce。拒绝授权记录 `denied`。中断或交换响应丢失时保留 `exchange_pending`，禁止重发，需重新授权；只有仍在等待回调的登录才能继续提交回调。

凭据在 `credentials.json` 中有三个阶段：

| 阶段 | 含义 | 可执行操作 |
| --- | --- | --- |
| `ready` | 已验证身份，并核对额度响应的账户 | 查询、显式续期或重新授权 |
| `refresh_pending` | 已记录续期意图，但没有完成保存新结果 | 重新授权；禁止重发续期，也不能用旧 access token 查询来解除 |
| `verification_pending` | 新结果已验证身份并保存，尚未确认额度账户 | 仅 GET 复核，无需再次交换或续期 |

续期先持久化 `refresh_pending`，再 POST 一次。新响应通过身份和账户绑定校验后，保存为 `verification_pending`，然后 GET 额度核对账户，成功才进入 `ready`。响应丢失、验签依赖的网络请求失败或落盘失败都可能留下不确定状态；即使实际上未消费 token，也不猜测或重发。重新授权必须保留同一 client、subject 和账户；切换账户需要单独目录。

`quota.py` 保留 bucket、时长和未知字段的语义，不把 weekly 或额外 bucket 当作缺失的 5h。额度耗尽不等于登录无效。查询成功也不证明窗口已经固定锚点，自动发送和锚点复核尚未迁入。

本地状态输出省略 token、subject 和账户 ID。现有测试使用临时目录与合成凭据；现阶段不接管原型服务。
