# 安全政策

## 支持的版本

当前维护 `0.1.x`。

## 报告漏洞

请不要在公开 issue / PR 里贴可复现的攻击步骤。

用 GitHub Security Advisory（仓库发布后）或通过维护者 GitHub 账号私信联系 **Shaffer Wang**，说明：

- 影响的版本 / commit
- 实际危害（本地任意命令、密钥泄漏、未授权远端写入等）
- 复现所需的最小条件（不要附带完整 exploit）

我们会确认后修复，并在 [CHANGELOG.md](CHANGELOG.md) 记录。

## 项目自身的安全边界

Local Foreman 会在用户指定的工作根目录执行 `read` / `write` / `shell`。

- 默认 **不会** 自动 `git push`、改 remote、或调用付费 API。
- 这些动作必须先进入 `ask`，由教练 `continue` / `revise` / `halt`。
- `halt` 必须停止循环。
- Smoke 与 CI 使用 mock Worker / Coach，不接触真实密钥或权重下载。

如果你发现 `needs_ask` 漏拦了危险命令，这属于安全问题，请按上面的方式报告。

## 密钥

`COACH_API_KEY` 只应放在环境变量或未入库的 `.env`。不要把它写进 ticket、日志或 README 示例的真实值。
