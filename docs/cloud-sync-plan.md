# 云端数据平台 — 规划（分支 `feat/cloud-sync`）

> 把本地采集的数据**脱敏后上云**，用户**网页登录看自己的数据**；agent 仍只做**本地采集**，本地 SQLite 是真源、另存本地备份。

## 决策基线（已敲定）
| 维度 | 选择 |
|------|------|
| 产品形态 | **多租户 SaaS** |
| PII 上云 | **全字段上云（不重脱敏）**——网页可看全部数据（含手机/微信/简历）|
| 数据控制 | **你集中控管全部用户数据**（你的 Supabase 项目）→ 你即数据控制者，责任全在你 |
| 合规姿态 | **强制用户免责**（注册时勾选 + 小字声明）；安全(RLS/静态加密/令牌)为硬要求 |
| 访问 | 网页**必须登录认证**；RLS 保证用户只看自己的数据 |
| 区域 | Supabase **新加坡区** |
| v1 范围 | **只读看板**（单向 local→cloud，网页纯展示）|
| 运行 | 各用户自跑 agent（自己 BOSS 号），推**全字段**到你的云 |

> ⚠️ **清醒提示（已与用户确认仍按此做）**：小字免责覆盖「你↔用户」，盖不住「你↔候选人」（数据主体未同意）；集中控管全量 PII = 最高责任 + 拖库即大规模泄露。**因此安全不是可选项**：RLS 强隔离 + 列级/静态加密 + 令牌最小权限是本方案的硬约束。

## 0. 目标 / 非目标
- **目标**：用户网页登录 → 看自己的候选人 / 评分排行榜 / 面试看板；数据本地采集 + 本地备份 + 脱敏上云。
- **非目标(v1)**：云端驱动招聘（永远不行，cua-driver 钉在 Mac）；网页回写动作（v2 再说）；云端看简历 PDF 原件（v2 接 Storage）。

## 1. 架构总览
```
[用户A的Mac] agent(本地采集) → SQLite(真源) → data/backups(本地备份)
                                    │ to_cloud(全字段映射)
                                    ▼
                          cloud_sync (urllib HTTPS, upsert by (tenant_id,uid))
                                    ▼
                ┌──────── Supabase 新加坡区 (Postgres + Auth + RLS + 静态加密) ────────┐
[用户B的Mac]→…─►│  candidates 表（全字段，含 PII；RLS 按 tenant_id=auth.uid 隔离）     │
                └───────────────────────▲──────────────────────────────────────────────┘
                                    │ Supabase JS (必须登录 auth + RLS)
                        [Cloudflare Pages 前端] ← 用户登录浏览（只读，可见全部数据）
```
**责任模型**：各用户自跑 agent（自己 BOSS 号）推**全字段**到**你的** Supabase；**你集中控管全部用户数据 → 你即数据控制者，合规责任全在你**。

## 2. 数据模型（全字段镜像上云）
本地 `candidates` 是真源；**云端 `candidates` 表镜像其全部业务字段**（网页要能看全部）：

| 上云字段 | 用途 | PII |
|---------|------|-----|
| `tenant_id` | 租户隔离键（= 用户 auth.uid）| — |
| `uid` | BOSS 用户标识，配 tenant 作联合主键 | 弱 |
| `name` / `school` / `degree` / `job_position` | 候选人基本信息 | 是(name) |
| **`phone` / `wechat`** | 联系方式（网页可见）| **强 PII** |
| **`resume_content`**（简历正文）/ `resume_path`(仅文件名) | 简历查看 | **强 PII** |
| `score` / `score_summary` / `grade` / `scored_at` | 评分排行榜 | 否 |
| `status` / `interview_type` / `interview_date` / `interview_time` | 面试看板 | 否 |
| `chat_history` | 沟通记录（可选，体积大）| 含 PII |
| `updated_at` | 排序 / 增量同步 | — |

- **简历附件 PDF**：v1 先只同步 `resume_content`（正文文本）；原始 PDF 文件**暂留本地**（要在云上看 PDF 需接 Supabase Storage，列入 v2）。
- **强 PII 列（phone/wechat/resume_content/chat_history）建议在 DB 层做列级加密或至少 RLS 严格隔离 + 静态加密开启**——既然全量上云，这是底线。
- 主键：`(tenant_id, uid)`，`upsert` 幂等。

## 3. 组件与改动
- **`app/cloud_sync.py`（新，零依赖 urllib）**
  - `to_cloud(row)`：本地行 → 云端全字段映射（含 phone/wechat/resume_content）；带上 `tenant_id`
  - `push(rows)`：Supabase PostgREST `upsert`（`on_conflict=tenant_id,uid`），best-effort
  - **离线队列**：推失败写 `data/cloud_queue.jsonl`，下次补推，**绝不阻塞本地采集**
