# 路线图

## 0.1 — 真实链路（当前）

- [x] `act` / `ask` / `apply` 协议与 ticket 校验
- [x] 真实 `MlxWorker`：可选导入 mlx-lm，chat template，`parse_action`
- [x] 真实 `OpenAICoach`：OpenAI 兼容 HTTP，10s 超时，脏 JSON 解析
- [x] 产品 CLI：`--worker` / `--coach`，状态打印，halt 非 0 退出
- [x] mock smoke + GitHub Actions（仅 mock）
- [x] Apache-2.0 文档与 OSS 元数据

## 0.2 — Worker 更稳

- [ ] 把 Qwen3 thinking 块从输出里剥干净（即使模板没关）
- [ ] 可配置 `max_tokens` / 采样
- [ ] 模型加载进度与失败重试提示
- [ ] 多轮 history 改成 chat turns，而不是整段 JSON

## 0.3 — 工具与守卫

- [ ] 更细的 git 只读白名单
- [ ] 工作区沙箱（限制 `write` / `shell` 出根目录）
- [ ] 用户确认钩子（终端里对 `halt` 以外的高风险 continue 再确认一次）

## 明确不做

- 不做 Cursor / Grok Bot 的克隆或兼容层
- 不在 CI 下载权重
- 不在 smoke 里打真实教练 API
- 不把本仓库绑到某个云厂商
