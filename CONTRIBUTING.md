# 贡献指南

谢谢你愿意改 Local Foreman。本仓库是独立的开源项目，不是 Cursor / Grok Bot 的克隆。

## 开发环境

Python 3.10+。核心运行时只依赖标准库。

```bash
python3 -m pip install -e .
# Apple Silicon 上才需要真实 Worker：
python3 -m pip install -e '.[mlx]'
```

## 跑通协议（必做）

CI 和本地 smoke **只走 mock**，不下载 MLX 权重，不调用真实教练 API：

```bash
./scripts/smoke.sh
```

成功应看到：

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
```

请不要在 PR 里加入会触发 `mlx_lm.load` 或 `OpenAICoach.advise` 的测试。

## 改代码时请守住的边界

1. **Local 干活，Coach 只出短指令。** Ticket 必须有一句清楚的 `problem`，不带整仓 dump。
2. `git push` / 远端写入必须先 `ask`，禁止在 `act` 里直接执行。
3. `apply` 必须把 `instruction` 注入下一轮 Worker system prompt。
4. 新增工具先走 `tools.py` 的 `needs_ask` / `classify_risk`。
5. 不要在仓库里提交权重、`.env`、真实 API key。
6. 空转思考不得调用教练。空转动手仍走 `act` 和四条升级。展开摘要只读本机 jsonl。轨迹只有一条 jsonl，不要再开一份平行日志。
7. MID write 先无损暂扣，`accept` 才落盘；`verify` 不计 `asked_coach`。

## 提交

- 小而完整的 PR：一个主题。
- 提交说明写清「为什么」，不要只列文件名。
- 本仓库可以由维护者在本地提交；发布 remote 由维护者处理，贡献者不必自建 remote。

## 行为准则

讨论对事不对人。安全问题请走 [SECURITY.md](SECURITY.md)，不要在公开 issue 里贴 exploit。
