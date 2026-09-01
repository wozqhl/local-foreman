# Local Foreman

本机 Qwen 干活；只有卡住才问远端教练。目标是更快、更省 token、质量相当——**尚未用数字证明**。

本仓库是独立开源项目，**不是** Cursor、Grok Bot 或任何托管编程 Agent 的克隆。作者：Shaffer Wang。许可：[Apache-2.0](LICENSE)。

## 30 秒跑起来（无需 API key）

无需 API key，仅 mock：

```bash
python3 -m pip install 'git+https://github.com/wozqhl/local-foreman.git'
python3 -m local_foreman --worker mock --coach mock "读 README"
```

贡献者先克隆后用可编辑安装：

```bash
python3 -m pip install -e .
python3 -m local_foreman --worker mock --coach mock "读 README"
```

等价：`pip install -e .` 之后 `local-foreman --worker mock --coach mock "读 README"`。

完整 mock smoke（同样无 key）：

```bash
./scripts/smoke.sh
```

看板（127.0.0.1，mock 演示，无 key）：

```bash
python3 -m local_foreman ui
```

然后打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)。

## Mac + MLX（真本地模型）

需要 **Apple Silicon**。Linux CI / 本开发盒没有 MLX，不要在这些机器上 `load` 权重。

```bash
python3 -m pip install -e '.[mlx]'

export LOCAL_FOREMAN_WORKER=mlx
export LOCAL_FOREMAN_COACH=openai
export LOCAL_FOREMAN_MLX_MODEL=mlx-community/Qwen3-8B-4bit   # 默认值，可省略
export COACH_BASE_URL=https://api.openai.com/v1              # DeepSeek / OpenRouter 换成它们的 base
export COACH_API_KEY=sk-...
export COACH_MODEL=gpt-4o

local-foreman "把 README 读一遍并总结"
# 等价：
python3 -m local_foreman --worker mlx --coach openai "把 README 读一遍并总结"
```

缺包时的错误会明确告诉你：在 Apple Silicon 上执行 `pip install 'local-foreman[mlx]'`。第一次用 `MlxWorker` 时才会 `load`；若本地没有缓存，`mlx-lm` 可能自行拉取权重。请先自己准备好，**不要在 smoke 或 CI 里触发下载**。首次加载会在 stderr / 看板打印中文进度，并对网络类失败按 `LOCAL_FOREMAN_LOAD_RETRIES` 重试（不是求助教练）。

CLI 会逐行打印状态（`act` / `verify` / `ask` / `apply`，persist 时还有 `idle`），最后打印 `done=`、`states=`、`problem=`、`verdicts=`。遇到 `halt` 以非 0 退出。一次性命令默认不写盘、不空转。要持续在场：

```bash
local-foreman --persist "把 README 读一遍并总结"
# 或 LOCAL_FOREMAN_PERSIST=1
```

轨迹只有一条 jsonl。人眼查看用同一个文件，不另起格式：

```bash
python3 -m local_foreman bench
python3 -m local_foreman traj --last 20
local-foreman traj --stats
```

`--last N` 只看最近，`--kind` 按逗号过滤，`--out` 导出仍是同一份 jsonl。`--stats` 统计本文件上的教练询问 / 回复次数；空转想法不计次。只有设置了 `COACH_USD_PER_ASK` 才额外估算美元。`LOCAL_FOREMAN_MAX_ASKS` 是询问硬上限。路径默认 `$LOCAL_FOREMAN_TRAJ` 或 `<cwd>/.local-foreman/traj.jsonl`。HIGH git/remote continue 默认在 TTY 再确认一次；CI / smoke 用 `LOCAL_FOREMAN_CONFIRM=0` 或 `--no-confirm`。

## 是什么

做法是三条风险车道，而不是每一步都问大模型（见 [protocol.md](protocol.md)）：

| 车道 | 何时 | 教练 |
| --- | --- | --- |
| LOW | 读文件、只读 git、空转 | 不问（0 token） |
| MID | 本地拟好的 write（无损暂扣，accept 才落盘） | `verify` 短票，看板写**核对中**，不是求助 |
| HIGH | 原四条升级（连败两次 / git·remote / review / unsure） | `ask` |

当前 `act` / `verify` / `ask` / `apply` 在 chase 这三项目标。mock 对照台 `python -m local_foreman bench` 用夹具通过率当质量、用 asks+verifies 当 token 代理。**没有真实模型分数之前，不宣称已达成。**

- 协议状态：`act` | `verify` | `ask` | `apply`（`idle` 是附加的本地空转，不问教练）
- 本机看板：干活中 / 核对中 / 求助中（正在咨询大模型） / 已收到指示 / 继续 / 空转中 / 自己在想 / 展开原文 / 空转动手 / 待确认

