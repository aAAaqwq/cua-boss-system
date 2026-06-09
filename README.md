# cua-boss-system

通过 cua-driver 驱动 Chrome（macOS Accessibility API）实现 BOSS直聘招聘自动化。

> **平台限制**: 仅支持 **macOS 12+ (Monterey 及以上)**，不支持 Linux/Windows。cua-driver 依赖 macOS Accessibility API。
>
> **测试环境**: Python 3.14.5 / cua-driver 0.5.1 / Swift 6.3.2 / Chrome 148 / macOS 26.5

## 依赖

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | ≥ 3.10 | 零 pip 依赖，纯标准库 |
| [cua-driver](https://github.com/cua-driver/cua-driver-rs) | ≥ 0.5.x | macOS `.app`，通过 Accessibility API 操控 Chrome |
| Xcode Command Line Tools (swiftc) | 任意 | 首次运行自动编译 CGEvent 鼠标工具到 `/tmp/cua_hid` |
| Google Chrome | 任意 | 需登录 BOSS直聘 |
| DeepSeek API | — | 智能回复（必须提前配置，未配置时降级为模板原文） |

### Chrome 必要设置

启动 Chrome 前确保开启：**菜单栏 → 显示 → 开发者 → ☑️ 允许来自 Apple 事件的 JavaScript**

这是 cua-driver `page` 命令执行 JS 的前提，缺失会导致职位同步和 uid 提取失败。

## 快速开始

```bash
# 1. 配置 DeepSeek API Key（可选，推荐）
cp .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY

# 2. 同步职位信息
python scripts/cua_sync_jobs.py --write

# 3. 收集候选人（简历+微信→SQLite）
python scripts/cua_collect.py --limit 10

# 4. 预览沟通回复（推荐先 dry-run）
python scripts/cua_chat_loop.py --dry-run

# 5. 预览主动打招呼
python scripts/cua_greeting_loop.py --dry-run
```

**推荐流程**: 先 `collect` 收集简历和微信 → 再 `chat_loop` 智能沟通（会读取 collect 写入的 DB 上下文）

## 脚本

### `cua_chat_loop.py` — 沟通页批量智能沟通

打开聊天页，逐个查看联系人，自动判断并执行：学校/学历筛选 → 不合适 → 阶段感知 → 智能回复。

```bash
python scripts/cua_chat_loop.py                   # 最多20人
python scripts/cua_chat_loop.py --dry-run          # 预览
python scripts/cua_chat_loop.py --limit 10         # 限制人数
python scripts/cua_chat_loop.py --min-degree 硕士   # 最低学历
python scripts/cua_chat_loop.py --schools "清华,北大" # 自定义学校
```

**核心流程**:
```
scan_contacts() → 逐个 review_one_candidate():
  ├─ click_contact()       点击联系人 + 提取 DOM data-id → uid
  ├─ _clear_input()        清空输入框（Cmd+A + Delete，兼容 React）
  ├─ read_conversation()   读右侧面板 → 学校/学历/聊天历史
  ├─ _load_candidate_context()  查 DB: has_resume, has_wechat, 历史聊天
  ├─ _compute_stage()      推算对话阶段:
  │     ├─ ready_for_interview  (简历+微信都有 → 推动约面试)
  │     ├─ has_resume_no_wechat (有简历 → 不问简历，聊岗位/微信)
  │     ├─ has_wechat_no_resume (有微信 → 不问微信，聊岗位细节)
  │     └─ early_stage         (新对话 → 正常流程)
  ├─ 学校/学历筛选         不在白名单 → click_buheshi()
  ├─ generate_reply()      模板匹配 + DeepSeek 生成:
  │     ├─ system_prompt.md  顶尖 HR 招聘专家人设
  │     ├─ 合并 DB+AX 聊天历史（最多20条）
  │     ├─ 岗位模板作提示词方向
  │     └─ 阶段上下文约束（不重复问已有信息）
  ├─ _reply_redundant()    兜底检查: 回复还在问简历/微信 → 阶段兜底文本
  ├─ type_reply()          清空输入框 + cua type 输入 (dry-run 不发送)
  └─ _save_chat_history()  聊天记录 upsert → candidates.db (按 uid 匹配)
```

### `cua_collect.py` — 沟通页批量收集（简历+微信→SQLite）

```bash
python scripts/cua_collect.py --dry-run
python scripts/cua_collect.py --limit 10
python scripts/cua_collect.py --min-degree 硕士
```

流程: 进入聊天页 → AX树扫描联系人 → 逐个审查(学校+学历筛选) → 提取简历+微信 → upsert 到 candidates.db

**与 chat_loop 共享 DB**: collect 写入 `has_resume`/`has_wechat`/`uid` 等，chat_loop 读取这些字段做阶段感知。

### `cua_greeting_loop.py` — 推荐页批量主动打招呼

```bash
python scripts/cua_greeting_loop.py --dry-run
python scripts/cua_greeting_loop.py --limit 5
python scripts/cua_greeting_loop.py --min-degree 硕士
```

### `cua_sync_jobs.py` — 职位管理页职位信息同步

```bash
python scripts/cua_sync_jobs.py             # 预览
python scripts/cua_sync_jobs.py --write     # 提取+覆盖写入 config/jobs.json
```

### `boss_click_buheshi.py` — "不合适"点击模块（调试/独立使用）

CGEvent 原生鼠标 hover + click，绕过 BOSS 的 Vue 事件系统。已被 `cua_collect.py` 和 `cua_chat_loop.py` 作为共享模块 import 使用。

```bash
python scripts/boss_click_buheshi.py    # 独立调试
```

### `query_db.py` — 数据库查询/统计/导出

```bash
python scripts/query_db.py              # 统计概览
python scripts/query_db.py --list       # 列出全部候选人
python scripts/query_db.py --export candidates.csv  # 导出 CSV

## 配置

### `config/jobs.json` — 岗位配置

`cua_sync_jobs.py --write` 自动从 BOSS 职位管理页同步生成。手动编辑添加 `category` 字段。

```json
{
  "version": 6,
  "jobs": [
    {
      "id": "dev",
      "title": "开发",
      "category": "tech",
      "requirements": "需要5-10年的Java开发经验，有架构经验",
      "salary": "16K-30K",
      "degree": "本科",
      "location": "广州天河区..."
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `id` | 唯一标识，对应 `templates.json` 中的 job_id |
| `category` | `tech` 技术岗 / `nontech` 非技术岗，决定类别模板 |
| `title/requirements/salary/degree/location` | 同步自 BOSS，也用作 `{变量}` 占位符值 |

### `config/templates.json` — 话术提示词模板

三层匹配结构，支持 `{salary}` `{location}` `{title}` `{requirements}` `{degree}` 占位符。

```
templates.json
├── jobs/           # 岗位专属模板（按 job_id）
│   ├── dev (10个)
│   ├── annotation (11个)
│   └── annotation-2 (11个)
├── categories/     # 类别通用模板（同 category 岗位共享）
│   ├── tech (7个)      — 技术栈/架构/经验/远程/开源/AI/项目
│   └── nontech (7个)   — KPI/战略/成长/管理/数据/资源/创业
└── fallback/       # 全局兜底模板（16个）
                    — 薪资/简历/面试/地点/福利/试用期/团队/晋升/加班/婉拒/微信...
```

**回复流程**：
```
候选人消息
  → 模板匹配(专属→类别→兜底) → 命中模板作提示词方向
  → 加载 DB 上下文(has_resume/has_wechat/历史聊天) → 推算对话阶段
  → DeepSeek(system_prompt.md + 阶段约束 + 岗位信息 + 聊天历史) → AI生成回复
    ↓ 未配置或失败
  → 降级返回模板原文
    ↓ 回复仍冗余(问已有的东西)
  → 阶段兜底文本替换
```

### `config/system_prompt.md` — DeepSeek 系统提示词

维护招聘官人设、对话推进策略、禁忌事项。修改此文件即时生效，无需改代码。

### `.env` — DeepSeek API 配置（必须提前配置）

```bash
cp .env.example .env  # 编辑 .env 填入 DEEPSEEK_API_KEY
```

```ini
DEEPSEEK_API_KEY=sk-your-api-key-here
# DEEPSEEK_BASE_URL=https://api.deepseek.com
# DEEPSEEK_MODEL=deepseek-chat
```

未配置时不会报错，但所有智能回复降级为模板原文，回复质量显著下降。

## 评分系统 (`app/scoring.py` + `config/scoring.json`)

多维度 AI 评分，满分 100。按岗位可自定义维度和权重，全部维度统一走 DeepSeek 一次 API 调用。

### 快速使用

```bash
# 命令行交互评分（从 candidates.db 读取）
python3 app/scoring.py
```

```python
from app.scoring import evaluate_candidate, format_score_report

score = evaluate_candidate(
    candidate_data={"name": "张三", "school": "华中科技大学", "degree": "硕士", ...},
    job_id="dev", category="tech",
    job_context="开发 — 需要5-10年Java经验",
)
print(format_score_report(score, verbose=True))
# 输出: 总分/100 + 评级 + 每维度分条 + 打分依据
```

### 维度配置（`config/scoring.json`）

两层配置，岗位覆盖优先：

| 来源 | 岗位 | 维度（权重降序） |
|---|---|---|
| tech 默认 | `dev` | 技术深度(35) 项目质量(30) 工具链匹配(15) 教育背景(8) 工作经验(7) 沟通表达(5) |
| nontech 默认 | `annotation` | 行业经验(25) 业绩成果(25) 资源网络(15) 管理能力(15) 教育背景(10) 沟通表达(10) |
| 岗位覆盖 | `annotation-2` | 战略思维(25) 落地执行(25) 学习能力(15) 管理能力(15) 教育背景(10) 沟通表达(10) |

新增岗位只需在 `scoring.json` 加维度配置（权重和=100），无需改代码。

### 评分流程

```
维度解析 → prompt 拼接（候选人信息 + 聊天记录 + 维度清单）→ DeepSeek 评估 → 加权汇总 → 报告
```

### 评级

| 总分 | ≥85 | ≥70 | ≥55 | ≥40 | <40 |
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
| `status` | TEXT | collected / replied / unsuitable |

**跨脚本协作**: `collect` 写入简历+微信 → `chat_loop` 读取做阶段感知 → 不重复问已有信息

## 项目结构

```
cua-boss-system/
├── app/
│   ├── db.py                 # 共享数据库模块(init_db / DB_PATH)
│   ├── filter_criteria.py    # 名校白名单(985/211/海外) + 学校匹配 + 学历判断
│   ├── chat_reply.py         # 模板匹配 + DeepSeek + 岗位检测 + 阶段感知 + 变量替换
│   └── scoring.py            # 候选人评分系统(AI多维度/按岗位自定义权重)
├── config/
│   ├── jobs.json             # 岗位配置（cua_sync_jobs.py --write 同步）
│   ├── templates.json        # 话术模板（专属→类别→兜底 三层）
│   ├── scoring.json          # 评分维度配置（按类别默认/岗位覆盖/权重100）
│   └── system_prompt.md      # DeepSeek 系统提示词（HR 招聘专家人设）
├── scripts/
│   ├── boss_click_buheshi.py   # "不合适"点击共享模块（CGEvent原生鼠标）
│   ├── cua_chat_loop.py        # 沟通页批量智能沟通（阶段感知+上下文合并）
│   ├── cua_collect.py          # 沟通页批量收集（简历+微信→SQLite）
│   ├── cua_greeting_loop.py    # 推荐页批量主动打招呼
│   ├── cua_sync_jobs.py        # 职位管理页同步岗位信息
│   └── query_db.py             # 数据库查询/统计/CSV导出
├── data/
│   └── candidates.db         # 候选人数据（collect+chat_loop 共享）
├── .env.example              # DeepSeek API 配置模板
├── SKILL.md                  # Agent 操作手册
├── CLAUDE.md                 # Claude 上下文文件
└── README.md                 # 本文件
```

## cua-driver 集成要点

- BOSS 聊天页联系人：`<span class="geek-name">` + JS 点击 + `data-id` 提取 uid
- 职位描述在 iframe 内：JS 读 `iframe.contentDocument.querySelector('textarea').value`
- `cua()` 非 JSON 返回截断 200 字：JS 必须返回 `JSON.stringify({status, uid})`
- 页面导航后索引全变：用标题/文本匹配，不用位置索引
- 连续操作触发风控：每岗间隔 1.5-3 秒随机
- 输入框清空：Cmd+A + Delete 模拟键盘操作，兼容 React/Vue 框架
- 所有脚本支持 `--dry-run` 预览模式
