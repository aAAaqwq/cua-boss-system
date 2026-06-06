# cua-boss-system

通过 cua-driver 驱动 Chrome（macOS Accessibility API）实现 BOSS直聘招聘自动化。

## 依赖

| 依赖 | 说明 |
|------|------|
| Python 3.10+ | 零 pip 依赖，纯标准库 |
| [cua-driver](https://github.com/cua-driver/cua-driver-rs) | macOS Accessibility API 操控 Chrome |
| swiftc (Xcode) | 编译 CGEvent 原生鼠标工具（首次自动编译到 `/tmp/cua_hid`） |
| Chrome | 需登录 BOSS直聘 |
| DeepSeek API（可选） | 智能回复，未配置时降级为模板原文 |

## 快速开始

```bash
# 1. 配置 DeepSeek API Key（可选，推荐）
cp .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY

# 2. 同步职位信息
python scripts/cua_sync_jobs.py --write

# 3. 预览沟通回复（推荐先 dry-run）
python scripts/cua_chat_loop.py --dry-run

# 4. 预览主动打招呼
python scripts/cua_greeting_loop.py --dry-run
```

## 脚本

### `cua_chat_loop.py` — 沟通页批量智能沟通

打开聊天页，逐个查看未读联系人，自动判断并执行：学校/学历筛选 → 不合适 → 岗位检测 → 智能回复。

```bash
python scripts/cua_chat_loop.py                   # 最多20人
python scripts/cua_chat_loop.py --dry-run          # 预览
python scripts/cua_chat_loop.py --limit 10         # 限制人数
python scripts/cua_chat_loop.py --min-degree 硕士   # 最低学历
python scripts/cua_chat_loop.py --schools "清华,北大" # 自定义学校
```

**回复流程**：模板匹配 → DeepSeek 结合上下文生成 → 不可用时降级模板原文。

### `cua_greeting_loop.py` — 推荐页批量主动打招呼

```bash
python scripts/cua_greeting_loop.py --dry-run
python scripts/cua_greeting_loop.py --limit 5
python scripts/cua_greeting_loop.py --min-degree 硕士
```

### `cua_collect.py` — 沟通页批量收集（简历+微信→SQLite）

```bash
python scripts/cua_collect.py --dry-run
python scripts/cua_collect.py --limit 10
python scripts/cua_collect.py --min-degree 硕士
```

### `cua_sync_jobs.py` — 职位管理页职位信息同步

```bash
python scripts/cua_sync_jobs.py             # 预览
python scripts/cua_sync_jobs.py --write     # 提取+覆盖写入 config/jobs.json
```

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
  → 模板匹配(专属→类别→兜底) 
    → DeepSeek(模板作提示词 + 岗位上下文 + 对话历史) → AI生成回复
      ↓ 未配置或失败
    降级返回模板原文
```

### `.env` — DeepSeek API 配置（可选）

```bash
cp .env.example .env
```

```ini
DEEPSEEK_API_KEY=sk-your-api-key-here
# DEEPSEEK_BASE_URL=https://api.deepseek.com
# DEEPSEEK_MODEL=deepseek-chat
```

未配置时自动降级为模板原文回复，不影响正常使用。

## 项目结构

```
cua-boss-system/
├── app/
│   ├── filter_criteria.py    # 名校白名单(985/211/海外) + 学校匹配 + 学历判断
│   └── chat_reply.py         # 模板匹配 + DeepSeek + 岗位检测 + 变量替换
├── config/
│   ├── jobs.json             # 岗位配置（cua_sync_jobs.py --write 同步）
│   └── templates.json        # 话术模板（专属→类别→兜底 三层）
├── scripts/
│   ├── boss_click_buheshi.py   # "不合适"点击共享模块（CGEvent原生鼠标）
│   ├── cua_chat_loop.py        # 沟通页批量智能沟通
│   ├── cua_collect.py          # 沟通页批量收集（简历+微信→SQLite）
│   ├── cua_greeting_loop.py    # 推荐页批量主动打招呼
│   └── cua_sync_jobs.py        # 职位管理页同步岗位信息
├── data/
│   └── candidates.db         # 候选人数据（cua_collect.py 输出）
├── .env.example              # DeepSeek API 配置模板
├── SKILL.md                  # Agent 操作手册
├── CLAUDE.md                 # Claude 上下文文件
└── README.md                 # 本文件
```

## cua-driver 集成要点

- BOSS 聊天页联系人：`<span class="geek-name">` + JS 点击
- 职位描述在 iframe 内：JS 读 `iframe.contentDocument.querySelector('textarea').value`
- `cua()` 非 JSON 返回截断 200 字：JS 必须返回 `JSON.stringify({text: ...})`
- 页面导航后索引全变：用标题/文本匹配，不用位置索引
- 连续操作触发风控：每岗间隔 3-7 秒随机
- Vue 事件代理：原生 CGEvent 点击绕过
- 所有脚本支持 `--dry-run` 预览模式
