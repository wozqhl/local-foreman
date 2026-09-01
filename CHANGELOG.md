# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/)，版本号按 SemVer。

## [0.4.1] — 2026-09-01

### Added

- 用户确认钩子：HIGH `apply` 且教练 `continue`（不是 halt）、升级原因是 git mutate / remote / `git push` 时，恢复 act 前再确认一次。默认 TTY 开启；`LOCAL_FOREMAN_CONFIRM=0` / `--no-confirm` 跳过。可注入 `confirm(prompt) -> bool`，smoke 不挡 stdin。拒绝记 `user_denied`，当 halt，不执行 git/remote，不再问教练。同意走原来的 continue。看板「待确认」。
- Smoke：`confirm-ok`（mock HIGH git-push：callback False 不逃沙箱、不 push；True 与今日 continue 相同）。
- README 顶部改为陌生人 30 秒 mock 跑通，协议细节下移。不宣称三项目标已达成。

## [0.4.0] — 2026-09-01

### Added

- 工作区沙箱：`root` 设定时 `read` / `write` / `shell`（及 `draft_diff`）经 `resolve_under_root` 限制在工作区根内；越界硬失败（`sandbox:` 前缀，`escalated=False`），不问教练。shell 对绝对路径、带 `..` 的相对路径、简单 `cd` 与常见重定向做 pragmatic 检查；cwd 仍为 root。
- 更细的 git 只读白名单：`git remote -v|show|get-url`、`git config --get|--list|-l`、`git stash list|show`、`git tag`/`-l`/`--list`、`git worktree list`、只读 `git branch` 不计入 mutate / needs_ask；push/commit/config 写入/remote add 等仍升级。
- Smoke：`sandbox-ok`、`git-ro-ok`。原有 token 保留。不宣称三项目标已达成。

## [0.3.0] — 2026-08-31

### Added

- MLX 模型加载进度与失败重试：`MlxWorker` 可注入 `loader` / `on_load`；默认 stderr 中文进度（加载中 / 重试 / 成功 / 失败）；`LOCAL_FOREMAN_LOAD_RETRIES`（默认 3）与 `LOCAL_FOREMAN_LOAD_RETRY_SLEEP`（默认 1，smoke 置 0）控制重试。看板映射 `加载中` / `重试加载`，不计入教练 ask。
- Smoke：`load-retry-ok`（假 loader 失败两次后成功、始终失败、ImportError 提示；不 `step`、不装 mlx-lm、不打教练）。原有 token 保留。不宣称三项目标已达成。

## [0.2.9] — 2026-08-28

### Changed

- MLX / fallback 多轮上下文改成 chat turns：`history_to_chat_turns` + `build_chat_messages` 把 loop 的 `action`/`result` 与 traj 的 kind/message 拆成 assistant/user，再交给 `apply_chat_template`（无模板时用带角色标记的纯文本）。不再把整段 history `json.dumps` 塞进一条 user。不 load 权重、不打教练。
- Smoke：`chat-turns-ok`。原有 token 保留。不宣称三项目标已达成。

## [0.2.8] — 2026-08-27

### Added

- `strip_thinking`: 剥掉 Qwen3 `<think>` / `<thinking>` / `<redacted_reasoning>` 块（含模板只留下的 `</think>`），再交给 `parse_action`。不编造内容；剥完仍不是 JSON 就走原来的 unsure。mock 与 MLX 共用。
- 可配置 MLX `max_tokens` / 采样：`LOCAL_FOREMAN_MAX_TOKENS`（默认 512）、可选 `LOCAL_FOREMAN_TEMP` / `LOCAL_FOREMAN_TOP_P`（未设置不传 sampler，保持原 generate 行为）、CLI `--max-tokens`。
- Smoke：`think-strip-ok`（mock only；含 thinking 夹具解析，以及 env 读入 MlxWorker / factory，不 `load`、不打教练）。原有 token 保留。不宣称三项目标已达成。

