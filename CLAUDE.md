# cua-boss-system

cua-driver 驱动的 BOSS直聘自动化系统。

## 项目结构

```
cua-boss-system/
├── app/
│   ├── db.py                 # 共享数据库模块(init_db / DB_PATH / schema迁移)
│   ├── filter_criteria.py    # 名校白名单(985/211/海外) + 学校匹配 + 学历判断
│   ├── chat_reply.py         # 模板匹配 + DeepSeek(阶段感知+上下文合并) + 岗位检测
│   └── scoring.py            # 候选人评分系统(AI多维度/岗位自动判断/按岗位自定义权重)
├── config/                   # template+local 双文件: -template.json提交git, 同名.json本地gitignore
│   ├── jobs.json / jobs-template.json       # 岗位配置(cua_sync_jobs.py同步)，岗位名(title)即唯一键
│   ├── reply.json / reply-templates.json    # 话术模板(专属→类别→兜底 三层)
│   ├── filter.json / filter-template.json   # 筛选条件(名校白名单+学历)
│   ├── scoring.json / scoring-template.json # 评分维度(类别默认/岗位覆盖/权重100)
│   ├── scoring_prompt.md     # 评分系统提示词(顶尖HR简历评分专家人设+评分准则，.md维护)
│   └── system_prompt.md      # DeepSeek 系统提示词(HR招聘专家人设，.md维护)
├── scripts/
│   ├── boss_pipeline.py        # 全流程编排(打招呼→收集→沟通，参数化，取代boss-full-pipeline skill)
│   ├── boss_click_buheshi.py   # "不合适"点击共享模块(真鼠标点图标渲染菜单→CGEvent点reason-item，isTrusted=true，不用JS点击)
│   ├── cua_chat_loop.py        # 沟通页批量智能沟通(阶段感知+uid提取+上下文合并)
│   ├── cua_collect.py          # 沟通页批量收集(简历+微信→SQLite)
│   ├── cua_greeting_loop.py    # 推荐页批量主动打招呼
│   ├── cua_sync_jobs.py        # 职位管理页同步岗位信息
│   ├── cua_interview.py        # 预约面试(选线上/线下+日期+时间，成功后写回DB)
│   ├── interview_reminder.py   # 面试提醒(读DB已约面试，可发macOS通知，纯读不操作Chrome)
│   └── query_db.py             # 数据库查询/统计/CSV导出 + --rank评分排行榜
├── data/
│   └── candidates.db         # 候选人数据(collect+chat_loop 共享)
├── .env.example              # DeepSeek API 配置模板
├── SKILL.md
├── CLAUDE.md
└── README.md
```

## 依赖

- **平台**: macOS 12+ (Monterey 及以上)，不支持 Linux/Windows
- Python 3.10+（纯标准库）
- `cua-driver` CLI (≥ 0.5.x)
- `swiftc`（可选；cua-driver 自身可能需要，本项目代码不再编译 Swift 工具，点击走 cua-driver 的 CGEvent 像素点击）
- Chrome（需登录 BOSS直聘）
- **Chrome 设置**: 菜单栏 → 显示 → 开发者 → ☑️ 允许来自 Apple 事件的 JavaScript
- DeepSeek API（必须提前配置，未配置时降级为模板原文，回复质量显著下降）

## 脚本

### cua_greeting_loop.py — 推荐页批量主动打招呼

```bash
python scripts/cua_greeting_loop.py                  # 打招呼20人(默认)
python scripts/cua_greeting_loop.py --dry-run         # 预览
python scripts/cua_greeting_loop.py --limit 3         # 打招呼3人(通过筛选并成功打招呼)
python scripts/cua_greeting_loop.py --min-degree 硕士  # 最低学历
```

流程: 进入推荐页 → AX树扫描候选人(学校取教育经历最后一行=本科) → 学校白名单+学历筛选 → 逐个点击打招呼 → 检测上限弹窗

**`--limit` = 成功打招呼人数**(非读卡片数): 循环条件 `while greeted < limit`，看过但被筛掉的卡片不计入，自动多翻卡片直到打满 limit 人(候选人耗尽则提前停)。dry-run 计「将打招呼」数避免空转。

### cua_chat_loop.py — 沟通页批量智能沟通

