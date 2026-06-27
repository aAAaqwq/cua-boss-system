-- ============================================================
-- cua-boss-system 云端 schema（Supabase / Postgres）
-- 在 Supabase 控制台 → SQL Editor 里执行一次。
-- 配套：app/cloud_sync.py（上传）、docs/cloud-sync-plan.md（规划）。
--
-- 安全硬约束（全字段含 PII 上云，见 plan §6）：
--   1) 本表启用 RLS，用户只能读自己租户(tenant_id = auth.uid())的行
--   2) Supabase 项目「Settings → Database」确认静态加密开启(默认开)
--   3) agent 用 service_role 写入(绕过 RLS)；service_role key 绝不进前端
--   4) 前端只用 anon key + 用户登录 JWT，受 RLS 约束、且只读
-- ============================================================

-- 候选人全字段镜像（本地 SQLite candidates 的下游，单向 local→cloud）
create table if not exists public.candidates (
  tenant_id      uuid        not null,           -- 租户 = 用户 auth.uid()
  uid            text        not null,           -- BOSS 用户标识（配 tenant 作联合主键）
  name           text,
  job_position   text,
  school         text,
  degree         text,
  resume_content text,                            -- 简历正文（强 PII）
  resume_filename text,
  resume_path    text,                            -- 仅文件名/本地路径，PDF 原件 v1 不上云
  has_resume     int,
  wechat         text,                            -- 微信（强 PII）
  has_wechat     int,
  phone          text,                            -- 手机（强 PII）
  email          text,
  score          real,
  score_summary  text,
  scored_at      timestamptz,
  status         text,
  chat_history   text,                            -- 沟通记录 JSON（含 PII，体积大）
  notes          text,
  interview_type text,
  interview_date text,
  interview_time text,
  interview_at   timestamptz,
  extracted_at   timestamptz,
  updated_at     timestamptz,
  synced_at      timestamptz default now(),       -- 云端落库时间
  primary key (tenant_id, uid)
);

-- 排行榜 / 增量排序索引（均带 tenant_id 前缀，配合 RLS）
create index if not exists idx_candidates_rank
  on public.candidates (tenant_id, score desc nulls last);
create index if not exists idx_candidates_updated
  on public.candidates (tenant_id, updated_at desc);

-- ── 行级安全（RLS）──
alter table public.candidates enable row level security;

-- 读：用户只能看自己租户的数据（网页只读靠这条隔离）
drop policy if exists "tenant_read_own" on public.candidates;
create policy "tenant_read_own" on public.candidates
  for select using (tenant_id = auth.uid());

-- 写：用户 JWT 只能写自己租户（agent 走 service_role 时绕过 RLS，不受此限）
drop policy if exists "tenant_write_own" on public.candidates;
create policy "tenant_write_own" on public.candidates
  for all using (tenant_id = auth.uid()) with check (tenant_id = auth.uid());


-- ============================================================
-- （可选，P3 多租户鉴权用）设备令牌表：把 agent 的长期令牌映射到 user
-- v1 先用 service_role 直推可不建；多用户上线前再启用。
-- ============================================================
create table if not exists public.device_tokens (
  token       text primary key,                  -- 发给 agent 的长期令牌（建议存哈希）
  user_id     uuid not null,                     -- 归属用户 = tenant_id
  label       text,                              -- 设备备注（如「我的Mac」）
  created_at  timestamptz default now(),
  last_used_at timestamptz,
  revoked     boolean default false
);
alter table public.device_tokens enable row level security;
drop policy if exists "own_tokens" on public.device_tokens;
create policy "own_tokens" on public.device_tokens
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());
