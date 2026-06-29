# CLI 命令速查与脚本详解

> 每个脚本的命令、参数、流程与前置条件；外加 cua-driver 集成要点、常见操作示例、定时任务部署。
> 安装与检查清单见 [setup.md](setup.md)，配置与系统详解见 [config.md](config.md)。

## `cua_greeting_loop.py` -- 推荐页批量打招呼

```bash
# 预览（不实际操作）
python scripts/cua_greeting_loop.py --dry-run

# 最多判断20人，实际打几个取决于筛选通过人数（默认）
python scripts/cua_greeting_loop.py

# 限制判断数量
python scripts/cua_greeting_loop.py --limit 10

# 学历筛选（默认本科）
python scripts/cua_greeting_loop.py --min-degree 硕士

# 自定义学校白名单
python scripts/cua_greeting_loop.py --schools "清华,北大,浙大"

# 不刷新页面（用当前页数据）
python scripts/cua_greeting_loop.py --no-refresh
```

**流程**：进入推荐页 -> AX 树扫描候选人卡片 -> 取教育经历最后一行（本科）-> 使用 `check_candidate()` 统一筛选 -> 逐个点击"打招呼"-> 检测上限弹窗自动停止。

**筛选逻辑**: 使用 `app/filter_criteria.check_candidate(school, degree, whitelist, min_degree)` 统一入口。

**前置条件**：Chrome 已打开 BOSS 直聘推荐牛人页面。

---

## `cua_chat_loop.py` -- 沟通页批量智能沟通

```bash
# 预览（不实际操作）
python scripts/cua_chat_loop.py --dry-run

# 最多处理20个未读（默认）
python scripts/cua_chat_loop.py

# 限制数量
python scripts/cua_chat_loop.py --limit 10

# 学历筛选
python scripts/cua_chat_loop.py --min-degree 硕士

# 自定义学校白名单
python scripts/cua_chat_loop.py --schools "清华,北大,浙大"

# 控制滚动加载
python scripts/cua_chat_loop.py --no-scroll
python scripts/cua_chat_loop.py --scroll-pages 5
```

**流程**：进入聊天页 -> 滚动加载更多联系人 -> 扫描未读 -> 逐个审查：
1. 点联系人 -> 读对话面板（学校/学历/消息）
2. 使用 `check_candidate()` 统一筛选 -> 不通过 -> 点"不合适"
3. 符合条件 -> 岗位检测 -> 三层模板匹配 -> DeepSeek 生成 -> 输入回复

**警告系统**：
- 启动时检查 DeepSeek API 是否配置（未配置时打印醒目警告）
- AX 聊天历史提取失败时警告（右侧面板结构可能变化）
- AX+DB 聊天历史均为空时警告（DeepSeek 仅凭最新消息生成）

**前置条件**：Chrome 已打开 BOSS 直聘沟通页面。

---

## `cua_collect.py` -- 沟通页批量收集候选人

```bash
# 预览
python scripts/cua_collect.py --dry-run

# 前10个（默认）
python scripts/cua_collect.py

# 限制数量
python scripts/cua_collect.py --limit 5

# 学历筛选
python scripts/cua_collect.py --min-degree 硕士

# 自定义学校
python scripts/cua_collect.py --schools "清华,北大"
```

**流程**：进入聊天页 -> 滚动加载 -> AX 树扫描所有联系人 -> 逐个：
1. 学校/学历不符合 -> 使用 `check_candidate()` -> 点"不合适"
2. 符合条件 -> 点"附件简历"-> 提取信息 -> 换微信 -> 写入 SQLite

**数据输出**：`data/candidates.db`（SQLite）。

**前置条件**：Chrome 已打开 BOSS 直聘沟通页面。

---

## `cua_sync_jobs.py` -- 职位管理页同步岗位信息

```bash
# 预览 + 自动写入 config/jobs.json（默认行为）
python scripts/cua_sync_jobs.py

# 仅预览不写入
python scripts/cua_sync_jobs.py --dry-run

# 只处理前 N 个
python scripts/cua_sync_jobs.py --limit 3
```