```bash
python scripts/cua_chat_loop.py                   # 最多20人
python scripts/cua_chat_loop.py --dry-run          # 预览
python scripts/cua_chat_loop.py --limit 10         # 限制人数
python scripts/cua_chat_loop.py --min-degree 硕士   # 最低学历
python scripts/cua_chat_loop.py --schools "清华,北大" # 自定义学校
```

流程:
```
1. start_session()              启动 cua-driver session
2. find_boss_window()           定位 Chrome BOSS窗口
3. navigate_to_chat()           导航到沟通页
4. scan_all_contacts()          AX树扫描联系人列表 → [{name, job, msg, time}, ...]
5. db = sqlite3.connect(DB_PATH) 直连已有 candidates.db
6. 逐个 review_one_candidate(db=db):
   ├─ a. click_contact()        点击联系人 + 提取 data-id → (clicked, uid)
   ├─ b. _clear_input()         Cmd+A + Delete 清空输入框(兼容React)
   ├─ c. read_conversation()    读右侧面板(AX树):
   │     ├─ 学校 / 学历 / info_line
   │     ├─ chat_history: [{role, content}, ...]（含 system 消息）
   │     └─ last_sender → "boss" | "candidate"
   ├─ d. _load_candidate_context(db, uid, name)
   │     查 DB: has_resume, has_wechat, wechat, status, db_chat_history
   ├─ e. _compute_stage(ctx, chat_history)
   │     ├─ ready_for_interview  (简历+微信都有 → 推动约面试)
   │     ├─ has_resume_no_wechat (有简历 → 不问简历，聊岗位/微信)
   │     ├─ has_wechat_no_resume (有微信 → 不问微信，聊岗位细节)
   │     ├─ awaiting_response    (已请求等回复 → 不重复请求)
   │     └─ early_stage         (新对话 → 无约束)
   ├─ f. 已回复?                 last_sender == "boss" → 跳过
   ├─ g. 学校筛选               match_school() ✗ → click_buheshi()
   ├─ h. 学历筛选               check_degree() ✗ → click_buheshi()
   ├─ i. 消息检查               无候选人消息 → 跳过
   ├─ j. 智能回复:
   │     ├─ detect_job()        消息+职位 → 匹配岗位名(唯一键)
   │     ├─ 合并 DB+AX 聊天历史(去重保序, 最近20条)
   │     └─ generate_reply():
   │           ├─ 模板匹配(专属→类别→兜底) → template_hint
   │           ├─ DeepSeek(system_prompt.md + 阶段约束 + 岗位信息 + 历史)
   │           └─ 降级: DeepSeek不可用 → 模板原文
   ├─ k. _reply_redundant()     回复还在问简历/微信 → 阶段兜底文本替换
   ├─ l. type_reply()           _clear_input() + cua type 输入(dry-run不发送)
   └─ 返回 result (含 uid + chat_history)
7. _save_chat_history()         聊天记录 upsert → candidates.db
   ├─ 有 uid → WHERE uid = ? 精准匹配
   └─ 无 uid → WHERE name = ? fallback
8. check_limit_popup()          每轮检测上限弹窗 → 遇到则终止
9. 随机间隔 1.5-3s              防风控
```

**关键特性**:
- **阶段感知**: 读取 DB 中 has_resume/has_wechat，不重复问已有信息，推动对话前进
- **上下文合并**: DB 历史聊天 + AX 树实时聊天合并去重，传给 DeepSeek 做上下文
- **uid 提取**: 点击联系人时从 DOM data-id 提取用户唯一标识，跨脚本精准匹配
- **系统提示词**: `config/system_prompt.md` 维护 HR 专家人设，修改即时生效
- **输入框清空**: 模拟 Cmd+A + Delete 键盘操作，兼容 React/Vue 框架
- **冗余兜底**: DeepSeek 回复仍问已有信息时，自动替换为阶段兜底文本

### cua_collect.py — 沟通页批量收集（简历+微信→SQLite）

```bash
python scripts/cua_collect.py --dry-run
python scripts/cua_collect.py --limit 20     # 处理联系人列表前20个(含被筛掉/跳过的)
python scripts/cua_collect.py --min-degree 硕士
python scripts/cua_collect.py --no-score   # 收集后不自动评分
```

> **`--limit` 语义(默认 20)**: 从聊天联系人列表**顶部往下处理的联系人个数**(前 N 个)，
> **含被学校/学历筛掉、无简历跳过的**——即「处理前 N 个联系人」，**不是**「收集到 N 份简历」。
> 与打招呼的 `--limit`(=成功打招呼人数)语义不同，勿混淆。

