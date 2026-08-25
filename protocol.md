# act / ask / apply

Local 干活。遇到问题先把问题说清楚，再问 Coach。Coach 只出短指令。Ticket 不带整仓 dump。

闭环：本地模型干活 → 说清当前问题 → 询问大模型 → 大模型给出指示 → 本地模型带着指示继续干活。

## 状态

| 状态 | 谁 | 做什么 |
| --- | --- | --- |
| `act` | Worker（本地 Qwen3-8B / mock） | 选一个 tool 或 `done` / `unsure` |
| `ask` | Local → Coach | 只发一张带 `problem` 的 Ticket |
| `apply` | Local | 把 Coach 的 `instruction` 注入下一轮 Worker system prompt，再按 verdict 走 |

循环：`act` →（升级条件）→ `ask` → `apply` → `act`。`halt` 在 `apply` 结束。

事件日志：`work` / `stuck`（带 problem） / `asked_coach` / `coach_instruction` / `resumed`。

## 何时升级（仅这些）

1. 同一工具连续失败两次
2. 即将 mutate git 仓库或对 remote 写入（含 `git push`、`gh`、改 remote、写 `.git/`）
3. 用户明确要求 review
4. Worker 发出 `unsure`

`git push` / remote write **必须**先 `ask`，禁止在 `act` 里直接执行。

## Ticket（Local → Coach）

```json
{
  "goal": "string",
  "problem": "one sentence: what failed, what was tried, what we need",
  "failed_steps": ["short log", "max 3"],
  "proposed_next": "string",
  "risk": "write|push|spend|none",
  "local_guess": "string"
}
```

校验：必须有 `goal`；必须有清楚的 `problem`（写明失败了什么、试过什么、需要什么）；`failed_steps` 最多 3 条，每条截断；`risk` 只能是四个枚举。不要整仓 dump。

## Coach 回复

```json
{
  "verdict": "continue|revise|halt",
  "instruction": "1-2 sentences",
  "next_tool": "optional"
}
```

- `continue`：回到 `act`，带着 instruction。不自动执行被拦住的 remote。
- `revise`：回到 `act`，换方案；instruction 必注入。
- `halt`：停。

Local **必须**把 `instruction` 写进下一轮 Worker system prompt（`## Coach instruction (must follow)`）。

## 本机看板

`python -m local_foreman ui` 在 `127.0.0.1:8765` 用 stdlib HTTP + SSE 展示：目标、当前状态、最后问题、教练指示、事件日志。中文状态：干活中 / 求助中（正在咨询大模型） / 已收到指示 / 继续。

## Tools v1

`read` / `write` / `shell`。只读 git（`status` `log` `diff` …）可在 `act` 执行。