**流程**：进入职位管理页 -> 扫描"开放中"岗位（同名去重）-> 逐个点编辑提取：
- `title`：AXTextField（最短中文）
- `requirements`：JS 直读 iframe 内 textarea（绕过 AX 200 字截断）
- `salary` / `degree` / `location`：AXStaticText/AXTextField

**关键行为**:
- 默认 **自动写入**（`--dry-run` 预览模式才跳过）
- 从 `jobs-template.json` 合并 id/category 元数据
- 保留旧 jobs.json 中的话术模板
- 有 hard-refresh 兜底：SPA 导航失败时重新加载页面

**前置条件**：Chrome 已打开 BOSS 直聘职位管理页面。

---

## `gen_reply_templates.py` -- AI 生成话术模板

```bash
# 为指定岗位生成模板（预览）
python scripts/gen_reply_templates.py --job-id dev

# 生成并直接写入 config/reply.json
python scripts/gen_reply_templates.py --job-id dev --write

# 为所有岗位生成
python scripts/gen_reply_templates.py --all --write
```

**流程**：加载 jobs.json 岗位信息 -> 调用 DeepSeek -> 解析 JSON 响应 -> 写入 reply.json（--write）。

**注意事项**：
- DeepSeek API 必须先配置
- 每个岗位生成 8-12 条模板
- 覆盖 10 个必备场景（打招呼、薪资、技术栈、工作经验、面试、团队、福利、加班、晋升、项目）

---

## `boss_click_buheshi.py` -- "不合适"点击模块（调试/独立使用）

```bash
# 独立调试
python scripts/boss_click_buheshi.py
```

此脚本被 `cua_collect.py` 和 `cua_chat_loop.py` 作为共享模块 import 使用。触发场景：**学校不在白名单** / **学历不达标**。

**流程**：AX 检测"不合适"-> CGEvent 原生鼠标 hover（触发下拉面板）-> AX 轮询等"标为不合适"面板展开（最多 15s）-> 原生点击 -> AX 验证。

---

## `boss_pipeline.py` -- 全流程编排（打招呼→收集→沟通）

```bash
# 默认：打招呼20 / 收集5 / 沟通5
python scripts/boss_pipeline.py

# 打到每日上限 + 各步放大
python scripts/boss_pipeline.py --greet 100 --collect 30 --chat 30

# 加筛选条件（透传给各步骤）
python scripts/boss_pipeline.py --min-degree 硕士 --schools "清华,北大"

# 全程预览不操作
python scripts/boss_pipeline.py --dry-run

# 中途失败修复后续跑（跳过已完成步骤）
python scripts/boss_pipeline.py --skip-greet --skip-collect
```

**流程**：顺序执行 `cua_greeting_loop.py` → `cua_collect.py` → `cua_chat_loop.py`，**前一步退出码为 0 才进下一步**；任一步失败立即中断并提示用 `--skip-*` 续跑。`--greet/--collect/--chat` 控制各步 `--limit`，`--min-degree/--schools/--dry-run` 透传。取代旧的 `boss-full-pipeline` skill。

---

## `query_db.py --rank` -- 评分排行榜

```bash
# 最近2天、前10、排除已面试（默认）
python scripts/query_db.py --rank

# 自定义窗口与人数
python scripts/query_db.py --rank --days 7 --top 20

# 不限时间 + 包含已面试
python scripts/query_db.py --rank --days 0 --include-interviewed

# 只按已缓存分数排（不调 DeepSeek）
python scripts/query_db.py --rank --no-score

# 强制重新评分
python scripts/query_db.py --rank --rescore

# 强制指定评分岗位/类别（自动检测不准时）
python scripts/query_db.py --rank --job-id ai-fullstack --category tech
```

**评分对象（默认）**：**未评分** 的候选人 ∪ **相关数据在 N 天内更新过且比上次评分新** 的候选人（N = `scoring.json` 的 `input_limits.rescore_window_days`，默认 2）。数据是否更新由 `candidates.updated_at` 判断 —— collect/chat_loop 改动简历/微信/聊天等列时由 DB 触发器自动刷新；评分(`scored_at`)/面试列不触发。`--rescore` 强制重算展示窗口内全部，`--no-score` 只读缓存。