流程: 进入聊天页 → AX树扫描联系人 → 逐个审查 → 提取uid+简历+微信 → upsert到candidates.db → **采集结束后实时评分**

**收集后实时评分（默认开，`--no-score` 关）**: Chrome 采集循环结束后，对本轮收集到、有简历正文的 uid 调 `auto_score_candidates()` → `evaluate_candidate_auto` + `record_score`，与 `query_db --rank` 同一路径同一缓存（score/score_summary/scored_at）。放在循环外批量做（不拖 Chrome 会话），best-effort（评分异常不影响采集与已入库数据）。看排行榜时分数已就绪，`--rank` 仍可刷新/重算。

**与 chat_loop 协作**: collect 写入 `has_resume`/`has_wechat`/`uid`，chat_loop 读取做阶段感知。共享 `app/db.py` 的 `init_db()` 保证表结构一致。

### cua_sync_jobs.py — 职位管理页职位信息同步

```bash
python scripts/cua_sync_jobs.py             # 预览
python scripts/cua_sync_jobs.py --write     # 提取+写入
python scripts/cua_sync_jobs.py --limit 3   # 调试
```

流程: 进入职位管理 → 扫描开放中岗位(同名去重) → 逐个点编辑:
- title: AXTextField(最短中文)
- requirements: JS直读iframe内textarea(绕过AX树200字截断)
- salary/degree/location: AXStaticText/AXTextField

### boss_pipeline.py — 全流程编排(打招呼→收集→沟通)

```bash
python scripts/boss_pipeline.py                          # 打招呼20/收集20/沟通20(默认)
python scripts/boss_pipeline.py --greet 100 --collect 30 --chat 30
python scripts/boss_pipeline.py --min-degree 硕士 --schools "清华,北大"
python scripts/boss_pipeline.py --dry-run                 # 全程预览
python scripts/boss_pipeline.py --skip-greet              # 跳过已完成步骤续跑
```

顺序执行 greeting→collect→chat，前一步退出码 0 才进下一步；任一步失败立即中断。`--greet/--collect/--chat` 控制各步 `--limit`，其余参数透传。取代旧的 boss-full-pipeline skill。

> **三个 `--limit` 语义不同，勿混淆**(默认均 20)：
> - `--greet` = **成功打招呼的人数**(被筛掉的不计，自动多翻卡片打满)
> - `--collect` = 联系人列表**顶部前 N 个**逐个处理(含被筛掉/无简历跳过的) —— 非「收到 N 份简历」
> - `--chat` = 联系人列表**顶部前 N 个**逐个审查(含被筛掉/已回复跳过的) —— 非「回复 N 个人」

### cua_interview.py — 预约面试

```bash
python scripts/cua_interview.py --uid 12345678 --type 线上 --date 2026-06-20 --time 14:30
python scripts/cua_interview.py --uid 12345678 --type 线下 --date 2026-06-20 --time 10:00 --dry-run
```

进沟通页定位联系人 → 打开面试邀请表单 → 选类型/日期/时间 → 发送。**成功后 `record_interview()` 写回 DB**(interview_* 字段 + status=interviewed)，dry-run/--no-db 不写。`--type` 仅 线上/线下。

### interview_reminder.py — 面试提醒

```bash
python scripts/interview_reminder.py              # 今天+明天(默认窗口1天)
python scripts/interview_reminder.py --within 3    # 未来3天
python scripts/interview_reminder.py --notify      # 额外发 macOS 系统通知
```

纯读 candidates.db 中已约面试(interview_date 非空)，按日期排序展示。**不操作 Chrome**，可安全做定时任务(如每天6点)。

### query_db.py --rank — 评分排行榜

```bash
python scripts/query_db.py --rank                    # 最近2天/前10/排除已面试(默认)
python scripts/query_db.py --rank --days 7 --top 20
python scripts/query_db.py --rank --no-score         # 只按已缓存分排,不调DeepSeek
python scripts/query_db.py --rank --rescore          # 强制重算
python scripts/query_db.py --rank --job-id "全栈开发"  # 强制指定岗位(传岗位名)
```

