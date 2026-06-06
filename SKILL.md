# cua-boss-system — Agent 操作手册

> cua-driver 驱动的 BOSS 直聘自动化。本文档供 Claude/Agent 操作项目使用。

## 项目概述

通过 `cua-driver` CLI 操控 Chrome 的 macOS Accessibility API，实现 BOSS 直聘的批量打招呼、智能沟通、候选人收集和职位同步。

**零 pip 依赖**，纯 Python 标准库。系统依赖：`cua-driver` CLI、`swiftc`（macOS 自带）、Chrome。

> **平台限制**: 仅支持 **macOS 12+ (Monterey 及以上)**，不支持 Linux/Windows。cua-driver 依赖 macOS Accessibility API。

## 前置依赖安装

### 1. Python 3.10+

```bash
# 检查版本
python3 --version   # 需要 >= 3.10

# 未安装时（macOS）：
brew install python@3.14
# 或从 https://www.python.org/downloads/ 下载安装包
```

### 2. Xcode Command Line Tools（提供 swiftc 编译器）

```bash
# 安装（弹窗确认，约 1-2 GB）
xcode-select --install

# 验证
swiftc --version
# 输出类似: Swift version 6.x ...

# 注意: 不需要完整 Xcode.app，Command Line Tools 即可
# swiftc 用于首次运行时自动编译 CGEvent 鼠标工具到 /tmp/cua_hid
```

### 3. Chrome

```bash
# 下载安装
# https://www.google.com/chrome/

# 验证
pgrep -x "Google Chrome" && echo "✓ Chrome 运行中" || echo "请先启动 Chrome"
```

### 4. cua-driver

cua-driver 是本项目核心依赖，macOS 原生 `.app` 应用（带 GUI），通过 Accessibility API 操控 Chrome。

### 下载

| 渠道 | 链接 |
|------|------|
| **GitHub Releases（推荐）** | https://github.com/trycua/cua/releases |
| **具体版本** | 找 `cua-driver-rs` 开头的 tag，下载 `.dmg`（如 `cua-driver-rs-v0.5.2`） |
| **项目主页** | https://github.com/trycua/cua |

> **架构选择**: 下载页有 `aarch64`（Apple Silicon M1/M2/M3/M4）和 `x86_64`（Intel）两个 `.dmg`，选错会无法启动。在终端运行 `uname -m` 确认架构。

### 安装步骤

```bash
# 1. 下载 .dmg → 打开 → 拖到 /Applications
#    安装后自动在 ~/.local/bin/ 创建 cua-driver 软链接

# 2. 首次授权 macOS 权限（Accessibility + Screen Recording）
cua-driver permissions grant

# 3. 安装 Agent Skills（Claude Code / OpenClaw 等）
cua-driver skills install

# 4. 启动后台服务
cua-driver serve

# 5. 设置开机自启（可选）
cua-driver autostart enable

# 6. 后续更新
cua-driver update --apply
```

### GUI 功能

`CuaDriver.app` 是 macOS 菜单栏应用：
- **光标叠加层** — agent 操作时显示虚拟光标，不移动真实鼠标（可用 `--no-overlay` 关闭）
- **权限管理** — Accessibility / Screen Recording 授权弹窗
- **daemon 常驻** — 后台运行，CLI 通过 Unix socket 通信
- **autostart** — 开机自启

### 常用命令

```bash
cua-driver status          # 检查服务状态
cua-driver doctor          # 全面诊断
cua-driver permissions status  # 权限状态
cua-driver skills status   # Agent skills 安装状态
cua-driver check-update    # 检查新版本
```

---

运行任何脚本前，逐项确认以下条件。任何一项不满足都需要先修复。

### 1. 系统依赖

```bash
# Python 3.10+
python3 --version

# cua-driver 已安装且在 PATH
cua-driver status

# swiftc 可用（macOS Xcode 自带）
swiftc --version

# Chrome 已启动
pgrep -x "Google Chrome" && echo "✓ Chrome 运行中"
```

### 2. Chrome 状态

- [ ] Chrome 已打开并**登录** BOSS直聘（zhipin.com）
- [ ] 登录态未过期（打开 https://www.zhipin.com/web/chat/index 能看到联系人列表）
- [ ] **已开启 "允许来自 Apple 事件的 JavaScript"**（Chrome 菜单栏 → 显示 → 开发者 → ☑️ 允许来自 Apple 事件的 JavaScript）— cua-driver `page` 命令执行 JS 的前提
- [ ] 目标页面已在标签页中打开：
  - 打招呼 → 推荐牛人页
  - 智能沟通 / 收集 → 沟通页（聊天页）
  - 同步职位 → 职位管理页

### 3. 配置文件

```bash
# 岗位配置存在
cat config/jobs.json | python3 -m json.tool > /dev/null && echo "✓ jobs.json"

# 话术模板存在
cat config/templates.json | python3 -m json.tool > /dev/null && echo "✓ templates.json"

# 系统提示词存在
test -f config/system_prompt.md && echo "✓ system_prompt.md"
```

