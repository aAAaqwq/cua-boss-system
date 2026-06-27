# 云端数据平台 — 规划（分支 `feat/cloud-sync`）

> 把本地采集的数据**脱敏后上云**，用户**网页登录看自己的数据**；agent 仍只做**本地采集**，本地 SQLite 是真源、另存本地备份。

## 决策基线（已敲定）
| 维度 | 选择 |
|------|------|
| 产品形态 | **多租户 SaaS** |
| PII 上云 | **重度脱敏后上云**（手机/微信/简历正文不出本地）|
| v1 范围 | **只读看板**（单向 local→cloud，网页纯展示）|
| 运行归属 | **各用户自跑 agent**（自己 BOSS 号、推自己的行）→ 责任分散 |

## 0. 目标 / 非目标
- **目标**：用户网页登录 → 看自己的候选人 / 评分排行榜 / 面试看板；数据本地采集 + 本地备份 + 脱敏上云。
- **非目标(v1)**：云端驱动招聘（永远不行，cua-driver 钉在 Mac）；网页回写动作（v2 再说）；PII 完整上云。

## 1. 架构总览
```
[用户A的Mac] agent(本地采集) → SQLite(真源) → data/backups(本地备份)
                                    │ 脱敏(to_public)
                                    ▼
                          cloud_sync (urllib HTTPS, upsert by uid_hash, 带 tenant)
                                    ▼
                ┌──────── Supabase (Postgres + Auth + RLS) ────────┐
[用户B的Mac]→…─►│  candidates_public 表（脱敏字段，RLS 按 user 隔离） │
                └───────────────────▲──────────────────────────────┘
                                    │ Supabase JS (auth + RLS)
                        [Cloudflare Pages 前端] ← 用户登录浏览（只读）
```
**责任模型**：各用户用自己 BOSS 号、自跑 agent、推自己的行 → 各自是自己数据的控制者；你 = 软件 + 基础设施提供方。

## 2. 数据模型（上云的脱敏子集）
本地 `candidates` 全字段保留（真源）。上云只推「决策够用、PII 最小」的子集：

| 上云字段 | 用途 | PII |
|---------|------|-----|
| `uid_hash` | uid 做 HMAC → 稳定主键、不可逆 | 否 |
| `name_masked` | 「张**」只留姓（可配 `NAME_MASK_MODE`：全名/留姓/全脱）| 弱 |
| `school` / `degree` / `job_position` | 决策展示 | 否 |
| `score` / `score_summary` / `grade` | 排行榜 | 否（summary 需二次过滤）|
| `status` / `interview_type` / `interview_date` / `interview_time` | 面试看板 | 否 |
| `updated_at` / `scored_at` | 排序 | 否 |
| **不上云**：`phone` `wechat` `resume_content` `resume_path` 原始 `chat_history` | 敏感 → 永留本地 | — |

> 取舍：网页能「挑出谁值得面」，但**拿联系方式 / 约面仍在本地 agent 做**（联系方式不出本地）。这是 v1 的有意设计。

## 3. 组件与改动
- **`app/cloud_sync.py`（新，零依赖 urllib）**
  - `to_public(row)`：脱敏映射 + `score_summary` 二次正则过滤（去掉模型偶尔写进的手机/微信）
  - `push(rows)`：Supabase PostgREST `upsert`（`on_conflict=tenant,uid_hash`），best-effort
  - **离线队列**：推失败写 `data/cloud_queue.jsonl`，下次补推，**绝不阻塞本地采集**
- **集成点**：`boss_pipeline` / `cua_collect` / `query_db --rank` 末尾按开关 `CLOUD_SYNC=on` 调 `push`
- **`scripts/cloud_sync.py`（新 CLI）**：`--push`（全量补推）/ `--dry-run`（看脱敏后长啥样，不上传）
- **`.env` 增**：`SUPABASE_URL` `SUPABASE_ANON_KEY` `CLOUD_AGENT_TOKEN`(用户身份) `TENANT_ID` `UID_HMAC_SECRET` `NAME_MASK_MODE` `CLOUD_SYNC`
- **Supabase**：`candidates_public` 表 + RLS(`tenant = auth.uid()`) + Auth(邮箱/magic link) + `device_tokens` 表（把 agent 令牌映射到 user）
- **`web/`（新子目录，Cloudflare Pages）**：Vite + Supabase JS（前端有自己的 npm 依赖，与零依赖的 agent 解耦）；登录 → 排行榜 / 候选人 / 面试 三页，**纯只读**