**评分对象**: (未评分 ∪ 数据近 N 天更新过且比上次评分新) **且有简历附件内容**(N=scoring.json input_limits.rescore_window_days,默认2;数据更新由 updated_at 触发器跟踪)。**按候选人沟通的职位 job_position 匹配岗位再评分并缓存**(score/scored_at) → 按总分降序展示「最近 --days 天活跃」未面试前 --top 名。--rescore 强制重算,--no-score 只读缓存,无 uid / 无简历者跳过。见下方评分系统。

## 话术模板 (config/reply.json / reply-templates.json)

三层匹配，**key 一律是岗位名(title)，无独立 job-id**：

| 层 | 配置位置 | key | match_layer |
|----|---------|-----|-------------|
| ①岗位专属 | `jobs.<岗位名>` | 岗位名(= jobs.json 的 title) | job |
| ②类别通用 | `categories.tech` / `categories.nontech` | 固定 tech/nontech，按 title+requirements 推断 | category |
| ③全局兜底 | `fallback` | 数组，无 key | fallback |

匹配策略: detect_job(沟通职位+消息) 命中岗位名 → 取①专属，否则②类别，再否则③兜底
          → 命中作 DeepSeek「建议回复方向」→ AI 结合上下文重写
          ↓ DeepSeek 未配置/失败
          降级返回模板原文

> **岗位标识 = 岗位名(title)**：不再有英文 job-id。岗位名本身唯一、可读，且与候选人
> 聊天的 `job_position` 一致，使 chat↔job↔reply-templates↔scoring 四处天然对齐。
> cua_sync_jobs 另尽力探测 BOSS 真实 jobId 存入 `boss_id` 字段备用（不作 key）。

## DeepSeek API 配置 (可选)

```bash
cp .env.example .env  # 填入 DEEPSEEK_API_KEY
```

配置优先级: `.env` 文件 → 环境变量。未配置时自动降级为模板原文。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | (无) | API 密钥，必须 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |

## 系统提示词 (config/system_prompt.md)

用 Markdown 维护招聘官人设和策略，修改即时生效无需改代码。包含:
- 核心原则（简短自然、推进对话、针对性）
- 对话推进策略（初次接触→了解背景→确认意向→约面试）
- 阶段回复方向表
- 禁忌事项（不重复问、不说废话、不复制粘贴）
- 特殊情况处理（发简历附件/简短回复/表达顾虑）

## 共享数据库 (app/db.py)

`init_db()` 统一管理表结构和迁移，所有脚本通过 `from app.db import init_db, DB_PATH` 共用。

candidates 表核心字段:
- `uid` — BOSS 用户唯一标识（DOM data-id），跨脚本匹配键
- `has_resume` / `has_wechat` — collect 写入，chat_loop 读取做阶段感知
- `chat_history` — 聊天记录 JSON，chat_loop 写入
- `updated_at` — 相关数据列(简历/微信/聊天等)变更时由触发器 `trg_candidates_touch` 自动刷新；评分/面试列不触发。用于 `--rank` 判断「数据是否比上次评分新」
- `score` / `score_summary` / `scored_at` — `query_db.py --rank` 懒评分缓存(scored_at 用 CURRENT_TIMESTAMP，与 updated_at 同 UTC 基准)
- `interview_type` / `interview_date` / `interview_time` / `interview_at` — `cua_interview.py` 预约成功后写入
- `status` — collected / replied / unsuitable / interviewed

## 筛选条件 (filter_criteria.py)

- 学校: 985/211/海外名校白名单
- 学历: 博士(4) > 硕士(3) > 本科(2) > 大专(1)
- 打招呼取卡片教育经历**最后一行**(时间最早=本科)，非最高学历

## 评分系统 (scoring.py + scoring.json)

多维度 AI 评分，满分 100。每个岗位可独立配置评分维度和权重。全部维度统一走 DeepSeek 一次 API 调用完成。

### 配置 (config/scoring.json / scoring-template.json)

运行时优先读 `scoring.json`(本地,gitignore)，不存在则用 `scoring-template.json` 兜底。**一个文件管全部评分细则，改这里即生效**：
```
category_defaults / job_overrides  →  维度+权重（岗位覆盖 > 类别默认，权重和=100）
grades                             →  评级分数线 S/A/B/C/D（{min,label,desc}）
input_limits                       →  传给 DeepSeek 的输入上限 + 重评窗口
                                       (resume_max_chars / chat_max_turns / rescore_window_days)
```

