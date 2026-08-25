# 路线图

## 0.1 — 真实链路

## 0.1.1 — 看板与说清问题

- [x] `act` / `ask` / `apply` 协议与 ticket 校验
- [x] 真实 `MlxWorker`：可选导入 mlx-lm，chat template，`parse_action`
- [x] 真实 `OpenAICoach`：OpenAI 兼容 HTTP，10s 超时，脏 JSON 解析
- [x] 产品 CLI：`--worker` / `--coach`，状态打印，halt 非 0 退出
- [x] mock smoke + GitHub Actions（仅 mock）
- [x] Apache-2.0 文档与 OSS 元数据
- [x] 把问题说清楚再问教练（`ticket.problem`）
- [x] 本机看板：干活中 / 求助中（正在咨询大模型） / 已收到指示 / 继续

## 0.2 — 持续在场（当前）

- [x] 一条轨迹 jsonl，看板与循环共用
- [x] 空转本地思考 + 指数退避（不问教练）
- [x] 分层压缩（只在本地，不编造记忆）
- [x] 按 seq 展开摘要（`retrieved`，注入 Worker 上下文）
- [x] 空转可走本地小动作（`idle_act`，仍守四条升级，不问教练）
- [x] Smoke：`traj-ok` / `idle-ok` / `compact-ok` / `retrieve-ok` / `idle-act-ok` / `traj-cli-ok`
- [x] `traj` CLI：人眼查看同一条 jsonl（`--last` / `--kind` / `--out`）
- [x] 本机统计教练询问次数（同一条 jsonl；空转不计；可选 `COACH_USD_PER_ASK`）
- [x] Smoke：`ask-cost-ok`

## 0.3 — Worker 更稳

- [ ] 把 Qwen3 thinking 块从输出里剥干净（即使模板没关）
- [ ] 可配置 `max_tokens` / 采样
- [ ] 模型加载进度与失败重试提示
- [ ] 多轮 history 改成 chat turns，而不是整段 JSON

## 0.4 — 工具与守卫

- [ ] 更细的 git 只读白名单
- [ ] 工作区沙箱（限制 `write` / `shell` 出根目录）
- [ ] 用户确认钩子（终端里对 `halt` 以外的高风险 continue 再确认一次）

## 明确不做

- 不做 Cursor / Grok Bot 的克隆或兼容层
- 不在 CI 下载权重
- 不在 smoke 里打真实教练 API
- 不在空转里打远程教练
- 不把本仓库绑到某个云厂商