**流程**：选出待评分候选人 → **先让 DeepSeek 判断最匹配岗位、再按该岗位维度评分并缓存**（写 `score`/`score_summary`/`scored_at`）→ 按总分降序展示「最近 `--days` 天活跃」（`COALESCE(updated_at, extracted_at)`）、未面试的前 `--top` 名 + 评级 + 简评。`--job-id` 可强制指定岗位跳过判断；无 `uid` 的候选人无法缓存，会被跳过并提示。

> 评分维度与权重在 `config/scoring.json`（见 [config.md](config.md)「评分系统」节）。`--rank` 不重复扣费：已评分的不会再调 API。

---

## `cua_interview.py` -- 预约面试

```bash
# 线上面试（默认 --type 线上）
python scripts/cua_interview.py --uid 12345678 --date 2026-06-20 --time 14:30

# 线下面试
python scripts/cua_interview.py --uid 12345678 --type 线下 --date 2026-06-20 --time 10:00

# 预览不发送
python scripts/cua_interview.py --uid 12345678 --date 2026-06-20 --time 14:30 --dry-run

# 不查/不写 DB（仅用 --name）
python scripts/cua_interview.py --name 张三 --date 2026-06-20 --time 10:00 --no-db
```

**流程**：按 `--uid` 在 DB 查候选人 → 进沟通页定位联系人 → 打开面试邀请表单 → 选类型/日期/时间 → 发送。**发送成功后写回 DB**（`interview_date/time/type` + `status=interviewed`，`--dry-run`/`--no-db` 不写）。`--date` 须 `YYYY-MM-DD`，`--time` 须 `HH:MM`，`--type` 仅 `线上`/`线下`。

**前置条件**：Chrome 已打开 BOSS 直聘沟通页面；候选人已在联系人列表中。

---

## `interview_reminder.py` -- 面试提醒

```bash
# 今天+明天的面试（默认窗口1天）
python scripts/interview_reminder.py

# 未来3天内
python scripts/interview_reminder.py --within 3

# 指定某天 / 所有未来面试
python scripts/interview_reminder.py --date 2026-06-20
python scripts/interview_reminder.py --all

# 额外发 macOS 系统通知（适合定时任务）
python scripts/interview_reminder.py --notify
```

**流程**：读 `candidates.db` 中已预约面试（`interview_date` 非空）→ 按 `--within`/`--date`/`--all` 过滤 → 按日期时间排序展示（含「今天/明天/X天后」标签、联系方式）。`--notify` 对每场面试发 macOS 通知。**纯读 DB，不操作 Chrome**，可安全做定时任务。

---

## cua-driver 集成要点（Agent 须知）

| 要点 | 说明 |
|------|------|
| 页面导航后索引全变 | 用标题/文本匹配，不要用位置索引 |
| 职位描述在 iframe 内 | JS 读 `iframe.contentDocument.querySelector('textarea').value` |
| cua() 非 JSON 截断 200 字 | JS 必须返回 `JSON.stringify({text: ...})` |
| 聊天页联系人 | `<span class="geek-name">` + JS click |
| 列表页卡片结构 | 岗位名 AXLink -> 状态 StaticText -> 编辑 AXLink |
| 连续操作触发风控 | 每步间隔 1.5-3 秒随机 |
| Vue 事件代理 | 原生 CGEvent 点击绕过 BOSS 的 Vue 事件系统 |
| "不合适"按钮 | `.operate-icon-item[8]`（第 9 个操作图标） |
| SPA 导航失败 | hard-refresh 兜底 -- 重新加载页面 URL |
| AX 聊天历史提取失败 | 不影响运行，但无上下文生成回复（有警告） |

---

## 常见操作示例

### 完整工作流：同步 -> 编辑 -> 收集 -> 沟通

