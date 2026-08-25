# Local Foreman

Mac 优先的本地 Agent：**本地小模型做全部工作**，远端大模型只当教练（引导 / 纠正），不亲手改仓库。

本仓库是独立开源项目，**不是 Cursor、Grok Bot 或任何托管编程 Agent 的克隆**。协议是自己的 `act` → `ask` → `apply`，见 [protocol.md](protocol.md)。

- 作者：Shaffer Wang
- 许可：[Apache-2.0](LICENSE)（与 Qwen / MLX 生态兼容；不主张第三方商标，见 [NOTICE](NOTICE)）
- 协议状态：`act` | `ask` | `apply`

## 是什么

| 角色 | 谁 | 职责 |
| --- | --- | --- |
| Worker | 本机 `mlx-lm`（默认 Qwen3-8B 4bit）或 mock | 选工具、读改文件、跑本地命令 |
| Local loop | 本仓库 | 决定何时升级；拦住 `git push` / 远端写入 |
| Coach | 任意 OpenAI 兼容 HTTP API，或 mock | 只回一张短 JSON：`continue` / `revise` / `halt` |

循环：`act` →（升级条件）→ `ask` → `apply` → `act`。`apply` **必须**把教练的 `instruction` 注入下一轮 Worker system prompt。`halt` 结束进程并返回非 0。

升级只发生在：同一工具连败两次、即将 mutate git / 写 remote、用户要求 review、Worker 发出 `unsure`。

## 真实链路怎么接

1. Apple Silicon 上装好 `mlx-lm` 和权重（见下一节）。
2. 准备一个 OpenAI 兼容的教练端点（OpenAI、DeepSeek、OpenRouter、自建网关都可以）。
3. 用 CLI 同时指定 Worker 和 Coach：

```bash
pip install -e '.[mlx]'

export LOCAL_FOREMAN_WORKER=mlx
export LOCAL_FOREMAN_COACH=openai
export LOCAL_FOREMAN_MLX_MODEL=mlx-community/Qwen3-8B-4bit   # 默认值，可省略
export COACH_BASE_URL=https://api.openai.com/v1              # DeepSeek / OpenRouter 换成它们的 base
export COACH_API_KEY=sk-...
export COACH_MODEL=gpt-4o

local-foreman "把 README 读一遍并总结"
# 等价：
python -m local_foreman --worker mlx --coach openai "把 README 读一遍并总结"
```

CLI 会逐行打印状态（`act` / `ask` / `apply`），最后打印 `done=`、`states=`、`verdicts=`。遇到 `halt` 以非 0 退出。

没有 Mac、或不想下载权重时，整条协议仍可用 mock 跑：

```bash
python -m local_foreman --worker mock --coach mock "读 README"
```

## Mac 安装 mlx-lm

需要 **Apple Silicon**。Linux CI / 本开发盒没有 MLX，不要在这些机器上 `load` 权重。

```bash
# 建议在虚拟环境里
python3 -m pip install 'local-foreman[mlx]'
# 或开发安装
python3 -m pip install -e '.[mlx]'
```

这会装上 `mlx-lm`。第一次用 `MlxWorker` 时才会 `load(LOCAL_FOREMAN_MLX_MODEL)`；若本地没有缓存，`mlx-lm` 可能自行拉取 `mlx-community/Qwen3-8B-4bit`。请先自己准备好权重，**不要在 smoke 或 CI 里触发下载**。

缺包时的错误会明确告诉你：在 Apple Silicon 上执行 `pip install 'local-foreman[mlx]'`。

## 环境变量

| 变量 | 值 | 说明 |
| --- | --- | --- |
| `LOCAL_FOREMAN_WORKER` | `mock` \| `mlx` | Worker 后端。Linux / smoke 用 `mock` |
| `LOCAL_FOREMAN_COACH` | `mock` \| `openai` | 教练后端。smoke 用 `mock` |
| `LOCAL_FOREMAN_MLX_MODEL` | HF id | 默认 `mlx-community/Qwen3-8B-4bit` |
| `COACH_BASE_URL` | URL | 任意 OpenAI 兼容根路径，默认 `https://api.openai.com/v1` |
| `COACH_API_KEY` | secret | 教练 API key；smoke 不需要 |
| `COACH_MODEL` | 模型名 | 默认 `gpt-4o` |
| `LOCAL_FOREMAN_ROOT` | 路径 | 工具工作根目录，默认 cwd |

CLI 的 `--worker` / `--coach` 会覆盖对应环境变量。`--smoke` 会强制两边都是 mock。

## Smoke

不下载模型，不调用真实 API：

```bash
./scripts/smoke.sh
```

成功时打印并以 0 退出：

```
act-ok
ask-ok
apply-ok
oss-ok
```

含义：

1. `act-ok` — 安全 read 只待在 `act`，不升级教练
2. `ask-ok` — 假的 remote push 被拦住并进入 `ask`
3. `apply-ok` — mock 教练的 `continue` / `revise` / `halt` 都走完 `apply`，且 `instruction` 注入下一轮 Worker system prompt
4. `oss-ok` — 仓库里已有 `LICENSE` 与 `.github/workflows/smoke.yml`（可选 token）

GitHub Actions（[`.github/workflows/smoke.yml`](.github/workflows/smoke.yml)）在 Ubuntu + Python 3.11 上只跑这一条 mock smoke。

## 不是什么

- **不是** Cursor / Grok Bot / 云端 Agent 的克隆或兼容层。
- **不是** 托管服务。默认全部在你自己的机器上跑。
- Coach 只看一张短 Ticket，不接收整仓 dump。
- 本项目只允许本地 git；不要在这个工作副本上加 remote、不要 push。发布由维护者处理。

## 许可与归属

源代码为 [Apache License 2.0](LICENSE)。可选推理栈（mlx-lm、mlx-community 权重、Qwen 名称）属于各自所有者，见 [NOTICE](NOTICE)。

更多： [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [ROADMAP.md](ROADMAP.md) · [CHANGELOG.md](CHANGELOG.md)