### 4. DeepSeek API（必须提前配置，未配置时降级为模板原文，智能回复质量大幅下降）

```bash
# 复制模板并填入 API Key
cp .env.example .env
# 编辑 .env，填入: DEEPSEEK_API_KEY=sk-your-key-here

# 验证
test -f .env && grep -q "DEEPSEEK_API_KEY=sk-" .env && echo "✓ DeepSeek 已配置" || echo "✗ 未配置，请先执行 cp .env.example .env 并填入密钥"
```

> **注意**: DeepSeek 密钥必须**运行前**在 `.env` 中配置好。未配置时脚本不会报错，但所有智能回复会降级为模板原文，回复质量显著下降。建议在首次运行前完成配置。

### 5. 数据库（首次运行自动创建）

```bash
# 检查 candidates.db 是否存在
test -f data/candidates.db && echo "✓ DB 已存在 ($(sqlite3 data/candidates.db 'SELECT COUNT(*) FROM candidates') 条记录)" || echo "首次运行将自动创建"
```

### 6. 快速一键检查

```bash
python3 -c "
import subprocess, json
checks = []

# Python
v = subprocess.run(['python3','--version'], capture_output=True, text=True).stdout.strip()
checks.append(('Python 3.10+', '3.1' in v))

# cua-driver
r = subprocess.run(['cua-driver','status'], capture_output=True, text=True)
checks.append(('cua-driver', r.returncode == 0))

# Chrome
r = subprocess.run(['pgrep','-x','Google','Chrome'], capture_output=True)
checks.append(('Chrome 运行', r.returncode == 0))

# 配置文件
from pathlib import Path
checks.append(('jobs.json', Path('config/jobs.json').exists()))
checks.append(('templates.json', Path('config/templates.json').exists()))
checks.append(('system_prompt.md', Path('config/system_prompt.md').exists()))

# DeepSeek
import os
for line in (Path('.env').read_text().splitlines() if Path('.env').exists() else []):
    if 'DEEPSEEK_API_KEY=sk-' in line:
        checks.append(('DeepSeek API', True)); break
else:
    checks.append(('DeepSeek API', False))

for name, ok in checks:
    print(f'  {\"✓\" if ok else \"✗\"} {name}')
passed = sum(1 for _,ok in checks if ok)
print(f'\n  {passed}/{len(checks)} 通过')
"
```

### 推荐运行顺序

```bash
# 1. 先 dry-run 预览，确认脚本行为正确
python scripts/cua_sync_jobs.py             # 预览岗位
python scripts/cua_sync_jobs.py --write     # 同步到 config/jobs.json
python scripts/cua_collect.py --dry-run     # 预览收集
python scripts/cua_chat_loop.py --dry-run   # 预览沟通

# 2. 实际执行（先 collect 再 chat_loop，chat_loop 依赖 collect 写入的 DB 上下文）
python scripts/cua_collect.py --limit 10
python scripts/cua_chat_loop.py --limit 20
```

### 数据库管理

```python
from app.db import backup_db, clear_db, init_db

backup_db("manual")    # 备份到 data/backups/candidates_YYYYMMDD_HHMMSS_manual.db
clear_db()             # 自动备份 + 清空 candidates 表（保留表结构）
init_db()              # 重建/确认表结构
```

---

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
│   ├── db.py                 # 共享数据库模块(init_db / backup_db / clear_db)
│   ├── filter_criteria.py    # 名校白名单 + 学校匹配 + 学历判断
│   └── chat_reply.py         # 模板匹配 + DeepSeek(阶段感知) + 岗位检测
├── config/
│   ├── jobs.json             # 岗位配置（cua_sync_jobs.py --write 同步）
│   ├── templates.json        # 话术模板（专属→类别→兜底 三层）
│   └── system_prompt.md      # DeepSeek 系统提示词（HR招聘专家人设）
├── scripts/
│   ├── boss_click_buheshi.py   # "不合适"点击共享模块（CGEvent原生鼠标）
│   ├── cua_chat_loop.py        # 沟通页批量智能沟通（阶段感知+uid提取）
│   ├── cua_collect.py          # 沟通页批量收集（简历+微信→SQLite）
│   ├── cua_greeting_loop.py    # 推荐页批量主动打招呼
│   └── cua_sync_jobs.py        # 职位管理页同步岗位信息
├── data/
│   ├── candidates.db         # 候选人数据（collect+chat_loop 共享）
│   └── backups/              # DB 备份目录（backup_db() 自动创建）
├── .env.example              # DeepSeek API 配置模板
├── SKILL.md                  # 本文件 — Agent 操作手册
├── CLAUDE.md                 # Claude 上下文文件
└── README.md                 # 项目说明
```
