# cua-boss-system — Agent 操作手册

> cua-driver 驱动的 BOSS 直聘自动化。本文档供 Claude/Agent 操作项目使用。

## 项目概述

通过 `cua-driver` CLI 操控 Chrome 的 macOS Accessibility API，实现 BOSS 直聘的批量打招呼、智能沟通、候选人收集和职位同步。

**零 pip 依赖**，纯 Python 标准库。系统依赖：`cua-driver` CLI、`swiftc`（macOS 自带）、Chrome。

## 触发关键词 → 脚本路由

| 用户说... | 应执行... |
|-----------|-----------|
| 打招呼、主动联系、推荐页、批量打招呼、牛人打招呼、勾搭候选人 | `cua_greeting_loop.py` |
| 回复、沟通、聊天、智能回复、处理未读、批量回复、看消息 | `cua_chat_loop.py` |
| 收集简历、提取微信、收集候选人、批量收集、捞简历、采集 | `cua_collect.py` |
| 同步职位、更新岗位、提取岗位信息、刷新职位列表、岗位配置 | `cua_sync_jobs.py` |
| 不合适、点不合适、buheshi、标为不合适 | `boss_click_buheshi.py` |
| 白名单、学校筛选、学历筛选、筛选条件 | `app/filter_criteria.py` |
| 话术、回复模板、聊天模板、自动回复内容 | `app/chat_reply.py` / `config/templates.json` |
| 岗位配置、职位要求、job config | `config/jobs.json` |
| 干跑、预览、dry run、不实际操作 | 任意脚本加 `--dry-run` |
| 部署到服务器、crontab、定时任务、自动化排期 | 参考下方定时任务节 |

---

## CLI 命令速查

### `cua_greeting_loop.py` — 推荐页批量打招呼

```bash
# 预览（不实际操作）
python scripts/cua_greeting_loop.py --dry-run

# 最多打5个（默认）
python scripts/cua_greeting_loop.py

# 限制数量
python scripts/cua_greeting_loop.py --limit 10

# 学历筛选（默认本科）
python scripts/cua_greeting_loop.py --min-degree 硕士

# 自定义学校白名单
python scripts/cua_greeting_loop.py --schools "清华,北大,浙大"

# 不刷新页面（用当前页数据）
python scripts/cua_greeting_loop.py --no-refresh
```

**流程**：进入推荐页 → AX 树扫描候选人卡片 → 取教育经历最后一行（本科）→ 学校白名单 + 学历筛选 → 逐个点击"打招呼"→ 检测上限弹窗自动停止。

**前置条件**：Chrome 已打开 BOSS 直聘推荐牛人页面。

---

### `cua_chat_loop.py` — 沟通页批量智能沟通

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

# 自定义话术模板
python scripts/cua_chat_loop.py --config custom_templates.json

# 控制滚动加载
python scripts/cua_chat_loop.py --no-scroll
python scripts/cua_chat_loop.py --scroll-pages 5
```

**流程**：进入聊天页 → 滚动加载更多联系人 → 扫描未读 → 逐个审查：
1. 点联系人 → 读对话面板（学校/学历/消息）
2. 上一句是我们发的 → 跳过
3. 学校不在白名单 → 点"不合适"
4. 学历不达标 → 点"不合适"
5. 符合条件 → 岗位检测 → 专属话术 → 输入回复

**回复策略**：模板匹配（专属→类别→兜底）→ DeepSeek 以模板作提示词结合上下文生成 → 未配置/失败时降级模板原文。

**前置条件**：Chrome 已打开 BOSS 直聘沟通页面。

---

### `cua_collect.py` — 沟通页批量收集候选人

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

**流程**：进入聊天页 → 滚动加载 → AX 树扫描所有联系人 → 逐个：
1. 学校/学历不符合 → 点"不合适"
2. 符合条件 → 点"附件简历"→ 提取信息 → 换微信 → 写入 SQLite

**数据输出**：`data/candidates.db`（SQLite）。

**前置条件**：Chrome 已打开 BOSS 直聘沟通页面。

---

### `cua_sync_jobs.py` — 职位管理页同步岗位信息

```bash
# 预览（不写入）
python scripts/cua_sync_jobs.py

# 提取 + 覆盖写入 config/jobs.json
python scripts/cua_sync_jobs.py --write