每个维度定义: `{key, name, weight, description}` — 纯描述，无需 `eval_type`。评级口径全项目走 `scoring.grade()` 单一入口。

### 评分前置：必须有简历附件内容

简历附件是主要评分依据。`resume_content` 为空 → 跳过评分（`skipped=True`，不调用 DeepSeek、不写分，保持未评分）。`query_db.py --rank` 在 SQL 层就过滤掉无简历者，并提示跳过人数。

### 岗位匹配：按「沟通的职位」（默认）

每段沟通都绑定一个 BOSS 招聘岗位（`candidates.job_position`）。评分针对候选人**实际沟通的那个岗位**，而非让模型从全部岗位里重新猜「最合适」：
- `match_job_by_position(candidate, jobs, config)` → 先用关键词 `detect_job(job_position + 最近候选人消息)` 匹配 jobs.json；未命中才回退 `match_best_job`（DeepSeek 判断）。返回 job_id（全失败→""）
- `evaluate_candidate_auto(candidate, jobs, config)` → 简历前置检查 + 岗位匹配 + 评分（一站式）

`query_db.py --rank` 默认走 auto 路径；`--job-id` 可强制指定岗位跳过匹配。

### 使用

```python
# 推荐: DeepSeek 自行判断岗位后评分
from app.scoring import evaluate_candidate_auto, load_scoring_config, format_score_report
from app.chat_reply import load_jobs_config

jobs = load_jobs_config().get("jobs", [])
score = evaluate_candidate_auto(
    candidate_data={"name": "张三", "school": "清华", "degree": "硕士", ...},
    jobs=jobs, config=load_scoring_config(),
)
print(format_score_report(score, verbose=True))

# 或显式指定岗位(跳过模型判断)
from app.scoring import evaluate_candidate
score = evaluate_candidate(candidate_data={...}, job_id="全栈开发",  # job_id 传岗位名
                           category="tech", job_context="...", config=load_scoring_config())
```

### 打分流程

```
0a.has_resume_content()                  → 无简历附件内容则跳过(skipped, 不评分)
0b.(auto)match_job_by_position()         → 按沟通职位 job_position 匹配岗位(--job-id 则跳过)
1. resolve_dimensions(job_id, category)  → 获取维度列表（覆盖 > 默认）
2. _build_scoring_prompt()               → 将全部维度 + 候选人信息 + 聊天记录
                                             拼接为评分 prompt
3. _call_deepseek_scoring()              → 一次 API 调用，返回所有维度分数+依据+总结
4. 加权汇总: raw_score / 10 × weight     → 总分
```

### AI prompt 内容

评分提示词分两层：
- **system 提示词**：`config/scoring_prompt.md` —— 顶尖 HR 简历评分专家人设 + 评分准则
  （岗位锚定 / 项目经历优先 / 证据驱动 / 警惕注水）+ 打分尺度 + 输出要求。改 .md 即时生效，
  无需动代码（与 chat 的 `system_prompt.md` 同模式，`scoring.py` 的 `load_scoring_prompt()` 缓存读盘）。
  文件缺失时降级为 `_FALLBACK_SCORING_PROMPT`。
- **user 消息（动态数据）**：岗位要求（job_context，来自 jobs.json）/ 候选人信息
  （姓名/职位/学校/学历/简历/备注）/ 聊天记录 / 评分维度清单（名称/权重/说明）/ 精确到维度 key 的 JSON 返回模板。

返回 JSON: `{"dimensions": {"key": {"score": 0-10, "evidence": "..."}}, "summary": "综合评价"}`

DeepSeek 未配置时所有维度标记为 0 分并在 errors 中提示。

## cua-driver 集成要点

- BOSS聊天页联系人: `<span class="geek-name">` + JS点击 + `data-id` 提取uid
- 职位描述在iframe内: JS读 `iframe.contentDocument.querySelector('textarea').value`
- cua()函数对非JSON返回截断200字: JS必须返回 `JSON.stringify({status, uid})`
- 列表页卡片结构: 岗位名AXLink → 状态StaticText → 编辑AXLink(岗位名在编辑**前面**)
- 页面导航后索引全变: 用标题匹配不用位置索引
- 连续操作触发风控: 每步间隔1.5-3s随机
- 输入框清空: Cmd+A + Delete 模拟键盘操作，兼容 React/Vue 框架
