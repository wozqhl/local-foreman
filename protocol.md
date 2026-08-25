# act / ask / apply

Local 干活。Coach 只出短指令。Ticket 不带整仓 dump。

## 状态

| 状态 | 谁 | 做什么 |
| --- | --- | --- |
| `act` | Worker（本地 Qwen3-8B / mock） | 选一个 tool 或 `done` / `unsure` |
| `ask` | Local → Coach | 只发一张 Ticket |
| `apply` | Local | 把 Coach 的 `instruction` 注入下一轮 Worker system prompt，再按 verdict 走 |

循环：`act` →（升级条件）→ `ask` → `apply` → `act`。`halt` 在 `apply` 结束。

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
  "failed_steps": ["short log", "max 3"],
  "proposed_next": "string",
  "risk": "write|push|spend|none",
  "local_guess": "string"
}
```

校验：`failed_steps` 最多 3 条，每条截断；必须有 `goal`；`risk` 只能是四个枚举。

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

Local **必须**把 `instruction` 写进下一轮 Worker system prompt。

## Tools v1

`read` / `write` / `shell`。只读 git（`status` `log` `diff` …）可在 `act` 执行。