# 只处理前 N 个
python scripts/cua_sync_jobs.py --write --limit 3
```

**流程**：进入职位管理页 → 扫描"开放中"岗位（同名去重）→ 逐个点编辑提取：
- `title`：AXTextField（最短中文）
- `requirements`：JS 直读 iframe 内 textarea（绕过 AX 200 字截断）
- `salary` / `degree` / `location`：AXStaticText/AXTextField

**前置条件**：Chrome 已打开 BOSS 直聘职位管理页面。

---

### `boss_click_buheshi.py` — "不合适"点击模块（调试/独立使用）

```bash
# 独立调试
python scripts/boss_click_buheshi.py
```

此脚本被 `cua_collect.py` 和 `cua_chat_loop.py` 作为共享模块 import 使用。触发场景：**学校不在白名单** / **学历不达标**。`--dry-run` 模式下跳过实际操作。

**流程**：AX 检测"不合适"→ CGEvent 原生鼠标 hover（触发下拉面板）→ AX 轮询等"标为不合适"面板展开（最多 15s）→ 原生点击 → AX 验证（薪资不符/学历不符/确认）。

---

## 定时任务部署

```bash
# 每天早上 8 点自动打招呼
0 8 * * 1-5 cd /path/to/cua-boss-system && python scripts/cua_greeting_loop.py --limit 10

# 每 2 小时检查未读并智能回复
0 */2 * * 1-5 cd /path/to/cua-boss-system && python scripts/cua_chat_loop.py --limit 20

# 每周一同步职位信息
0 9 * * 1 cd /path/to/cua-boss-system && python scripts/cua_sync_jobs.py --write

# 每天下午收集候选人简历
0 14 * * 1-5 cd /path/to/cua-boss-system && python scripts/cua_collect.py --limit 15
```

**注意**：所有脚本依赖 Chrome 已登录 BOSS 直聘并在对应页面。建议先 `--dry-run` 验证后再部署。

---

## 配置

### `config/jobs.json` — 岗位配置

```json
{
  "version": 6,
  "jobs": [
    {
      "id": "dev",
      "title": "开发",
      "category": "tech",
      "requirements": "需要5-10年Java经验",
      "salary": "16K-30K",
      "degree": "本科",
      "location": "广州天河区..."
    }
  ]
}
```

- `cua_sync_jobs.py --write` 自动同步 title/requirements/salary/degree/location
- `id` + `category` 手动维护，对应 `templates.json` 中的模板分组
- 字段值自动作为模板 `{salary}` `{location}` 等占位符的替换源

### `config/templates.json` — 话术提示词模板

三层匹配结构：`jobs`（岗位专属）→ `categories`（tech/nontech 类别通用）→ `fallback`（全局兜底）。
每层按 `priority` 升序匹配，`priority: 99` + `match_keywords: []` 为最终兜底。
支持 `{salary}` `{location}` `{title}` 等占位符自动从 `jobs.json` 取值。

### `app/filter_criteria.py` — 筛选条件

- **学校白名单**：985（39 所）+ 211 非 985（73 所）+ 海外名校（QS 前 100）
- **学历等级**：博士 (4) > 硕士 (3) > 本科 (2) > 大专 (1)

---

## cua-driver 集成要点（Agent 须知）

| 要点 | 说明 |
|------|------|
| 页面导航后索引全变 | 用标题/文本匹配，不要用位置索引 |
| 职位描述在 iframe 内 | JS 读 `iframe.contentDocument.querySelector('textarea').value` |
| cua() 非 JSON 截断 200 字 | JS 必须返回 `JSON.stringify({text: ...})` |
| 聊天页联系人 | `<span class="geek-name">` + JS click |
| 列表页卡片结构 | 岗位名 AXLink → 状态 StaticText → 编辑 AXLink |
| 连续操作触发风控 | 每岗间隔 3-7 秒随机 |
| Vue 事件代理 | 原生 CGEvent 点击绕过 BOSS 的 Vue 事件系统 |
| "不合适"按钮 | `.operate-icon-item[8]`（第 9 个操作图标） |

---

## 常见操作示例

### 完整流程：从打招呼到收集

```bash
# 1. 先预览
python scripts/cua_greeting_loop.py --dry-run
python scripts/cua_chat_loop.py --dry-run

# 2. 实际执行
python scripts/cua_greeting_loop.py --limit 10 --min-degree 硕士
python scripts/cua_chat_loop.py --limit 20 --min-degree 硕士
python scripts/cua_collect.py --limit 10 --min-degree 硕士

# 3. 同步职位
python scripts/cua_sync_jobs.py --write
```

### 调试特定候选人

```bash
# 预览模式只看不操作
python scripts/cua_chat_loop.py --dry-run --limit 5

# 只测试"不合适"点击
python scripts/boss_click_buheshi.py
```

---

## 文件结构

```
cua-boss-system/
├── app/
│   ├── filter_criteria.py    # 名校白名单 + 学校匹配 + 学历判断
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
├── SKILL.md                  # 本文件 — Agent 操作手册
├── CLAUDE.md                 # Claude 上下文文件
└── README.md                 # 项目说明
```
