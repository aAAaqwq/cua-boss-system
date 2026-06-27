# 更新日志 (CHANGELOG)

本项目重要变更记录。日期格式 YYYY-MM-DD。

## [2026-06-27] 云端数据平台 + 多租户鉴权 + 管理后台（大版本）

把「本地采集的数据」升级为「**脱敏全量上云 + 多用户登录查看 + 后台统一管理**」的完整 SaaS 形态。
本地 SQLite 仍是真源；云端是受控镜像。详见 `docs/cloud-sync-plan.md` / `docs/数据安全说明.md` / `docs/权限架构.md`。

### 新增
- **上云模块** `app/cloud_sync.py`（零依赖 urllib）：候选人全字段 upsert 到 Supabase，按 `(tenant_id, uid)` 幂等；
  失败入本地队列 `data/cloud_queue.jsonl` 自动补推，best-effort 绝不阻塞采集。
- **登录绑定鉴权**：`cloud_sync.py login/logout` 用账号密码换 user token（存 `data/.cloud_auth.json`，600）；
  上云以登录身份、**RLS 强制只能写自己 tenant**；token 自动续期。service_role 降级为「仅拥有者」。
- **许可门禁**：`boss_pipeline / cua_greeting_loop / cua_collect / cua_chat_loop` 启动即 `require_account()`，
  **未用下发账号登录则拒绝运行**（注：本地明文门禁，挡正常使用、非防逆向，见 `docs/权限架构.md`）。
- **自动同步**：collect / chat 本地存完后自动增量上云（`CLOUD_SYNC=on` 开关）。
- **建表 SQL** `supabase/schema.sql`：`candidates` 全字段表 + RLS（tenant 隔离）+ 排行榜索引 + `device_tokens`。
- **用户看板** `web/index.html`（Cloudflare Pages）：登录看自己数据，详情抽屉（简历/微信/沟通气泡/一键复制）、搜索、筛选、评分色。
- **管理后台**：`admin-api/`（Cloudflare Worker 持 service_role，服务端）+ `web/admin.html`（管理面板：列出所有用户/数据量、开通/停用账号、查看任一用户候选人）；`scripts/admin.py`（本机 CLI 同等能力）。
- **CLI** `scripts/cloud_sync.py`：`login/logout/--push/--dry-run/--flush/--status`。
- **文档**：`docs/cloud-sync-plan.md`（规划）、`docs/数据安全说明.md`（采集/存储/鉴权/合规）、`docs/权限架构.md`（角色/钥匙/安全门）。

### 安全
- 公开注册已关闭（Supabase `disable_signup=true`），账号**只能后台创建**。
- 三道门：登录门禁 / RLS 行级隔离 / 管理员 uid 白名单；service_role 只在服务端，浏览器/用户机器/git 永不接触。
- 全部密钥在 `.env`（gitignored）；`config.js` / `cloud_auth.json` / `cloud_queue.jsonl` 均 gitignored。
- 已知边界：**云端数据服务端强制、不可绕**；**本地自动化为开源明文门禁、可改代码绕过**（如需锁死，走「必需能力服务端代理」）。

## [2026-06-25 ~ 26] 自动化稳定性与质量加固

- **简历下载**：策略1/2 加 `%PDF-`+体积校验（堵限流JSON存成.pdf）、防串档（姓名核验+mtime）、下载限速、防死循环。
- **评分**：根因修复 JSON 截断（max_tokens 1200→2600 + 依据精简 + 截断重试）；失败不落库（不再把 0 分缓存成粘性结果）；float 容错。
- **智能沟通**：拒绝意图识别（明显标不合适/委婉停追问，防误杀）；无简历用「在线资料+自述」针对性提问；`detect_job` 用对岗位；学校提取限定右面板（修误杀）；dry-run 零副作用。
- **打招呼**：刷新前激活窗口（cmd+R 真生效，每次换新人）+ 去掉无效滚动；学历未读到不再误杀（白名单学校宽松放行）。
- **键盘操作**：Esc 关简历预览 / 回车发送前先激活 Chrome 到前台（后台跑也可靠）。
- **CHRO 助手人格三件套**：`Identity.md` / `Soul.md` / `Agent.md`——引导用户由浅入深用好产品、收尾必给下一步。
