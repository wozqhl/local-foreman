# act / verify / ask / apply

产品要同时追三件事（**尚未用数字证明已达成**）：比只用远端大模型更快、质量相当、token 显著更少。做法是三条风险车道，而不是每一步都问大模型。

闭环：本地干活 → 按车道留下 / 核对草稿 / 说清问题再问 → 按指示继续。空转永远不问教练。

## 三条车道（先路由，再级联）

路由按 **工具种类**，不是只看 worker 的 raw confidence（FrugalGPT / RouteLLM 式 cascade）：

| 车道 | 何时 | 教练 | 状态 |
| --- | --- | --- | --- |
| LOW | `read`、git 只读、空转想法 | 不问（0 token） | 留在 `act` |
| MID | 本地拟好的 `write`（非 `.git`） | `verify` 短票，教练不重写文件 | `verify` |
| HIGH | 原四条：同工具连败两次、即将 mutate git / remote / `git push`、用户 review、Worker `unsure` | `ask`（stuck ticket） | `ask` |

级联与从文献里偷来的约束（不是已证明的成绩）：

- **动作级核对要的是教练等价结果，不是 token 一字不差**（Leviathan / Speculative Actions / ISP）。accept 表示「教练自己也会走这一步」，不是字符串相等。
- **不要每笔 write 都核对**：滚动 accept 率已经高时，跳过 verify，直接落盘（DSP）。率低（< 0.5）则下一笔 write 升到 `ask`（speculation tax）。
- **一次工具失败先本地自修**（EcoAssistant），不要立刻 HIGH ask；**两次失败才 ask**。一次失败也可以进 MID 核对。
- **raw confidence 不是唯一门**（AutoMix）。路由先看工具种类（读留下、写核对），再叠加本地检查 / 校准率。
- 有本地检查且通过（CRITIC：例如 `.py` 干跑 `ast.parse`）则跳过 verify，直接落盘。

`git push` / remote write **必须**先 `ask`，禁止在 `act` 里直接执行。空转本身不问教练；空转动手仍走 `act` + HIGH 四条，MID verify 会跳过。

## 无损暂扣（lossless hold）

待写入的 draft **在教练 `accept` 之前不得落盘**。`revise` / `halt` **丢弃**草稿。这是 speculative decoding / Speculative Actions 在动作层：本地先起草，教练只验收。

等待 `verify` 或 `ask` 时，本地只允许预跑 `read` / git 只读。**禁止**投机写文件（Speculative Actions Assumption 2）。

## 状态

| 状态 | 谁 | 做什么 |
| --- | --- | --- |
| `act` | Worker（本地 Qwen3-8B / mock） | 选一个 tool 或 `done` / `unsure` |
| `verify` | Local → Coach | 发 aider 风格的 draft 票，不是 stuck 问题票。看板：**核对中**（不得写成求助中） |
| `ask` | Local → Coach | 只发一张带 `problem` 的 Ticket。看板：求助中（正在咨询大模型） |
| `apply` | Local | 按 verdict 走：ask 的 continue/revise/halt；verify 的 accept/revise/halt |
| `idle` | Worker（本地） | 附加状态。短 `thought`，指数退避。空转本身不打教练 |

循环：`act` →（low 留下 \| verify \| ask）→ `apply` → `act`。`halt` 在 `apply` 结束。`idle` 不进入这条闭环。

事件：`work` / `stuck` / `asked_coach` / `coach_instruction` / `resumed` / `thought` / `retrieved` / `idle_act` / `verified_coach` / `coach_verdict` / `lesson`。`verified_coach` **不计入** `asked_coach`。`lesson` 是 Reflexion 的一行教训（revise 时追加），retrieve 可以捡回来。`coach_verdict` 带 `(conf, act, verdict)`，留给以后 EAGLE-2 校准，不当当场的唯一门。

## Verify 票（MID，不是 stuck）

```json
{
  "kind": "verify",
  "goal": "string",
  "claim": "one sentence: what we did / will do",
  "draft": "path + truncated unified-diff or excerpt",
  "risk": "write|none"
}
```