## 4. 同步策略
- **单向** local→cloud；本地 SQLite 永远是真源。
- **幂等**：`upsert by (tenant, uid_hash)`；删除走软删（status）不硬删。
- **触发**：每次 pipeline / 评分后 + 手动 `scripts/cloud_sync.py --push`。
- **容错**：网络 / Supabase 挂 → 入本地队列，下次补推。
- **频率**：批量、非实时（省额度）。

## 5. 鉴权（各用户自跑下）
1. 用户网页注册 → Supabase Auth 账号（= tenant）。
2. 网页生成一个**设备令牌**存 `device_tokens`（关联其 user_id）→ 用户贴进本地 `.env` 的 `CLOUD_AGENT_TOKEN`。
3. agent 用该令牌调 PostgREST，RLS 确保**只能写自己 tenant 的行**。
4. ⚠️ **绝不把 service_role key 下发给用户**（那是全库管理员权限，泄露 = 灾难）。

## 6. 合规设计
- **脱敏**：phone/wechat/resume 不出本地 → 云端无「可直接联系到人」的敏感信息。
- **责任分散**：各用户自跑自号 → 各自控制者；你提供工具与基础设施。
- **数据跨境**：Supabase 选**新加坡区**或**境内自托管**；文档/隐私条款明示数据存放地。
- **ToS / 用户协议**：明确「使用即代表你对自己抓取并上传的数据负责」（免责 + 用户协议勾选）。
- ⚠️ 仍未消除的法律风险见 §8。

## 7. 分阶段路线图
| 阶段 | 内容 | 估时 |
|------|------|------|
| **P0 验证** | 建 Supabase 项目 + 1 张表 + 手动 curl upsert 一行脱敏数据，跑通最小闭环 | 0.5d |
| **P1 本地推送** | `cloud_sync.py`（脱敏+队列+push）+ pipeline 集成 + CLI；本地验证 | 1–2d |
| **P2 只读前端** | `web/`（Vite+Supabase JS）排行榜页 → Cloudflare Pages 部署；单租户跑通 | 2–3d |
| **P3 多租户+鉴权** | Auth + RLS + device_tokens + 注册/登录；两账号互不可见 | 2–3d |
| **P4 打磨** | 候选人/面试页、错误处理、`.env.example`、文档、人格三件套加「看板」引导 | 1–2d |

## 8. 风险与未决（还要你拍板）
1. **脱敏 ≠ 绝对安全**：name + school + 岗位组合仍可能再识别。要不要连 name 也全脱（只留「候选人 #3」）？→ 影响网页可用性。
2. **去了联系方式，网页价值打折**：网页只能「看 + 决策」，拿电话/约面仍回本地。接受？
3. **BOSS ToS**：即便脱敏，把候选人数据搬离平台存自有云仍可能违约。要不要法务过一遍 / 强制用户免责协议？
4. **令牌安全**：设备令牌泄露 = 该用户数据可被写。要不要令牌轮换 + 作用域限制？
5. **`score_summary` 漏 PII**：模型可能在总结里写进电话/微信 → 上云前**必须**正则二次过滤（已列入 P1）。
6. **成本**：免费档够起步（Supabase 500MB / 50k MAU、Cloudflare Pages 免费）；注意 DB 容量上限。

## 9. 验收标准
- [ ] 两个账号各自本地跑 → 网页各看各的排行榜，**互不可见**（RLS 生效）。
- [ ] 云端表中**无** phone / wechat / resume 原文（脱敏校验脚本通过）。
- [ ] 断网时本地采集不受影响，恢复后自动补推（队列生效）。
- [ ] 本地 SQLite 仍是完整真源 + `data/backups` 正常。
- [ ] 前端只读，无任何写库入口（v1）。
