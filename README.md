# Local Foreman

Mac 优先的本地 Agent：**本地小模型做全部工作**，远端大模型只当教练（引导 / 纠正），不亲手改仓库。

完整闭环：**本地模型干活 → 遇到问题，把当前问题表述清楚 → 询问大模型 → 大模型给出相关指示 → 本地模型带着指示继续干活。**

本仓库是独立开源项目，**不是 Cursor、Grok Bot 或任何托管编程 Agent 的克隆**。协议是自己的 `act` → `ask` → `apply`，见 [protocol.md](protocol.md)。

- 作者：Shaffer Wang
- 许可：[Apache-2.0](LICENSE)（与 Qwen / MLX 生态兼容；不主张第三方商标，见 [NOTICE](NOTICE)）
- 协议状态：`act` | `ask` | `apply`（`idle` 是附加的本地空转，不问教练）
- 本机看板：干活中 / 求助中（正在咨询大模型） / 已收到指示 / 继续 / 空转中 / 自己在想

## 是什么

| 角色 | 谁 | 职责 |
| --- | --- | --- |
| Worker | 本机 `mlx-lm`（默认 Qwen3-8B 4bit）或 mock | 选工具、读改文件、跑本地命令 |
| Local loop | 本仓库 | 决定何时升级；拦住 `git push` / 远端写入；把问题写成一句 `problem` |
| Coach | 任意 OpenAI 兼容 HTTP API，或 mock | 只回一张短 JSON：`continue` / `revise` / `halt` + `instruction` |

循环：`act` →（升级条件）→ `ask` → `apply` → `act`。`apply` **必须**把教练的 `instruction` 注入下一轮 Worker system prompt。`halt` 结束进程并返回非 0。

升级只发生在：同一工具连败两次、即将 mutate git / 写 remote、用户要求 review、Worker 发出 `unsure`。

发给教练的不是仓库 dump，而是一句说清楚的问题：失败了什么、试过什么、现在需要什么。事件日志是一条轨迹：`work` → `stuck`（带问题）→ `asked_coach` → `coach_instruction` → `resumed`，空转再追加 `thought`。看板 SSE 和磁盘 jsonl 共用这一条，不另起一份日志。

空转是附加能力，不是闭环的第五步。没待处理的 Ticket、也没在跑工具时，本地 Worker 可以写一句短独白；间隔大约从 5 秒起，加倍直到上限。新目标或进入 `ask` 会把退避清零。空转**不会**打教练；想动工具仍走 `act` 和原来的四条升级条件。

最近的轨迹原文进 Worker 上下文，更老的在本地分层摘要（不编造记忆，原始 jsonl 不改写）。压缩不问教练。

## 本机看板怎么开

用标准库 HTTP + SSE，只绑 `127.0.0.1:8765`，不需要浏览器插件：

```bash
python -m local_foreman ui
# 或
local-foreman ui
```

然后打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)。

页面会显示：目标、当前状态、最后问题陈述、最后一条教练指示、心思、事件日志。状态文案是中文：

- **干活中** — 本地模型在工作
- **求助中（正在咨询大模型）** — 已把问题说清楚，正在问教练
- **已收到指示** — 教练的 `continue` / `revise` / `halt` 已回来
- **继续** — 指示已写入 Worker system prompt，本地接着干
- **空转中** / **自己在想** — 本地在想，没有问教练。看板默认打开 persist + idle，心思日志会慢慢变长

没有 API key 也能看：页面会跑一段 mock 演示（先读 README，再假装一次远端写入被拦住，教练回 `continue`，本地继续）。不要在 smoke 里打真实教练接口。

```bash
# 换端口
LOCAL_FOREMAN_UI_PORT=8765 python -m local_foreman ui
# 只开页面、不自动演示
python -m local_foreman ui --no-demo
```

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

CLI 会逐行打印状态（`act` / `ask` / `apply`，persist 时还有 `idle`），最后打印 `done=`、`states=`、`problem=`、`verdicts=`。遇到 `halt` 以非 0 退出。
一次性命令默认不写盘、不空转。要持续在场：

```bash
local-foreman --persist "把 README 读一遍并总结"
# 或 LOCAL_FOREMAN_PERSIST=1
```

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
| `LOCAL_FOREMAN_UI_HOST` | host | 看板地址，默认 `127.0.0.1` |
| `LOCAL_FOREMAN_UI_PORT` | port | 看板端口，默认 `8765` |
| `LOCAL_FOREMAN_PERSIST` | `0` \| `1` | 一次性 CLI 是否写轨迹并空转。看板自己默认打开 |
| `LOCAL_FOREMAN_TRAJ` | 路径 | 轨迹 jsonl，默认 `<cwd>/.local-foreman/traj.jsonl` |
| `LOCAL_FOREMAN_IDLE_START` | 秒 | 空转起始间隔，默认 `5` |
| `LOCAL_FOREMAN_IDLE_CAP` | 秒 | 空转间隔上限，默认 `300` |

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
problem-ok
apply-ok
ui-ok
oss-ok
traj-ok
idle-ok
compact-ok
```

含义：

1. `act-ok` — 安全 read 只待在 `act`，不升级教练
2. `ask-ok` — 假的 remote push 被拦住并进入 `ask`
3. `problem-ok` — Ticket 带一句清楚的 `problem`（失败了什么、试过什么、需要什么）
4. `apply-ok` — mock 教练的 `continue` / `revise` / `halt` 都走完 `apply`，且 `instruction` 注入下一轮 Worker system prompt
5. `ui-ok` — `GET /` 是 HTML，并能观察到求助 / 咨询大模型事件
6. `oss-ok` — 仓库里已有 `LICENSE` 与 `.github/workflows/smoke.yml`（可选 token）
7. `traj-ok` — 轨迹 jsonl 写盘，已有事件，重启后能读回来
8. `idle-ok` — 指数退避 + `thought`，全程不打教练
9. `compact-ok` — 旧条目被摘要，最近几条保持原文

GitHub Actions（[`.github/workflows/smoke.yml`](.github/workflows/smoke.yml)）在 Ubuntu + Python 3.11 上只跑这一条 mock smoke。

## 不是什么

- **不是** Cursor / Grok Bot / 云端 Agent 的克隆或兼容层。
- **不是** 托管服务。默认全部在你自己的机器上跑。
- Coach 只看一张短 Ticket（核心是 `problem`），不接收整仓 dump。
- 本项目只允许本地 git；不要在这个工作副本上加 remote、不要 push。发布由维护者处理。

## 许可与归属

源代码为 [Apache License 2.0](LICENSE)。可选推理栈（mlx-lm、mlx-community 权重、Qwen 名称）属于各自所有者，见 [NOTICE](NOTICE)。

更多： [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [ROADMAP.md](ROADMAP.md) · [CHANGELOG.md](CHANGELOG.md)