```bash
# 1. 同步当前职位
python scripts/cua_sync_jobs.py

# 2. 检查 jobs-template.json 是否有新岗位的 id/category
#    如无，手动添加

# 3. 为新岗位生成话术模板
python scripts/gen_reply_templates.py --all --write

# 4. 收集候选人
python scripts/cua_collect.py --limit 10 --min-degree 硕士

# 5. 智能沟通
python scripts/cua_chat_loop.py --limit 20 --min-degree 硕士
```

### 调试特定候选人

```bash
# 预览模式只看不操作
python scripts/cua_chat_loop.py --dry-run --limit 5

# 只测试"不合适"点击
python scripts/boss_click_buheshi.py
```

### 调整筛选条件

```bash
# 创建本地配置（首次）
cp config/filter-template.json config/filter.json

# 编辑 config/filter.json，增删学校或改学历要求
# 运行时自动生效，无需重启

# 或者通过命令行临时覆盖
python scripts/cua_greeting_loop.py --schools "清华,北大" --min-degree 硕士
```

---

## 定时任务（由 agent 平台调度，无需专门脚本）

**设计原则**：本项目**不提供定时安装脚本**。脚本只负责「执行功能」（`boss_pipeline.py` 等），
「按点触发」交给**当前 agent 平台自己的定时机制**（各平台不同：内置 schedule / cron / 系统调度均可）。
agent 的职责是：把用户口语**映射成正确命令**，再用平台机制到点调用。

### 默认定时任务（推荐节奏）

> **每天 9 / 13 / 17 点各跑一遍全流程：打招呼到上限 + 收简历 50 + 智能沟通 50。**
> agent 在这三个时刻各调用一次：
>
> ```bash
> python scripts/boss_pipeline.py --greet max --collect 50 --chat 50
> ```
>
> `--greet max` = 打招呼**打到 BOSS 每日上限自动停**（不设人数目标）；`--collect/--chat 50` = 各处理 50 个联系人。

### 口语 → 命令 映射（agent 照此调用）

| 用户说 | agent 到点执行 |
|--------|----------------|
| 「每天 9/13/17 点跑一遍、打招呼到上限、收简历和沟通各 50」（**默认**） | 三个时刻各跑：`python scripts/boss_pipeline.py --greet max --collect 50 --chat 50` |
| 「每天上午只跑打招呼到上限」 | `python scripts/cua_greeting_loop.py --limit max` |
| 「每天早 6 点提醒今天面试」 | `python scripts/interview_reminder.py --within 1 --notify` |
| 「每周一同步岗位」 | `python scripts/cua_sync_jobs.py --write` |

映射要点：
- **「打招呼到上限」= `--greet max`（或 `上限`/`0`）**：不设人数目标，撞到 BOSS 每日上限弹窗会**自动停**，不会硬刚；候选人耗尽也会停。
- **`--collect N` / `--chat N`** = 联系人列表**顶部往下处理/审查的个数**（含被筛掉/跳过的），**不是**「收到 N 份简历」。
- 需要筛选时透传 `--min-degree 硕士`、`--schools "清华,北大"`。
- 排错可先 `--dry-run` 预览。`boss_pipeline.py` 任一步失败即中断（退出码非 0）。

### 到点必须满足（GUI 自动化前提，否则会「静默失败」）

> ⚠️ 除 `interview_reminder.py`（纯读 DB、不碰 Chrome，可在任意环境定时）外，其余脚本都驱动 Chrome，
> 必须跑在**已登录的桌面图形会话**里并持有 TCC 权限。普通 crontab 跑在非图形会话拿不到这些权限，
> 到点看似执行却没动 Chrome——**所以才交给跑在你登录会话里的 agent 平台去触发**。

1. 电脑**已开机并登录**且保持会话（建议关闭自动睡眠或配 `caffeinate`）；
2. Chrome 已登录 BOSS 直聘；
3. 触发 python 的程序已获**辅助功能 + 自动化**权限；
4. 已用下发账号 `login`（许可门禁，`boss_pipeline.py` 启动即校验）。
