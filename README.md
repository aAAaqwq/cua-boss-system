# cua-boss-system

通过 cua-driver 驱动 Chrome（macOS Accessibility API）实现 BOSS直聘招聘自动化。

> **平台限制**: 仅支持 **macOS 12+ (Monterey 及以上)**，不支持 Linux/Windows。cua-driver 依赖 macOS Accessibility API。
>
> **测试环境**: Python 3.14.5 / cua-driver 0.5.1 / Swift 6.3.2 / Chrome 148 / macOS 26.5

## 依赖

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.10 | 零 pip 依赖，纯标准库 |
| [cua-driver](https://github.com/cua-driver/cua-driver-rs) | >= 0.5.x | macOS `.app`，通过 Accessibility API 操控 Chrome |
| Xcode Command Line Tools (swiftc) | 可选 | cua-driver 自身可能需要；本项目代码不再编译 Swift 工具（点击走 cua-driver 的 CGEvent 像素点击） |
| Google Chrome | 任意 | 需登录 BOSS直聘 |
| DeepSeek API | -- | 智能回复（必须提前配置，未配置时降级为模板原文） |

### Chrome 必要设置

启动 Chrome 前确保开启：**菜单栏 -> 显示 -> 开发者 -> [x] 允许来自 Apple 事件的 JavaScript**

这是 cua-driver `page` 命令执行 JS 的前提，缺失会导致职位同步和 uid 提取失败。

## 快速开始

```bash
# 0. 登录授权账号（🔒 许可门禁，必须，否则所有自动化脚本启动即拒跑）
python scripts/cloud_sync.py login --email <邮箱> --password <密码>
# 账号由管理员后台开通（公开注册已关闭）；已登录可跳过

# 1. 配置 DeepSeek API Key
cp .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY

# 2. 配置筛选条件（可选，使用默认白名单可跳过）
cp config/filter-template.json config/filter.json
# 编辑 config/filter.json 自定义学校白名单和学历要求

# 3. 同步职位信息
python scripts/cua_sync_jobs.py --dry-run   # 预览
python scripts/cua_sync_jobs.py             # 自动写入 config/jobs.json

# 4. 收集候选人（简历+微信->SQLite）
python scripts/cua_collect.py --limit 10

# 5. 预览沟通回复（推荐先 dry-run）
python scripts/cua_chat_loop.py --dry-run

# 6. 预览主动打招呼
python scripts/cua_greeting_loop.py --dry-run

# 7. 一条命令跑完整流程（打招呼 -> 收集 -> 沟通）
python scripts/boss_pipeline.py            # 默认 打招呼20 / 收集前20 / 沟通前20

# 8. 看评分排行榜（DeepSeek 自行判断岗位 + 评分 + 缓存）
python scripts/query_db.py --rank --days 2 --top 10

# 9. 预约面试
python scripts/cua_interview.py --uid <UID> --type 线上 --date 2026-06-20 --time 14:30
```

**推荐流程**: 先 `collect` 收集简历和微信 -> 再 `chat_loop` 智能沟通（会读取 collect 写入的 DB 上下文）。或直接用 `boss_pipeline.py` 一条命令串起三步。完整的端到端验收流程见 [SKILL.md](SKILL.md) 的「最佳测试实践」。

## 接入 OpenClaw（Quick Start）

把本项目装成 OpenClaw 上一个**专属招聘助手 agent「伯乐」**：它**永远以本项目为根**、每轮自动注入项目上下文、不外溢到别的项目。原理是 OpenClaw 的 per-agent `workspace` + `contextInjection:"always"`。

```bash
# 1. 备份平台主配置（务必）
cp ~/.openclaw/openclaw.json ~/.openclaw/openclaw.json.bak.$(date +%Y%m%d-%H%M%S)

# 2. 在 ~/.openclaw/openclaw.json 的 agents.list 数组里加一个 agent：
#    （model 段照抄同文件其它 agent；workspace 指向本项目目录）
#    {
#      "id": "bole", "name": "伯乐",
#      "workspace": "<本项目绝对路径>",
#      "model": { "primary": "<照抄其它 agent>", "fallbacks": ["..."] },
#      "identity": { "name": "伯乐", "theme": "BOSS直聘招聘自动化助手", "emoji": "🎯" },
#      "groupChat": { "mentionPatterns": ["伯乐","招聘","BOSS直聘","打招呼","收简历","候选人"] }
#    }

# 3. 校验 JSON 合法（不合法别重启，先恢复备份）
python3 -m json.tool ~/.openclaw/openclaw.json >/dev/null && echo "✓ openclaw.json OK"

# 4. 重启 OpenClaw 服务，向「伯乐」发一句「跑一遍完整流程」即可
```

- ✅ **永远知道 + 锁定本项目**：`workspace`=项目目录 → 目录硬隔离，且每轮自动注入项目根的 `AGENTS.md`/`CLAUDE.md`/`TOOLS.md`/`IDENTITY.md`/`SOUL.md`。
- 🔒 **可选命令层硬约束 + 绑定专属入口**：给伯乐配 `exec-approvals.json` 白名单、`bindings` 绑 Telegram bot。
- 完整 5 步（含 exec 白名单、渠道绑定、诚实边界）见 [references/setup.md](references/setup.md) 的「接入 OpenClaw」节。

## 配置文件架构

所有配置文件采用 **template+local 双文件模式**：`-template.json` 提交到 git 作为参考，同名 `.json` 文件是本地方可自定义的运行文件（`.gitignore`）。

| 文件 | 作用 | 编辑方式 |
|------|------|----------|
| `.env` | DeepSeek API 密钥 | `cp .env.example .env` 后编辑 |
| `config/jobs.json` | 岗位配置（同步自 BOSS） | `cua_sync_jobs.py` 自动写入 |
| `config/jobs-template.json` | 岗位元数据模板（维护 id/category） | 手动编辑，sync 脚本自动合并 |
| `config/reply.json` | 本地话术配置（运行时读取） | `gen_reply_templates.py --write` 或手动编辑 |
| `config/reply-templates.json` | 话术模板参考（提交到 git） | 手动编辑，新增岗位时手动添加 |
| `config/filter.json` | 本地筛选条件（运行时读取） | `cp filter-template.json filter.json` 后编辑 |
| `config/filter-template.json` | 筛选条件模板（提交到 git） | 手动编辑 |
| `config/scoring.json` | 本地评分维度配置（运行时读取） | `cp scoring-template.json scoring.json` 后编辑 |
| `config/scoring-template.json` | 评分维度模板（提交到 git） | 手动编辑 |
| `config/system_prompt.md` | DeepSeek 系统提示词 | 手动编辑，即时生效 |

### 配置加载优先级

- **`filter.json` > `filter-template.json`** -- 运行时读取，不存在则用 template 兜底
- **`reply.json` > `reply-templates.json`** -- 运行时读取，不存在则用 template 兜底
- **`scoring.json` > `scoring-template.json`** -- 运行时读取，不存在则用 template 兜底
- **`jobs.json`（同步生成） + `jobs-template.json`（id/category 合并）** -- sync 脚本读取 template 获取元数据
- **`.env` 文件 > 环境变量** -- DeepSeek API 配置

## 脚本

### `cua_chat_loop.py` -- 沟通页批量智能沟通

打开聊天页，逐个查看联系人，自动判断并执行：学校/学历筛选 -> 不合适 -> **拒绝意图识别** -> 阶段感知 -> 智能回复。

> **拒绝识别（Issue #1）**：回复前用 DeepSeek 判候选人最新消息意图（`config/intent_prompt.md`）。**明显拒绝**（已入职/不合适/不看机会）→ 标「不合适」+ 依据写入 `notes`；**委婉拒绝**（再看看/考虑一下）→ 默认只停止追问、不标记（防误杀，可配 `config/reply.json` 的 `rejection_policy`）；不确定/DeepSeek 不可用 → 照常回复，绝不误标。dry-run 只预览。

```bash
python scripts/cua_chat_loop.py                   # 最多20人
python scripts/cua_chat_loop.py --dry-run          # 预览
python scripts/cua_chat_loop.py --limit 10         # 限制人数
python scripts/cua_chat_loop.py --min-degree 硕士   # 最低学历
python scripts/cua_chat_loop.py --schools "清华,北大" # 自定义学校
```

**核心流程**:
```
scan_contacts() -> 逐个 review_one_candidate():
  |- click_contact()       点击联系人 + 提取 DOM data-id -> uid
  |- _clear_input()        清空输入框（Cmd+A + Delete，兼容 React）
  |- read_conversation()   读右侧面板 -> 学校/学历/聊天历史
  |- _load_candidate_context()  查 DB: has_resume, has_wechat, 历史聊天
  |- _compute_stage()      推算对话阶段:
  |     |- ready_for_interview  (简历+微信都有 -> 推动约面试)
  |     |- has_resume_no_wechat (有简历 -> 不问简历，聊岗位/微信)
  |     |- has_wechat_no_resume (有微信 -> 不问微信，聊岗位细节)
  |     |- awaiting_response    (已请求等回复 -> 不重复请求)
  |     '- early_stage         (新对话 -> 正常流程)
  |- 学校/学历筛选          check_candidate() -> 不通过 -> click_buheshi()
  |- generate_reply()      模板匹配 + DeepSeek 生成:
  |     |- system_prompt.md  顶尖 HR 招聘专家人设
  |     |- 合并 DB+AX 聊天历史（最多20条）
  |     |- 岗位模板作提示词方向
  |     '- 阶段上下文约束（不重复问已有信息）
  |- _reply_redundant()    兜底检查: 回复还在问简历/微信 -> 阶段兜底文本
  |- type_reply()          清空输入框 + cua type 输入 (dry-run 不发送)
  '- _save_chat_history()  聊天记录 upsert -> candidates.db
```

**警告系统**: 启动时检查 DeepSeek API 配置（未配置时醒目警告）、AX 聊天历史提取失败时警告、AX+DB 聊天历史均为空时警告。

### `cua_collect.py` -- 沟通页批量收集（简历+微信->SQLite）

```bash
python scripts/cua_collect.py --dry-run
python scripts/cua_collect.py --limit 10
python scripts/cua_collect.py --min-degree 硕士
python scripts/cua_collect.py --no-score      # 收集后不自动评分
```

流程: 进入聊天页 -> AX树扫描联系人 -> 逐个审查（使用 `check_candidate()` 筛选）-> 提取简历+微信 -> upsert 到 candidates.db -> **收集结束后对本轮有简历者实时评分并缓存**

**收集后实时评分（默认开）**: Chrome 采集循环结束后，对本轮收集到、有简历正文的候选人即时调用 `evaluate_candidate_auto` 评分并 `record_score` 缓存（与 `query_db --rank` 同一路径、同一缓存字段），看排行榜时分数秒显示。best-effort：评分异常不影响采集结果。`--no-score` 可关闭（省 DeepSeek 调用）。

**与 chat_loop 共享 DB**: collect 写入 `has_resume`/`has_wechat`/`uid` 等，chat_loop 读取这些字段做阶段感知。

### `cua_greeting_loop.py` -- 推荐页批量主动打招呼

```bash
python scripts/cua_greeting_loop.py              # 扫描->筛选->打招呼（打满20人为止）
python scripts/cua_greeting_loop.py --dry-run    # 仅预览
python scripts/cua_greeting_loop.py --limit 10   # 打招呼10人（通过筛选并成功打招呼）
python scripts/cua_greeting_loop.py --limit max  # 打到每日上限自动停（也可写 上限/0）
python scripts/cua_greeting_loop.py --min-degree 硕士
python scripts/cua_greeting_loop.py --schools "清华,北大,浙大"
```

使用 `check_candidate()` 统一筛选入口。打招呼取卡片教育经历**最后一行**（时间最早=本科），非最高学历。

> **`--limit` 语义**：指**成功打招呼的人数**（通过筛选并点击成功），看过但被筛掉的卡片**不计入**。会自动多翻卡片直到打满 `--limit` 人，候选人不足时提前停止。预览模式下计「将打招呼」数。
> **`--limit max`/`上限`/`0` = 打到每日上限**：不设人数目标，撞 BOSS 每日上限弹窗或候选人耗尽才停（定时任务默认用此）。

### `cua_sync_jobs.py` -- 职位管理页职位信息同步

```bash
python scripts/cua_sync_jobs.py             # 提取 + 自动写入 config/jobs.json
python scripts/cua_sync_jobs.py --dry-run   # 仅预览不写入
python scripts/cua_sync_jobs.py --limit 3   # 只处理前 N 个
```

**关键特性**:
- 只提取"开放中"岗位，跳过"关闭"
- 同名岗位去重
- 从 `jobs-template.json` 合并 id/category 元数据
- 保留旧 jobs.json 中的话术模板
- 有 hard-refresh 兜底：SPA 导航失败时重新加载页面

### `gen_reply_templates.py` -- AI 生成话术模板

调用 DeepSeek 根据岗位信息自动生成专属话术模板，写入 `config/reply.json`。

```bash
# 为指定岗位生成（预览）
python scripts/gen_reply_templates.py --job-id "开发"

# 生成并直接写入 reply.json
python scripts/gen_reply_templates.py --job-id "开发" --write

# 为所有岗位生成
python scripts/gen_reply_templates.py --all --write
```

### `boss_click_buheshi.py` -- "不合适"点击模块（调试/独立使用）

点击走 **真鼠标渲染 + CGEvent 像素点击**（`isTrusted=true` 可信，避免反爬）。完整实测结论（缺一步就点不到）：

1. **理由项（reason-item）是 React 懒渲染**——不 hover 时 DOM 里 0 个。`JS dispatchEvent hover` 和 `AX show_menu` **都渲染不出**（实测 0 个）；**只有「真鼠标点『不合适』图标」**（CGEvent，光标落在图标上=真 `:hover`）能把菜单渲染进 DOM（实测 3/3，渲出 9 个 reason-item）。
2. 渲染后 **JS 强制展开**目标 reason-item（覆盖隐藏样式）→ 取 `getBoundingClientRect`，按 `scale=截图宽/视口宽` 换算成截图像素坐标 → **CGEvent `click {x,y}`** 真鼠标点击（实测 5/5，isTrusted=true）。
3. CGEvent 点击**需 Chrome 前台**（流水线后台跑时若 Chrome 不在前台会全部落空）→ 模块内部会先 `osascript activate`。
4. 菜单里「标为不合适」只是标题 `DIV.title`、**点不动**；真正可点的是 `.reason-item`（薪资不符/学历不符/期望不符/其他原因），点它才会真标记，默认选「其他原因」。
5. 这些元素都是 `AXStaticText`、只有 `showmenu/scrolltovisible`、**没有 press** → AX press / `element_index` 点击对其无效（能定位但点不动）；JS `el.click()` 能点但 `isTrusted=false` 会被反爬检测——故都不用。

已被 `cua_collect.py` 和 `cua_chat_loop.py` 作为共享模块 import 使用。

```bash
python scripts/boss_click_buheshi.py    # 独立调试
```

### `query_db.py` -- 数据库查询/统计/导出/排行榜

```bash
python scripts/query_db.py                          # 列出全部候选人（默认）
python scripts/query_db.py --name 张                 # 按名字搜索
python scripts/query_db.py --school 清华 --has-resume # 组合筛选
python scripts/query_db.py --stats                   # 统计概览
python scripts/query_db.py --export candidates.csv   # 导出 CSV
python scripts/query_db.py --rank --days 2 --top 10  # 评分排行榜（见下）
```

### `boss_pipeline.py` -- 全流程编排（打招呼 -> 收集 -> 沟通）

把三个脚本串成一条参数化流水线，顺序执行、前一步成功才进下一步。取代旧的 `boss-full-pipeline` skill。

```bash
python scripts/boss_pipeline.py                          # 打招呼20 / 收集前20 / 沟通前20（默认）
python scripts/boss_pipeline.py --greet max --collect 50 --chat 50   # 定时任务默认：打招呼到上限 + 收/聊各50
python scripts/boss_pipeline.py --greet 100 --collect 30 --chat 30
python scripts/boss_pipeline.py --min-degree 硕士 --schools "清华,北大"
python scripts/boss_pipeline.py --dry-run                 # 全程预览
python scripts/boss_pipeline.py --skip-greet              # 跳过已完成步骤续跑
```

`--greet/--collect/--chat` 控制各步 `--limit`（默认均 20）；`--min-degree/--schools/--dry-run` 透传给各步骤。任一步失败立即中断（退出码非 0）。

> **三个 `--limit` 语义不同，勿混淆**：
> - `--greet` = **成功打招呼的人数**（不符合筛选的不计，自动多翻卡片直到打满）；**填 `max`/`上限`/`0` = 打到每日上限自动停**
> - `--collect` = 联系人列表**顶部前 N 个**逐个处理（含被学校/学历筛掉、无简历跳过的）—— **不是**「收到 N 份简历」
> - `--chat` = 联系人列表**顶部前 N 个**逐个审查（含被筛掉、已回复跳过的）—— **不是**「回复 N 个人」

### `cua_interview.py` -- 预约面试

```bash
python scripts/cua_interview.py --uid 12345678 --date 2026-06-20 --time 14:30          # 线上（默认）
python scripts/cua_interview.py --uid 12345678 --type 线下 --date 2026-06-20 --time 10:00
python scripts/cua_interview.py --uid 12345678 --date 2026-06-20 --time 14:30 --dry-run # 预览不发送
```

进沟通页定位联系人 -> 打开面试邀请表单 -> 选类型/日期/时间 -> 发送。**成功后写回 DB**（`interview_*` 字段 + `status=interviewed`），从而被排行榜排除、被面试提醒读取。`--type` 仅 `线上`/`线下`。

### `interview_reminder.py` -- 面试提醒

```bash
python scripts/interview_reminder.py              # 今天+明天的面试（默认窗口1天）
python scripts/interview_reminder.py --within 3    # 未来3天内
python scripts/interview_reminder.py --date 2026-06-20
python scripts/interview_reminder.py --all         # 所有未来面试
python scripts/interview_reminder.py --notify      # 额外发 macOS 系统通知（适合定时任务）
```

读 `candidates.db` 中已预约面试并按日期排序展示。**纯读 DB 不操作 Chrome**，可安全做定时任务（如每天 6 点提醒）。

## 配置详解

### `config/jobs.json` -- 岗位配置（自动同步）

`cua_sync_jobs.py` 自动从 BOSS 职位管理页同步生成。字段值自动作为模板 `{salary}` `{location}` 等占位符的替换源。

```json
{
  "version": 3,
  "jobs": [
    {
      "title": "开发",
      "requirements": "需要5-10年的Java开发经验，有架构经验",
      "salary": "16K-30K",
      "degree": "本科",
      "location": "广州天河区...",
      "match_keywords": ["开发", "java", "架构"]
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `title` | **岗位名即唯一键**（无独立 id）。对应 `reply-templates.json` 的 `jobs.<岗位名>` 与 `scoring.json` 的 `job_overrides.<岗位名>` |
| `match_keywords` | （可选）岗位匹配业务关键词；不填则自动用岗位名+要求的中英文 token 匹配 |
| `requirements/salary/degree/location` | 同步自 BOSS，也用作 `{变量}` 占位符值；类别由 title+requirements 自动推断 tech/nontech |
| `boss_id` | （可选，自动）BOSS 真实 jobId，sync 尽力探测，仅备用 |

### `config/jobs-template.json` -- 岗位元数据模板（手动维护）

手动维护 `id` 和 `category` 映射，sync 脚本按 id 匹配合并。

```json
{
  "jobs": [
    {
      "id": "dev",
      "title": "开发",
      "category": "tech",
      "requirements": "需要5-10年的Java开发经验，有架构经验",
      ...
    }
  ]
}
```

新岗位流程: 在 BOSS 发布 -> `cua_sync_jobs.py` 同步 -> 检查 `jobs-template.json` 是否覆盖匹配 -> 若不匹配，手动在 `jobs-template.json` 添加 id/category。

### `config/reply.json` / `reply-templates.json` -- 话术模板

三层匹配结构，支持 `{salary}` `{location}` `{title}` `{requirements}` `{degree}` 占位符。

```
reply-templates.json
├── jobs/           # 岗位专属模板（key = 岗位名 title，无独立 id）
│   ├── 开发 (多条)
│   ├── 营销总监 (多条)
│   '- CEO助理 (多条)
├── categories/     # 类别通用模板（同 category 岗位共享）
│   ├── tech       -- 技术栈/架构/经验/远程/开源/AI/项目
│   '- nontech     -- KPI/战略/成长/管理/数据/资源/创业
'- fallback/       -- 全局兜底模板（16条）
                    -- 薪资/简历/面试/地点/福利/试用期/团队/晋升/加班/婉拒/微信...
```

**回复流程**：
```
候选人消息
  -> 模板匹配(专属->类别->兜底) -> 命中模板作提示词方向
  -> 加载 DB 上下文(has_resume/has_wechat/历史聊天) -> 推算对话阶段
  -> DeepSeek(system_prompt.md + 阶段约束 + 岗位信息 + 聊天历史) -> AI生成回复
    | 未配置或失败
    -> 降级返回模板原文
    | 回复仍冗余(问已有的东西)
    -> 阶段兜底文本替换
```

**关键行为**: DeepSeek 即使没有模板匹配也会被调用（模板是"建议方向"而非必需条件）。

### `config/filter.json` / `filter-template.json` -- 筛选条件配置

学校白名单 + 学历等级。运行时 `app/filter_criteria.py` 从 `filter.json` 加载，不存在则用 `filter-template.json` 兜底。

```json
{
  "school_whitelist": ["清华大学", "北京大学", ...],
  "min_degree": "本科",
  "degree_rank": {"博士": 4, "硕士": 3, "本科": 2, "大专": 1}
}
```

共 251 所学校（140 所国内 + 111 所海外），全部中文名。白名单可根据需要增删。

### `config/system_prompt.md` -- DeepSeek 系统提示词

维护招聘官人设、对话推进策略、禁忌事项。修改此文件即时生效，无需改代码。

### `.env` -- DeepSeek API 配置

```bash
cp .env.example .env  # 编辑 .env 填入 DEEPSEEK_API_KEY
```

```ini
DEEPSEEK_API_KEY=sk-your-api-key-here
# DEEPSEEK_BASE_URL=https://api.deepseek.com
# DEEPSEEK_MODEL=deepseek-chat
```

未配置时不会报错，但所有智能回复降级为模板原文，回复质量显著下降。脚本启动时会打印醒目警告。

### `config/scoring.json` / `scoring-template.json` -- 评分细则（统一可改）

一个文件管全部评分细则，改这里即生效、无需动代码。运行时优先读 `scoring.json`（本地，gitignore），不存在则用 `scoring-template.json` 兜底。

| 配置块 | 作用 |
|------|------|
| `category_defaults` / `job_overrides` | 评分维度 + 权重（岗位覆盖 > 类别默认，权重和=100） |
| `grades` | 评级分数线（S/A/B/C/D，`{min,label,desc}`） |
| `input_limits` | 传给 DeepSeek 的输入上限：`resume_max_chars`(4000) / `chat_max_turns`(30) / `rescore_window_days`(2) |

详见下方评分系统。

## 统一筛选模块 (`app/filter_criteria.py`)

所有脚本通过统一的 `check_candidate(school, degree, whitelist, min_degree) -> (passed, reason)` 接口做筛选。

```python
from app.filter_criteria import check_candidate, check_degree, ALL_ELITE_SCHOOLS

# 统一入口 -- 同时检查学校 + 学历
passed, reason = check_candidate("清华大学", "硕士")
# -> (True, None)

passed, reason = check_candidate("某某学院", "本科")
# -> (False, "学校不符 (某某学院 不在白名单)")

# 学历检查（向后兼容，chat_reply 也 import 此函数）
check_degree("硕士", "本科")  # -> True
```

**可扩展的 FilterCriteria**: `app/filter_criteria.py` 提供 `FilterCriteria` 数据类，当前支持 school_whitelist/min_degree/min_years，预留了 age_range/tech_stack/industry 等字段。

## 话术模板生成 (`scripts/gen_reply_templates.py`)

调用 DeepSeek 根据岗位要求自动生成专属话术模板，写入 `config/reply.json`。每个岗位生成 8-12 条场景模板，覆盖打招呼/薪资/技术栈/面试/福利等 10 个必备场景。

## 评分系统 (`app/scoring.py` + `config/scoring.json`)

多维度 AI 评分，满分 100。按岗位可自定义维度和权重，全部维度统一走 DeepSeek 一次 API 调用。

**岗位自动判断（默认）**: 评分前先让 DeepSeek 从开放岗位列表中判断候选人最匹配的岗位（`match_best_job` / `evaluate_candidate_auto`），再按该岗位类别取维度、用其 requirements 作上下文评分。`query_db.py --rank` 默认走此路径；`--job-id` 可强制指定跳过判断。

### 快速使用

```bash
# 评分入口 = 排行榜命令（DeepSeek 判断岗位 + 评分 + 缓存到 DB）
# app/scoring.py 是库模块，没有 CLI，通过 query_db --rank 或代码调用
python3 scripts/query_db.py --rank --days 2 --top 10
```

```python
# 推荐：DeepSeek 自行判断岗位后评分
from app.scoring import evaluate_candidate_auto, load_scoring_config, format_score_report
from app.chat_reply import load_jobs_config

jobs = load_jobs_config().get("jobs", [])
score = evaluate_candidate_auto(
    candidate_data={"name": "张三", "school": "华中科技大学", "degree": "硕士", ...},
    jobs=jobs, config=load_scoring_config(),
)
print(format_score_report(score, verbose=True))
# 输出: 总分/100 + 评级 + 每维度分条 + 打分依据

# 或显式指定岗位（跳过模型判断）
from app.scoring import evaluate_candidate
score = evaluate_candidate(candidate_data={...}, job_id="全栈开发",  # job_id 传岗位名
                           category="tech", job_context="...")
```

### 维度配置

| 来源 | 适用 | 维度（权重降序） |
|---|---|---|
| tech 默认 | 技术岗（如 `全栈开发`） | 技术深度(35) 项目质量(30) 工具链匹配(15) 教育背景(8) 工作经验(7) 沟通表达(5) |
| nontech 默认 | 非技术岗 | 行业经验(25) 业绩成果(25) 资源网络(15) 管理能力(15) 教育背景(10) 沟通表达(10) |
| 岗位覆盖 | `annotation-2` | 战略思维(25) 落地执行(25) 学习能力(15) 管理能力(15) 教育背景(10) 沟通表达(10) |

新增岗位只需在 `scoring.json` 加维度配置（权重和=100），无需改代码。

### 评级

| 总分 | >=85 | >=70 | >=55 | >=40 | <40 |
|---|---|---|---|---|---|
| 评级 | S 强烈推荐 | A 推荐 | B 可考虑 | C 待定 | D 不推荐 |

## 数据库 (candidates.db)

`app/db.py` 统一管理表结构和迁移，所有脚本共用。

| 字段 | 类型 | 说明 |
|------|------|------|
| `uid` | TEXT | BOSS 用户唯一标识（DOM data-id），跨脚本匹配键 |
| `name` | TEXT | 候选人姓名 |
| `school` / `degree` | TEXT | 学校 / 学历 |
| `resume_content` | TEXT | 简历全文（collect 写入） |
| `has_resume` | INTEGER | 是否已有简历 |
| `wechat` / `has_wechat` | TEXT / INTEGER | 微信号 / 是否已交换 |
| `chat_history` | TEXT | 聊天记录 JSON（chat_loop 写入） |
| `updated_at` | TIMESTAMP | 数据列变更时触发器自动刷新（评分/面试列不触发），`--rank` 据此判断数据是否变新 |
| `score` / `score_summary` / `scored_at` | REAL / TEXT / TIMESTAMP | 评分缓存（`query_db.py --rank` 懒写入） |
| `interview_type` / `interview_date` / `interview_time` / `interview_at` | TEXT | 已约面试（`cua_interview.py` 成功后写入） |
| `status` | TEXT | collected / replied / unsuitable / interviewed |

**跨脚本协作**: `collect` 写入简历+微信 -> `chat_loop` 读取做阶段感知 -> 不重复问已有信息；`--rank` 写评分、`cua_interview.py` 写面试，二者再被排行榜/提醒读取。

## 项目结构

```
cua-boss-system/
├── app/
│   ├── db.py                 # 共享数据库模块(init_db / DB_PATH / schema迁移)
│   ├── filter_criteria.py    # 统一筛选：check_candidate() + 名校白名单 + 学历等级
│   ├── chat_reply.py         # 模板匹配 + DeepSeek(阶段感知+上下文合并) + 岗位检测 + 拒绝意图识别
│   └── scoring.py            # 候选人评分系统(AI多维度/按岗位自定义权重)
├── config/
│   ├── jobs.json             # 岗位配置（cua_sync_jobs.py 自动同步，提交到 git）
│   ├── jobs-template.json    # 岗位元数据模板（手动维护 id/category，提交到 git）
│   ├── reply.json            # 本地话术配置（gitignore，运行时读取）
│   ├── reply-templates.json  # 话术模板参考（提交到 git）
│   ├── filter.json           # 本地筛选配置（gitignore，运行时读取）
│   ├── filter-template.json  # 筛选条件模板（提交到 git，251所学校）
│   ├── scoring.json          # 本地评分配置（gitignore，运行时读取）
│   ├── scoring-template.json # 评分维度模板（提交到 git，类别默认/岗位覆盖/权重100）
│   └── system_prompt.md      # DeepSeek 系统提示词（HR招聘专家人设）
├── scripts/
│   ├── boss_pipeline.py        # 全流程编排（打招呼->收集->沟通，参数化）
│   ├── boss_click_buheshi.py   # "不合适"点击共享模块（真鼠标点图标渲染菜单→CGEvent点reason-item，isTrusted=true，不用JS点击）
│   ├── cua_chat_loop.py        # 沟通页批量智能沟通（阶段感知+uid提取+上下文合并）
│   ├── cua_collect.py          # 沟通页批量收集（简历+微信->SQLite）
│   ├── cua_greeting_loop.py    # 推荐页批量主动打招呼
│   ├── cua_sync_jobs.py        # 职位管理页同步岗位信息
│   ├── cua_interview.py        # 预约面试（线上/线下+日期+时间，成功后写回DB）
│   ├── interview_reminder.py   # 面试提醒（读DB已约面试，可发macOS通知）
│   ├── gen_reply_templates.py  # AI生成话术模板（调用DeepSeek）
│   └── query_db.py             # 数据库查询/统计/CSV导出 + --rank评分排行榜
├── data/
│   ├── candidates.db         # 候选人数据（collect+chat_loop 共享）
│   └── backups/              # DB 备份目录（backup_db() 自动创建）
├── .env                      # DeepSeek API 配置（gitignore）
├── .env.example              # DeepSeek API 配置模板
├── references/               # Agent 参考文档
│   ├── setup.md              # 安装/运行前检查/接入 OpenClaw
│   ├── cli.md                # 命令速查 + 定时任务映射
│   ├── config.md             # 配置/话术/评分详解
│   └── faq.md                # 非技术用户场景应对手册
├── IDENTITY.md / SOUL.md / AGENTS.md  # CHRO助手「伯乐」人格三件套
├── SKILL.md / TOOLS.md       # Agent 操作手册 / 项目速查
├── CLAUDE.md                 # Claude 上下文文件
└── README.md                 # 本文件
```

## cua-driver 集成要点

- BOSS 聊天页联系人：`<span class="geek-name">` + JS 点击 + `data-id` 提取 uid
- 职位描述在 iframe 内：JS 读 `iframe.contentDocument.querySelector('textarea').value`
- `cua()` 非 JSON 返回截断 200 字：JS 必须返回 `JSON.stringify({status, uid})`
- 页面导航后索引全变：用标题/文本匹配，不用位置索引
- 连续操作触发风控：每步间隔 1.5-3 秒随机
- 输入框清空：Cmd+A + Delete 模拟键盘操作，兼容 React/Vue 框架
- 所有脚本支持 `--dry-run` 预览模式
- 列表页卡片结构：岗位名 AXLink -> 状态 StaticText -> 编辑 AXLink（岗位名在编辑**前面**）