## [0.2.7] — 2026-08-26

### Added

- EAGLE-2 滚动校准：同一条 traj jsonl 上用最近 `coach_verdict` 的 `(conf, act, verdict)` 估 P(accept | conf_bucket, act_type)。样本够（≥8）且 P≥0.9、又不是 git-mutate，则跳过 verify，留在 LOW（已检查才落盘，否则 skip hold）。校准与 raw conf 长期分歧时，只在原有 HIGH 四条命中才升 ask，不另造原因。样本不足仍走 DSP 0.75 skip 与 tax <0.5。
- Smoke：`calibrate-ok`。原有 token 保留，仍只走 mock。无损暂扣、AutoMix 自核、demo 缓存、MAX_ASKS / MAX_VERIFIES 保持。不宣称三项目标已达成。

## [0.2.6] — 2026-08-26

### Added

- AutoMix 本地自核：发 verify/ask 之前，worker 或廉价检查给 pending claim 打 p。p 很低两次且不是升级条件则不把无望的活送给教练烧 token；p 高且已有 CRITIC 检查则留在 LOW。
- EcoAssistant 本地 demo 缓存：`verify` accept（文件落盘）后把 `{goal/task_sketch, claim, path, draft excerpt}` 追加到 `.local-foreman/demos.jsonl`（或 `LOCAL_FOREMAN_DEMOS`）。以后 path/goal 相近的 write 注入 1–2 条到 worker system prompt。只存本地，不存教练改写。
- Smoke：`self-verify-ok`、`demo-ok`。原有 token 保留，仍只走 mock。无损暂扣、工具种类路由、DSP skip、speculation tax、MAX_ASKS / MAX_VERIFIES 保持。

## [0.2.5] — 2026-08-26

### Added

- 三条风险车道：LOW 留下 / MID `verify`（无损暂扣 write） / HIGH 原四条 `ask`。路由按工具种类，不以 raw confidence 为唯一门。
- Verify 票是 aider 风格的 path + 截断 unified-diff，不是 stuck 问题票。看板「核对中」，不计入 `asked_coach`。
- 等待核对/询问时只允许预跑 read / git-ro，绝不投机写。
- CRITIC：本地 `.py` 干跑通过则跳过 verify。滚动 accept 率高则不再每笔 write 都核对（DSP）；< 0.5 时下一笔 write 升到 ask。revise 写一行 `lesson`（Reflexion）。`coach_verdict` 记 `(conf, act, verdict)` 供以后 EAGLE-2 校准。
- mock 对照台 `python -m local_foreman bench`：墙钟 / asks+verifies / 夹具通过率。不宣称三项目标已达成。
- Smoke：`verify-ok`、`bench-ok`。原有 token 保留，仍只走 mock。

## [0.2.4] — 2026-08-26

### Added

- 硬上限 `LOCAL_FOREMAN_MAX_ASKS`：同一条 traj 上再问一次会超过上限时，跳过 ask，留在本地（空转或带原因 halt），绝不调用教练。未设置则不设上限。
- Smoke 新 token：`max-ask-ok`。原有 token 保留，仍只走 mock。

## [0.2.3] — 2026-08-26

### Added

- 本机统计教练用量：同一条 traj jsonl 上数 `asked_coach` / `coach_instruction`。空转 `thought` / `idle_act` / `retrieved` 不计次。
- `local-foreman traj --stats` 打印 `asks=` / `replies=`。只有设置了 `COACH_USD_PER_ASK` 才额外给出 `estimated_usd`（默认不估美元）。
- 看板增加「教练用量」卡片，数字来自同一条轨迹。
- Smoke 新 token：`ask-cost-ok`。原有 token 保留，仍只走 mock。

## [0.2.2] — 2026-08-26

### Added

- `local-foreman traj` / `python -m local_foreman traj`：读循环写下的同一条 jsonl。`--last N`、`--kind thought,idle_act,retrieved`、`--out path`。不另起格式。
- 看板可下载同一文件（`GET /traj`）。
- Smoke 新 token：`traj-cli-ok`。原有 token 保留，仍只走 mock。

