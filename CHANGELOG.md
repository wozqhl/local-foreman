# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/)，版本号按 SemVer。

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