- **集成点**：`boss_pipeline` / `cua_collect` / `query_db --rank` 末尾按开关 `CLOUD_SYNC=on` 调 `push`
- **`scripts/cloud_sync.py`（新 CLI）**：`--push`（全量补推）/ `--dry-run`（看将上传什么，不真传）
- **`.env` 增**：`SUPABASE_URL` `SUPABASE_ANON_KEY` `CLOUD_AGENT_TOKEN`(用户身份令牌) `TENANT_ID` `CLOUD_SYNC`
- **Supabase**：`candidates` 表（全字段）+ **RLS(`tenant_id = auth.uid()`)** + **静态加密开启** + Auth(邮箱/magic link) + `device_tokens` 表（agent 令牌→user）
- **`web/`（新子目录，Cloudflare Pages）**：Vite + Supabase JS（前端有自己的 npm 依赖，与零依赖的 agent 解耦）；登录 → 排行榜 / 候选人 / 面试 三页，**纯只读**

## 4. 同步策略
- **单向** local→cloud；本地 SQLite 永远是真源。
- **幂等**：`upsert by (tenant_id, uid)`；删除走软删（status）不硬删。
- **触发**：每次 pipeline / 评分后 + 手动 `scripts/cloud_sync.py --push`。
- **容错**：网络 / Supabase 挂 → 入本地队列，下次补推。
- **频率**：批量、非实时（省额度）。

## 5. 鉴权（各用户自跑下）
1. 用户网页注册 → Supabase Auth 账号（= tenant）。
2. 网页生成一个**设备令牌**存 `device_tokens`（关联其 user_id）→ 用户贴进本地 `.env` 的 `CLOUD_AGENT_TOKEN`。
3. agent 用该令牌调 PostgREST，RLS 确保**只能写自己 tenant 的行**。
4. ⚠️ **绝不把 service_role key 下发给用户**（那是全库管理员权限，泄露 = 灾难）。

## 6. 合规与安全设计（全量上云 → 安全是第一要务）
- **你即数据控制者**：集中控管全部用户的全量候选人 PII → 合规责任**全在你**（非责任分散）。
- **强制免责**：注册时**勾选用户协议** + 小字声明「你对自己抓取并上传的数据负责」。
  - ⚠️ 免责只约束「你↔用户」，**不豁免「你↔候选人」的法定义务**（候选人未同意，见 §8）。
- **数据跨境**：Supabase **新加坡区**；隐私条款明示数据存于境外新加坡。
- **安全硬约束**（全量 PII 集中，下面每条都是底线，不是可选）：
  - **RLS 强隔离**（`tenant_id = auth.uid()`），跨租户零可见；网页必须登录才拿数据。
  - Supabase **静态加密**开启；强 PII 列（phone/wechat/resume）考虑 **pgcrypto 列级加密**。
  - 设备令牌**最小权限 + 可轮换**；**绝不**把 service_role key 下发给用户。
  - 前端**只读、无写入口**；service_role 仅你后台运维用，不进前端。
- ⚠️ 仍未消除的法律风险见 §8。

## 7. 分阶段路线图
| 阶段 | 内容 | 估时 |
|------|------|------|
| **P0 验证** | 建 Supabase 项目 + 1 张表 + 手动 curl upsert 一行脱敏数据，跑通最小闭环 | 0.5d |
| **P1 本地推送** | `cloud_sync.py`（脱敏+队列+push）+ pipeline 集成 + CLI；本地验证 | 1–2d |
| **P2 只读前端** | `web/`（Vite+Supabase JS）排行榜页 → Cloudflare Pages 部署；单租户跑通 | 2–3d |
| **P3 多租户+鉴权** | Auth + RLS + device_tokens + 注册/登录；两账号互不可见 | 2–3d |
| **P4 打磨** | 候选人/面试页、错误处理、`.env.example`、文档、人格三件套加「看板」引导 | 1–2d |

## 8. 风险（已知情接受，列此备忘）
1. **法律（最大）**：全量候选人 PII 集中在你云端、候选人**从未同意** → PIPL/GDPR 是法定义务，小字免责盖不住「你↔候选人」。**已确认按此推进；强烈建议正式对外前过一次法务。**
2. **拖库 = 大规模个人信息泄露**：phone/wechat/简历全集中一处 → 一旦被攻破后果严重 → §6 安全硬约束**必须全部落实**，不可省任何一条。
3. **BOSS ToS**：把候选人数据搬离平台存自有云仍可能违反其用户协议。
4. **令牌安全**：设备令牌泄露 = 该用户数据可被读/写 → 必须**可轮换 + 最小权限**。
5. **成本 / 容量**：简历正文 + `chat_history` 全量上云占空间 → 注意 Supabase 免费档 500MB；超了升级或精简 `chat_history`（可只存最近 N 条）。

## 9. 验收标准
- [ ] 两个账号各自本地跑 → 网页**登录后各看各的全部数据**（含联系方式/简历），**互不可见**（RLS 生效）。
- [ ] **未登录**访问前端 → 拿不到任何数据。
- [ ] Supabase **静态加密已开启**；**service_role key 不出现在前端构建产物**里。
- [ ] 注册流**强制勾选用户协议/免责**后才能使用。
- [ ] 断网时本地采集不受影响，恢复后自动补推（队列生效）。
- [ ] 本地 SQLite 仍是完整真源 + `data/backups` 正常。
- [ ] 前端只读，无任何写库入口（v1）。
