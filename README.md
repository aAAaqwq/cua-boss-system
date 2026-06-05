# cua-boss-system

通过 cua-driver 驱动 Chrome 实现 BOSS直聘自动化：打招呼、批量聊天回复、候选人审查、岗位同步。

## 依赖

| 依赖 | 说明 |
|------|------|
| Python 3.10+ | 零 pip 依赖，纯标准库 |
| [cua-driver](https://github.com/cua-driver/cua-driver-rs) | macOS Accessibility API 操控 Chrome |
| Chrome | 需登录 BOSS直聘 |

## 快速开始

```bash
# 候选人审查（推荐入口）— 逐个查看未读，学校筛选，智能回复
python scripts/cua_review_loop.py --dry-run

# 确实要发送时去掉 --dry-run
python scripts/cua_review_loop.py --limit 10

# 岗位同步 — 从职位管理页提取岗位详情到 config/jobs.json
python scripts/sync_jobs.py --write

# 批量打招呼 — 推荐页扫描候选人
python scripts/cua_greeting_loop.py --dry-run

# 批量聊天回复 — 模板匹配所有未读
python scripts/cua_chat_loop.py --dry-run
```

## 脚本说明

### `cua_review_loop.py` — 批量回复（★ 推荐）

`cua_chat_loop.py` 的增强版，在批量回复基础上增加：
- 学校白名单筛选（不符合自动点"不合适"）
- 已回复判断（上一句是我们发的则跳过，避免重复回复）
- 岗位感知话术（根据候选人消息检测目标岗位，匹配专属模板）

```
打开聊天页 → 扫描未读 → 逐个点击
  ├─ 上一句是我们发的 → 跳过
  ├─ 学校不在白名单 → 点"不合适"
  ├─ 学历不达标 → 点"不合适"
  └─ 符合条件 → 岗位检测 → 专属话术 → 输入回复
```

```bash
python scripts/cua_review_loop.py                   # 最多20人
python scripts/cua_review_loop.py --dry-run          # 预览
python scripts/cua_review_loop.py --limit 10         # 限制人数
python scripts/cua_review_loop.py --min-degree 硕士   # 学历要求
python scripts/cua_review_loop.py --schools "清华,北大" # 自定义学校
```

### `sync_jobs.py` — 岗位同步

从 BOSS 职位管理页提取岗位详情，覆盖写入 `config/jobs.json`：

```
进入职位管理 → 扫描开放中岗位 → 逐个点编辑
  → 提取 title/requirements/salary/degree/location
  → 覆盖写入 jobs.json（保留已有话术模板）
```

```bash
python scripts/sync_jobs.py             # 预览
python scripts/sync_jobs.py --write     # 提取+写入
python scripts/sync_jobs.py --limit 3   # 调试
```

### `cua_greeting_loop.py` — 推荐页打招呼

刷新推荐牛人页面 → AX 树扫描 → 学校筛选 → 点击"打招呼"：

```bash
python scripts/cua_greeting_loop.py --dry-run
python scripts/cua_greeting_loop.py --limit 5
python scripts/cua_greeting_loop.py --schools "清华,北大"
```

### `cua_chat_loop.py` — 批量回复（基础版）

扫描未读 → 读对话 → 模板匹配 → 发送。无学校筛选和已回复判断，推荐用 `cua_review_loop.py` 替代：

```bash
python scripts/cua_chat_loop.py --dry-run
python scripts/cua_chat_loop.py --limit 20
```

### `extract_jobs_bookmarklet.js` — 手动提取

在 Chrome Console 粘贴运行，批量提取岗位编辑页数据。用于 cua-driver 不可用时的 fallback。

## 配置

### `config/jobs.json` — 岗位配置

```json
{
  "version": 3,
  "jobs": [
    {
      "id": "dev",
      "title": "开发",
      "requirements": "需要5-10年的Java开发经验，有架构经验",
      "location": "广州",
      "salary": "16K-30K",
      "degree": "本科",
      "templates": []
    }
  ],
  "fallback_templates": [
    { "id": "fallback", "match_keywords": [], "reply": "收到，我稍后看一下回复你～", "priority": 99 }
  ]
}
```

### `config/chat_templates.json` — 话术模板（旧格式）

```json
{
  "templates": [
    {
      "id": "greeting_reply",
      "match_keywords": ["你好", "您好", "hi", "hello"],
      "reply": "...",
      "priority": 1
    }
  ]
}
```

回复策略：模板优先 → DeepSeek API 兜底 → fallback 文本。设置 `DEEPSEEK_API_KEY` 环境变量启用 AI 生成。

## 项目结构

```
cua-boss-system/
├── app/
│   ├── filter_criteria.py    # 名校白名单 + 学校匹配 + FilterCriteria
│   └── chat_reply.py         # 模板匹配 + DeepSeek API + 岗位检测
├── config/
│   ├── jobs.json             # 岗位配置（sync_jobs.py 自动同步）
│   └── chat_templates.json   # 话术模板
├── scripts/
│   ├── cua_review_loop.py    # ★ 候选人审查（学校筛选+回复）
│   ├── cua_greeting_loop.py  # 推荐页打招呼
│   ├── cua_chat_loop.py      # 聊天页批量回复
│   ├── sync_jobs.py          # 岗位同步
│   └── extract_jobs_bookmarklet.js  # 手动提取
├── CLAUDE.md
└── README.md
```
