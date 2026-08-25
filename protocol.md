# act / ask / apply

Local 干活。遇到问题先把问题说清楚，再问 Coach。Coach 只出短指令。Ticket 不带整仓 dump。

闭环：本地模型干活 → 说清当前问题 → 询问大模型 → 大模型给出指示 → 本地模型带着指示继续干活。

空转思考是附加的：没人催、也没升级条件时，本地模型自己想一句，写进同一条轨迹。**空转永远不问教练。**

## 状态

| 状态 | 谁 | 做什么 |
| --- | --- | --- |
| `act` | Worker（本地 Qwen3-8B / mock） | 选一个 tool 或 `done` / `unsure` |
| `ask` | Local → Coach | 只发一张带 `problem` 的 Ticket |
| `apply` | Local | 把 Coach 的 `instruction` 注入下一轮 Worker system prompt，再按 verdict 走 |
| `idle` | Worker（本地） | 附加状态。短 `thought`，指数退避。可挑一个本地小动作再进 `act`。空转本身不打教练 |

循环：`act` →（升级条件）→ `ask` → `apply` → `act`。`halt` 在 `apply` 结束。`idle` 不进入这条闭环。

事件日志（一条 append-only jsonl，看板 SSE 与轨迹共用）：`work` / `stuck`（带 problem） / `asked_coach` / `coach_instruction` / `resumed` / `thought`（空转独白） / `retrieved`（展开压缩摘要） / `idle_act`（空转选中的本地小动作）。

## 何时升级（仅这些）

1. 同一工具连续失败两次
2. 即将 mutate git 仓库或对 remote 写入（含 `git push`、`gh`、改 remote、写 `.git/`）
3. 用户明确要求 review
4. Worker 发出 `unsure`

`git push` / remote write **必须**先 `ask`，禁止在 `act` 里直接执行。

空转思考 **不是** 第五条升级条件。Idle thinking MUST NEVER call the coach。空转里如果要动工具，仍走 `act` + 上面四条，并记一条 `idle_act`。

## 轨迹

一条 append-only jsonl。路径：`LOCAL_FOREMAN_TRAJ`，默认 `<cwd>/.local-foreman/traj.jsonl`。进程重启后接着写。

每条含：`ts` `seq` `kind` `message` `goal`，以及当时的 tool observation / ticket / coach reply（若有）。

- `local-foreman ui` 默认打开 persist + idle，看板会慢慢长出心思日志。看板可下载同一条 jsonl（`/traj`）。
- 一次性 CLI 默认仍是任务驱动；`--persist` 或 `LOCAL_FOREMAN_PERSIST=1` 才写盘并空转。
- `local-foreman traj` / `python -m local_foreman traj` 读同一条 jsonl：默认 cat，`--last N` 只看最近，`--kind thought,idle_act,retrieved` 过滤，`--out path` 导出。`--stats` 统计本文件上的 ask / 教练回复。不另起一份日志。
- 教练用量只数 `asked_coach` 与 `coach_instruction`。空转想法不计次。`COACH_USD_PER_ASK` 未设置时只报次数，不估美元。

新的用户目标，或进入 `ask`，会把空转退避重置回起始间隔（约 5s，可配，加倍直到上限）。

## 分层压缩（只在本地）

最近的轨迹原文进入 Worker 上下文；更老的按层摘要。摘要只复述已有的 `kind` + `message`，不编造记忆。原始 jsonl 不改写。摘要带 `first_seq` / `last_seq`，可从同一条 jsonl 取回原文，注入 Worker 上下文，并记一条 `retrieved`。压缩和展开都不走远程教练。

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

`python -m local_foreman ui` 在 `127.0.0.1:8765` 用 stdlib HTTP + SSE 展示：目标、当前状态、最后问题、教练指示、心思、教练用量、事件日志。中文状态：干活中 / 求助中（正在咨询大模型） / 已收到指示 / 继续 / 空转中 / 自己在想 / 展开原文 / 空转动手。空转、展开原文、空转动手都不得写成正在咨询大模型，也不计入教练次数。

## Tools v1

`read` / `write` / `shell`。只读 git（`status` `log` `diff` …）可在 `act` 执行。