| 角色 | 谁 | 职责 |
| --- | --- | --- |
| Worker | 本机 `mlx-lm`（默认 Qwen3-8B 4bit）或 mock | 选工具、读改文件、跑本地命令 |
| Local loop | 本仓库 | 决定何时升级；拦住 `git push` / 远端写入；把问题写成一句 `problem` |
| Coach | 任意 OpenAI 兼容 HTTP API，或 mock | 只回一张短 JSON：`continue` / `revise` / `halt` + `instruction` |

循环：`act` →（low 留下 / verify / ask）→ `apply` → `act`。HIGH 的 `apply` **必须**把教练的 `instruction` 注入下一轮 Worker system prompt。`halt` 结束进程并返回非 0。HIGH continue 且原因是 git mutate / remote / `git push` 时，恢复 act 前再确认一次（TTY 默认；`--no-confirm` 可跳过）。MID 的 write 在 `accept` 前不落盘。

设定了工作区 `root` 时，`read` / `write` / `shell` 不能逃出根目录（绝对路径或 `..` 会本地硬拦，前缀 `sandbox:`，不问教练）。persist 长驻 Worker 也依赖这一层沙箱。

HIGH 升级只发生在：同一工具连败两次、即将 mutate git / 写 remote、用户要求 review、Worker 发出 `unsure`。

发给教练的不是仓库 dump，而是一句说清楚的问题：失败了什么、试过什么、现在需要什么。MID 写操作先 hold，`verify` 票是 claim+draft，`accept` 才落盘。事件日志是一条轨迹：`work` → `stuck`（带问题）→ `asked_coach` → `coach_instruction` → `resumed`，核对再追加 `verified_coach` / `coach_verdict`（不计 ask），空转再追加 `thought`，需要时还有 `retrieved` / `idle_act` / `lesson` / `user_denied`。看板 SSE 和磁盘 jsonl 共用这一条，不另起一份日志。

空转是附加能力，不是闭环的第五步。没待处理的 Ticket、也没在跑工具时，本地 Worker 可以写一句短独白；间隔大约从 5 秒起，加倍直到上限。新目标或进入 `ask` 会把退避清零。空转**不会**打教练；想动工具仍走 `act` 和原来的四条升级条件，并记 `idle_act`。

最近的轨迹原文进 Worker 上下文，更老的在本地分层摘要（不编造记忆，原始 jsonl 不改写）。摘要可按 seq 展开回原文，注入 Worker 上下文，记 `retrieved`。压缩和展开都不问教练。

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
- **核对中** — 教练在看本地草稿（claim+draft），不是求助，不计 ask
- **求助中（正在咨询大模型）** — 已把问题说清楚，正在问教练
- **已收到指示** — 教练的 `continue` / `revise` / `halt` 已回来
- **继续** — 指示已写入 Worker system prompt，本地接着干
- **待确认** — HIGH continue 且下一步是 git/remote/`git push`，等人点头再恢复干活
- **空转中** / **自己在想** — 本地在想，没有问教练。看板默认打开 persist + idle，心思日志会慢慢变长
- **展开原文** — 把压缩摘要按 seq 取回同一条 jsonl 的原文
- **空转动手** — 空转选了一个本地小动作，仍走 `act`，没有问教练
- **教练用量** — 同一条轨迹上的询问 / 回复次数。核对另计，不计入 ask。空转想法不计次；未设置 `COACH_USD_PER_ASK` 时不估美元

没有 API key 也能看：页面会跑一段 mock 演示（先读 README，再假装一次远端写入被拦住，教练回 `continue`，本地继续）。不要在 smoke 里打真实教练接口。

```bash
# 换端口
LOCAL_FOREMAN_UI_PORT=8765 python -m local_foreman ui
# 只开页面、不自动演示
python -m local_foreman ui --no-demo
```

## 环境变量

| 变量 | 值 | 说明 |
| --- | --- | --- |
| `LOCAL_FOREMAN_WORKER` | `mock` \| `mlx` | Worker 后端。Linux / smoke 用 `mock` |
| `LOCAL_FOREMAN_COACH` | `mock` \| `openai` | 教练后端。smoke 用 `mock` |
| `LOCAL_FOREMAN_MLX_MODEL` | HF id | 默认 `mlx-community/Qwen3-8B-4bit` |
| `LOCAL_FOREMAN_MAX_TOKENS` | 整数 | 可选。MLX `generate` 长度，默认 512 |
| `LOCAL_FOREMAN_TEMP` | 浮点 | 可选。设置后传 mlx-lm sampler；未设置保持 generate 默认 |
| `LOCAL_FOREMAN_TOP_P` | 浮点 | 可选。设置后传 mlx-lm sampler；未设置保持 generate 默认 |
| `LOCAL_FOREMAN_LOAD_RETRIES` | 整数 | 可选。MLX `load` 重试次数，默认 3（最小 1） |
| `LOCAL_FOREMAN_LOAD_RETRY_SLEEP` | 秒 | 可选。两次 load 之间的等待，默认 1；smoke 置 0 |
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
| `COACH_USD_PER_ASK` | 美元/次 | 可选。设置后 `traj --stats` 与看板才估算费用；未设置只计次数 |
| `LOCAL_FOREMAN_MAX_ASKS` | 整数 | 可选。教练询问硬上限。再问一次会超过则跳过 ask，留在本地（空转或 halt），不调用教练。未设置不设上限 |
| `LOCAL_FOREMAN_MAX_VERIFIES` | 整数 | 可选。核对硬上限。再核一次会超过则跳过 verify，留在本地。未设置不设上限 |
| `LOCAL_FOREMAN_DEMOS` | 路径 | 可选。EcoAssistant demo 缓存 jsonl。默认 `<cwd>/.local-foreman/demos.jsonl`。只存本地 compact demo，不存教练改写 |
| `LOCAL_FOREMAN_CONFIRM` | `0` \| `1` | 可选。HIGH git/remote `continue` 是否再确认。默认 TTY 开启；`0` / `--no-confirm` 跳过（CI/smoke） |

