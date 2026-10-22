# Changelog

## 0.1.0 — Unreleased

首个实验性版本，名称为 codex-nudge。

- 单账号 Linux CLI，独立浏览器 PKCE 登录和续期，无需安装 Codex。
- 默认只读，显式开启后观测普通 5h 窗口，在确认空闲时发送一次低消耗请求。
- 持久化发送意图；结果不明时阻止补发；保存实际 usage 并复核固定 reset。
- 私有配置、脱敏离线诊断导出及 systemd 用户服务生成。
- MIT 许可证；获取仓库后通过 uv 和锁文件运行，发布版本 tag 与源码。

使用实验性的 Codex 兼容认证及后端协议，无公开稳定接口保证。只验证过当前账户的普通 5h 行为；weekly、其他套餐及额外 reset 不触发发送。实测 18–19 tokens 不是消耗上限。技术可用与代码许可不代表平台对本用途的授权，详见[公开采用边界](docs/compatibility.md)。
