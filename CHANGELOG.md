# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/)，版本号按 SemVer。

## [0.2.5] — 2026-08-26

### Added

- 三条风险车道：LOW 留下 / MID `verify`（无损暂扣 write） / HIGH 原四条 `ask`。路由按工具种类，不以 raw confidence 为唯一门。
- Verify 票是 aider 风格的 path + 截断 unified-diff，不是 stuck 问题票。看板「核对中」，不计入 `asked_coach`。
- 等待核对/询问时只允许预跑 read / git-ro，绝不投机写。
- CRITIC：本地 `.py` 干跑通过则跳过 verify。滚动 accept 率 < 0.5 时下一笔 write 升到 ask。revise 写一行 `lesson`。
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