Coach 回复：`{"verdict":"accept|revise|halt","instruction":"1-2 sentences"}`。

- `accept`：把暂扣的 write 落盘。教练不重写文件。
- `revise`：丢弃草稿，注入 instruction，写一条 `lesson`，回到 `act`。
- `halt`：丢弃草稿，停。

`LOCAL_FOREMAN_MAX_VERIFIES` 硬限制 `verified_coach`。超过则跳过核对、留在本地（可自己落盘）。未设置不设上限。

## 轨迹

一条 append-only jsonl。路径：`LOCAL_FOREMAN_TRAJ`，默认 `<cwd>/.local-foreman/traj.jsonl`。进程重启后接着写。

每条含：`ts` `seq` `kind` `message` `goal`，以及当时的 tool observation / ticket / coach reply（若有）。

- `local-foreman ui` 默认打开 persist + idle，看板会慢慢长出心思日志。看板可下载同一条 jsonl（`/traj`）。
- 一次性 CLI 默认仍是任务驱动；`--persist` 或 `LOCAL_FOREMAN_PERSIST=1` 才写盘并空转。
- `local-foreman traj` / `python -m local_foreman traj` 读同一条 jsonl：默认 cat，`--last N` 只看最近，`--kind thought,idle_act,retrieved` 过滤，`--out path` 导出。`--stats` 统计本文件上的 ask / 教练回复。不另起一份日志。
- 教练用量：`asked_coach` / `coach_instruction` 是 HIGH。`verified_coach` / `coach_verdict` 另计，**不是** ask。空转想法不计次。`--stats` 可报 `verify_accept_rate`。`COACH_USD_PER_ASK` 未设置时只报次数，不估美元。
- `LOCAL_FOREMAN_MAX_ASKS` 硬限制 `asked_coach`。超过则跳过 ask，留在本地，绝不调用教练。
- `LOCAL_FOREMAN_MAX_VERIFIES` 硬限制 `verified_coach`。超过则跳过核对，留在本地。

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

`python -m local_foreman ui` 在 `127.0.0.1:8765` 用 stdlib HTTP + SSE 展示：目标、当前状态、最后问题、教练指示、心思、教练用量、事件日志。中文状态：干活中 / **核对中** / 求助中（正在咨询大模型） / 已收到指示 / 继续 / 空转中 / 自己在想 / 展开原文 / 空转动手。核对中、空转、展开原文、空转动手都不得写成正在咨询大模型。核对次数不计入 ask。

## 对照台（mock only）

`python -m local_foreman bench` 在同一组夹具上比 local-foreman（三条车道）和 remote-only（每步都问教练）。记录墙钟（注入 worker 5ms / verify 40ms / ask 80ms）、asks、verifies、token 代理（asks+verifies）、夹具通过率。质量在这里就是同一批 mock 任务的通过率，**不是**真实模型分数。不宣称三项目标已达成。

## Related

只列本协议真正用到的工作，不扩写、不发明链接：

- Leviathan speculative decoding — https://arxiv.org/abs/2211.17192
- Speculative Actions (Ye et al.) — https://arxiv.org/abs/2510.04371
- Interactive Speculative Planning — https://arxiv.org/abs/2410.00079
- Dynamic Speculative Planning — https://arxiv.org/abs/2509.01920
- FrugalGPT — https://arxiv.org/abs/2305.05176
- AutoMix — https://arxiv.org/abs/2310.12963
- RouteLLM — https://github.com/lm-sys/RouteLLM
- EAGLE-2 — https://arxiv.org/abs/2406.16858
- aider architect/editor — https://aider.chat/2024/09/26/architect.html
- CRITIC — https://arxiv.org/abs/2305.11738
- Reflexion — https://arxiv.org/abs/2303.11366
- EcoAssistant — https://arxiv.org/abs/2310.03046

## Tools v1

`read` / `write` / `shell`。只读 git（`status` `log` `diff` …）可在 `act` 执行。
