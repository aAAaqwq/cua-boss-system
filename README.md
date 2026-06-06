# cua-boss-system

通过 cua-driver 驱动 Chrome 实现 BOSS直聘自动化。

## 依赖

| 依赖 | 说明 |
|------|------|
| Python 3.10+ | 零 pip 依赖，纯标准库 |
| [cua-driver](https://github.com/cua-driver/cua-driver-rs) | macOS Accessibility API 操控 Chrome |
| swiftc (Xcode) | 编译 CGEvent 原生鼠标工具（首次自动编译到 /tmp/cua_hid） |
| Chrome | 需登录 BOSS直聘 |

## 快速开始

```bash
# 沟通页批量智能沟通（推荐入口）
python scripts/cua_chat_loop.py --dry-run

# 推荐页批量主动打招呼
python scripts/cua_greeting_loop.py --dry-run

# 职位管理页职位信息同步
python scripts/cua_sync_jobs.py --write
```

## 脚本

### `boss_click_buheshi.py` — "不合适"按钮点击（共享模块）

`cua_collect.py` 和 `cua_chat_loop.py` 共用此模块触发"不合适"操作。

流程：AX 检测"不合适" → macOS CGEvent 原生鼠标 hover（触发下拉面板）→ AX 轮询等"标为不合适"面板展开 → 原生点击 → AX 验证。

两个脚本在以下场景调用：**学校不在白名单** / **学历不达标**。`--dry-run` 模式下跳过。

```bash
# 独立调试
python scripts/boss_click_buheshi.py
```

### `cua_chat_loop.py` — 沟通页批量智能沟通

打开聊天页，逐个查看未读联系人，自动判断并执行：

- 学校/学历筛选 → 不符合点"不合适"
- 已回复判断 → 跳过，避免重复回复
- 岗位感知话术 → 匹配专属回复模板

```bash
python scripts/cua_chat_loop.py                   # 最多20人
python scripts/cua_chat_loop.py --dry-run          # 预览
python scripts/cua_chat_loop.py --limit 10         # 限制人数
python scripts/cua_chat_loop.py --min-degree 硕士   # 最低学历
python scripts/cua_chat_loop.py --schools "清华,北大" # 自定义学校
```

### `cua_greeting_loop.py` — 推荐页批量主动打招呼

打开推荐牛人页 → 扫描候选人 → 学校/学历筛选 → 逐个点击"打招呼"：

```bash
python scripts/cua_greeting_loop.py --dry-run
python scripts/cua_greeting_loop.py --limit 5
python scripts/cua_greeting_loop.py --min-degree 硕士
```

### `cua_sync_jobs.py` — 职位管理页职位信息同步

进入职位管理页 → 扫描开放中岗位 → 逐个点编辑提取详情 → 覆盖写入 `config/jobs.json`：

```bash
python scripts/cua_sync_jobs.py             # 预览
python scripts/cua_sync_jobs.py --write     # 提取+写入
```

## 配置

### `config/jobs.json` — 岗位配置（cua_sync_jobs.py 自动同步）

```json
{
  "jobs": [
    {
      "id": "dev",
      "title": "开发",
      "requirements": "需要5-10年的Java开发经验，有架构经验",
      "salary": "16K-30K",
      "degree": "本科",
      "templates": []
    }
  ],
  "fallback_templates": [
    { "match_keywords": [], "reply": "收到，我稍后看一下回复你～", "priority": 99 }
  ]
}
```

### `config/chat_templates.json` — 话术模板

回复策略：模板匹配 → DeepSeek API（需 `DEEPSEEK_API_KEY`）→ fallback。

## 项目结构

```
cua-boss-system/
├── app/
│   ├── filter_criteria.py    # 名校白名单 + 学校匹配
│   └── chat_reply.py         # 模板匹配 + DeepSeek API + 岗位检测
├── config/
│   ├── jobs.json             # 岗位配置
│   └── chat_templates.json   # 话术模板
├── scripts/
│   ├── boss_click_buheshi.py   # "不合适"点击共享模块
│   ├── cua_chat_loop.py    # 沟通页批量智能沟通
│   ├── cua_collect.py      # 沟通页批量收集（简历+微信→SQLite）
│   ├── cua_greeting_loop.py  # 推荐页批量主动打招呼
│   └── cua_sync_jobs.py          # 职位管理页职位信息同步
├── SKILL.md
├── CLAUDE.md
└── README.md
```