## [0.2.1] — 2026-08-26

### Added

- 按 seq 展开压缩摘要：同一条 jsonl 取回原文，注入 Worker 上下文。记 `retrieved`。不走教练。
- 空转可挑一个本地小动作（read/write/shell），走现有 `act` 和四条升级条件。记 `idle_act`。空转不问教练。
- 看板同一条心思日志显示 `retrieved` / `idle_act`（展开原文 / 空转动手）。
- Smoke 新 token：`retrieve-ok`、`idle-act-ok`。原有 token 保留，仍只走 mock。

## [0.2.0] — 2026-08-26

### Added

- 一条 append-only 轨迹 jsonl（`LOCAL_FOREMAN_TRAJ`，默认 `.local-foreman/traj.jsonl`）。现有事件加上新的 `thought`，与看板 SSE 共用同一份日志，重启还能接着写。
- 空转本地思考 + 指数退避：没 Ticket、没在跑工具时，本地 Worker 写一句短独白。不问教练。新目标或进入 `ask` 会重置间隔。`ui` 默认 persist+idle；一次性 CLI 仍任务驱动，除非 `--persist` / `LOCAL_FOREMAN_PERSIST=1`。
- 分层压缩：最近原文，更老的本地摘要。不编造记忆，不改写 jsonl，不走远程教练。
- 看板中文状态增加：空转中 / 自己在想。空转不会标成正在咨询大模型。
- Smoke 新 token：`traj-ok`、`idle-ok`、`compact-ok`。原有 token 保留，仍只走 mock。

## [0.1.1] — 2026-08-25

### Added

- Ticket 增加必填 `problem`：一句话说清失败了什么、试过什么、需要什么。不再把现场当 dump 扔给教练。
- 事件日志：`work` / `stuck` / `asked_coach` / `coach_instruction` / `resumed`。`apply` 后 Worker 必须带着教练指示继续。
- 本机看板：`python -m local_foreman ui`（stdlib HTTP + SSE，`127.0.0.1:8765`）。中文状态：干活中 / 求助中（正在咨询大模型） / 已收到指示 / 继续。
- mock 演示：读 README → 假升级 → 教练 continue → 继续干活，无需 API key。
- Smoke 新 token：`problem-ok`、`ui-ok`。原有 `act-ok` / `ask-ok` / `apply-ok` / `oss-ok` 保留。

## [0.1.0] — 2026-08-25

### Added

- 真实 `MlxWorker`：默认 `mlx-community/Qwen3-8B-4bit`，可由 `LOCAL_FOREMAN_MLX_MODEL` 覆盖；首次 `step` 才 `load`，用 chat template 生成，再走 `parse_action`。
- 缺失 mlx-lm 时给出明确错误：在 Apple Silicon 上执行 `pip install 'local-foreman[mlx]'`。
- 硬化后的 `OpenAICoach`：10s 超时、`Accept: application/json`、`User-Agent: local-foreman/0.1.0`，脏 JSON 与 Worker 同一套解析，并 `validate_reply`。任意 OpenAI 兼容 `base_url`（DeepSeek、OpenRouter 等）。
- 产品 CLI：`python -m local_foreman "goal"` / `local-foreman`，`--worker mock|mlx`，`--coach mock|openai`，打印状态迁移，`halt` 非 0 退出。
- OSS 文件：Apache-2.0 `LICENSE`、`NOTICE`、`CONTRIBUTING.md`、`SECURITY.md`、`ROADMAP.md`、本 changelog。
- `.github/workflows/smoke.yml`：Ubuntu + Python 3.11，仅 mock smoke。

### Notes

- Smoke 仍只使用 mock Worker / Coach，不下载权重、不调用真实 API。
- 本版本不是 Cursor / Grok Bot 的克隆。
