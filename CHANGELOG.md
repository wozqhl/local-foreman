# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/)，版本号按 SemVer。

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