CLI 的 `--worker` / `--coach` 会覆盖对应环境变量；`--max-tokens` 写入 `LOCAL_FOREMAN_MAX_TOKENS`。`--no-confirm` 写入 `LOCAL_FOREMAN_CONFIRM=0`。`--smoke` 会强制两边都是 mock。

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
retrieve-ok
idle-act-ok
traj-cli-ok
ask-cost-ok
max-ask-ok
verify-ok
bench-ok
self-verify-ok
demo-ok
calibrate-ok
think-strip-ok
chat-turns-ok
load-retry-ok
sandbox-ok
git-ro-ok
confirm-ok
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
10. `retrieve-ok` — 压缩摘要能按 seq 展开回原始 jsonl，并注入 Worker 上下文
11. `idle-act-ok` — 空转可触发本地安全 act，不打教练；远端写入仍走四条升级
12. `traj-cli-ok` — `traj --last` 读同一条 jsonl，能打印 `thought` / `idle_act` / `retrieved`
13. `ask-cost-ok` — mock ask/apply 后 `asks>=1`；仅空转的 persist 片段 `asks` 仍为 0
14. `max-ask-ok` — `LOCAL_FOREMAN_MAX_ASKS=1` 时第一次 ask 会打教练，第二次升级跳过，不调用教练
15. `verify-ok` — 本地 write 走 `verify` → `accept` 才落盘，不走 `ask`；revise 丢弃草稿
16. `bench-ok` — mock 对照台：local asks+verifies < remote-only 调用，local 墙钟更短，夹具通过率相同
17. `self-verify-ok` — 本地自核：高 p + CRITIC 留在 LOW；p 很低两次且非升级不打教练；真升级仍走 ask
18. `demo-ok` — verify accept 后本地缓存 demo，相似 write 注入 worker；revise / ask / 教练改写不入库
19. `calibrate-ok` — 同一条 traj 上滚动校准 P(accept|conf_bucket,act_type)；够样本且 P 高则跳过 verify；分歧不另造 HIGH；样本不足仍走 DSP / tax
20. `think-strip-ok` — 带 `<think>` 的夹具经 `parse_action` 得到 tool 而不是 unsure；`LOCAL_FOREMAN_MAX_TOKENS` 读入 MlxWorker / factory（不 load）
21. `chat-turns-ok` — history 拆成 assistant/user chat turns（非 JSON dump）；不 load、不打教练
22. `load-retry-ok` — 注入假 loader：失败两次后成功 / 始终失败 / ImportError 提示；不 load 权重、不打教练
23. `sandbox-ok` — tempfile root 下：根内 write/shell 成功；`../` 与绝对路径越界 read/write/shell 硬拦（`sandbox:`，`escalated=False`）
24. `git-ro-ok` — `git remote -v` / `config --get` / `stash list` / `tag -l` / `worktree list` 不 needs_ask；push/commit/remote add/config 写入仍升级
25. `confirm-ok` — mock HIGH git-push：`confirm` 回 False 记 `user_denied`、不执行 push、不逃沙箱；回 True 走原来的 continue

GitHub Actions（[`.github/workflows/smoke.yml`](.github/workflows/smoke.yml)）在 Ubuntu + Python 3.11 上只跑这一条 mock smoke。

## 不是什么

- **不是** Cursor / Grok Bot / 云端 Agent 的克隆或兼容层。
- **不是** 托管服务。默认全部在你自己的机器上跑。
- Coach 只看一张短 Ticket（核心是 `problem`），不接收整仓 dump。
- 本项目只允许本地 git；不要在这个工作副本上加 remote、不要 push。发布由维护者处理。

## 许可与归属

源代码为 [Apache License 2.0](LICENSE)。可选推理栈（mlx-lm、mlx-community 权重、Qwen 名称）属于各自所有者，见 [NOTICE](NOTICE)。

更多： [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [ROADMAP.md](ROADMAP.md) · [CHANGELOG.md](CHANGELOG.md)
