# Local Foreman

Mac 优先的本地 Agent。**本地小模型做全部工作**，远端大模型只当教练：引导 / 纠正，不亲手改仓库。

- 用户：Shaffer Wang
- 本仓库是独立项目，不属于 oss-cash-lab
- 协议状态：`act` | `ask` | `apply`（见 [protocol.md](protocol.md)）

## Mac / MLX

在 Apple Silicon 上，Worker 走 `mlx-lm`，模型：

`mlx-community/Qwen3-8B-4bit`

本 Linux 盒子没有 MLX。这里用 `mock` Worker / Coach 跑通协议；不要在 smoke 里下载模型或打真实 API。

```bash
# Mac（真模型，需已安装 mlx-lm，自行准备权重）
export LOCAL_FOREMAN_WORKER=mlx
export LOCAL_FOREMAN_COACH=openai
export COACH_BASE_URL=https://api.openai.com/v1
export COACH_API_KEY=...
export COACH_MODEL=gpt-4o
PYTHONPATH=src python3 -m local_foreman "把 README 读一遍并总结"
```

## 怎么跑 smoke

不下载模型，不调用真实 API：

```bash
cd /path/to/local-foreman
./scripts/smoke.sh
```

成功时打印三行 token 并以 0 退出：

```
act-ok
ask-ok
apply-ok
```

含义：

1. `act-ok` — 安全 read 只待在 `act`，不升级教练
2. `ask-ok` — 假的 remote push 被拦住并进入 `ask`
3. `apply-ok` — mock 教练的 `continue` / `revise` / `halt` 都走完 `apply`，且 `instruction` 注入下一轮 Worker system prompt

## 环境变量

| 变量 | 值 | 说明 |
| --- | --- | --- |
| `LOCAL_FOREMAN_WORKER` | `mock` \| `mlx` | Worker 后端。Linux / smoke 用 `mock` |
| `LOCAL_FOREMAN_COACH` | `mock` \| `openai` | 教练后端。smoke 用 `mock` |
| `COACH_BASE_URL` | URL | OpenAI 兼容接口，默认 `https://api.openai.com/v1` |
| `COACH_API_KEY` | secret | 教练 API key；smoke 不需要 |
| `COACH_MODEL` | 模型名 | 默认 `gpt-4o` |
| `LOCAL_FOREMAN_ROOT` | 路径 | 工具的工作根目录，默认 cwd |

跑一条真实目标（仍可用 mock）：

```bash
export LOCAL_FOREMAN_WORKER=mock
export LOCAL_FOREMAN_COACH=mock
PYTHONPATH=src python3 -m local_foreman "读 README"
```

`git push` / 远端写入必须先 `ask`。本项目只允许本地 git，不要加 remote，不要 push。
