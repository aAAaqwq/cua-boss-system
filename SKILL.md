# cua-boss-system -- Agent 操作手册

> cua-driver 驱动的 BOSS 直聘自动化。本文档供 Claude/Agent 操作项目使用。

## 项目概述

通过 `cua-driver` CLI 操控 Chrome 的 macOS Accessibility API，实现 BOSS 直聘的批量打招呼、智能沟通、候选人收集和职位同步。

**零 pip 依赖**，纯 Python 标准库。系统依赖：`cua-driver` CLI、`swiftc`（macOS 自带）、Chrome。

> **平台限制**: 仅支持 **macOS 12+ (Monterey 及以上)**，不支持 Linux/Windows。cua-driver 依赖 macOS Accessibility API。
>
> **当前测试环境**: Python 3.14.5 / cua-driver 0.5.1 / Swift 6.3.2 / Chrome 148 / macOS 26.5

## 前置依赖安装

### 1. Python 3.10+

```bash
# 检查版本
python3 --version   # 需要 >= 3.10

# 未安装时（macOS）：
brew install python@3.14    # 当前测试通过版本，>= 3.10 即可
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
pgrep -x "Google Chrome" && echo "x Chrome 运行中" || echo "请先启动 Chrome"
```

### 4. cua-driver (>= 0.5.x)

cua-driver 是本项目核心依赖，macOS 原生 `.app` 应用（带 GUI），通过 Accessibility API 操控 Chrome。当前测试版本 `0.5.1`。

#### 下载

| 渠道 | 链接 |
|------|------|
| **GitHub Releases（推荐）** | https://github.com/trycua/cua/releases |
| **具体版本** | 找 `cua-driver-rs` 开头的 tag，下载 `.dmg`（如 `cua-driver-rs-v0.5.2`） |
| **项目主页** | https://github.com/trycua/cua |

> **架构选择**: 下载页有 `aarch64`（Apple Silicon M1/M2/M3/M4）和 `x86_64`（Intel）两个 `.dmg`，选错会无法启动。在终端运行 `uname -m` 确认架构。

#### 安装步骤

```bash
# 1. 下载 .dmg -> 打开 -> 拖到 /Applications
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

#### GUI 功能

`CuaDriver.app` 是 macOS 菜单栏应用：
- **光标叠加层** -- agent 操作时显示虚拟光标，不移动真实鼠标（可用 `--no-overlay` 关闭）
- **权限管理** -- Accessibility / Screen Recording 授权弹窗
- **daemon 常驻** -- 后台运行，CLI 通过 Unix socket 通信
- **autostart** -- 开机自启

#### 常用命令

```bash
cua-driver status          # 检查服务状态
cua-driver doctor          # 全面诊断
cua-driver permissions status  # 权限状态
cua-driver skills status   # Agent skills 安装状态
cua-driver check-update    # 检查新版本
```

---

## 运行前检查清单

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
pgrep -x "Google Chrome" && echo "x Chrome 运行中"
```

### 2. Chrome 状态

- [ ] Chrome 已打开并**登录** BOSS直聘（zhipin.com）
- [ ] 登录态未过期（打开 https://www.zhipin.com/web/chat/index 能看到联系人列表）
- [ ] **已开启 "允许来自 Apple 事件的 JavaScript"**（Chrome 菜单栏 -> 显示 -> 开发者 -> [x] 允许来自 Apple 事件的 JavaScript）-- cua-driver `page` 命令执行 JS 的前提
- [ ] 目标页面已在标签页中打开：
  - 打招呼 -> 推荐牛人页
  - 智能沟通 / 收集 -> 沟通页（聊天页）
  - 同步职位 -> 职位管理页

### 3. 配置文件

```bash
# 岗位配置存在
cat config/jobs.json | python3 -m json.tool > /dev/null && echo "x jobs.json"

# 话术模板存在（先找 reply.json -> 兜底 reply-templates.json）
cat config/reply.json 2>/dev/null || cat config/reply-templates.json | python3 -m json.tool > /dev/null && echo "x reply templates OK"

# 系统提示词存在
test -f config/system_prompt.md && echo "x system_prompt.md"
```

### 4. DeepSeek API（必须提前配置，未配置时降级为模板原文，智能回复质量大幅下降）

```bash
# 复制模板并填入 API Key
cp .env.example .env
# 编辑 .env，填入: DEEPSEEK_API_KEY=sk-your-key-here

# 验证
test -f .env && grep -q "DEEPSEEK_API_KEY=sk-" .env && echo "x DeepSeek 已配置" || echo "x 未配置，请先执行 cp .env.example .env 并填入密钥"
```

> **注意**: DeepSeek 密钥必须**运行前**在 `.env` 中配置好。未配置时脚本不会报错，但所有智能回复会降级为模板原文，回复质量显著下降。建议在首次运行前完成配置。

### 5. 数据库（首次运行自动创建）

```bash
# 检查 candidates.db 是否存在
test -f data/candidates.db && echo "x DB 已存在 ($(sqlite3 data/candidates.db 'SELECT COUNT(*) FROM candidates') 条记录)" || echo "首次运行将自动创建"
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
checks.append(('templates.json', Path('config/reply.json').exists() or Path('config/reply-templates.json').exists()))
checks.append(('system_prompt.md', Path('config/system_prompt.md').exists()))

# DeepSeek
import os
for line in (Path('.env').read_text().splitlines() if Path('.env').exists() else []):
    if 'DEEPSEEK_API_KEY=sk-' in line:
        checks.append(('DeepSeek API', True)); break
else:
    checks.append(('DeepSeek API', False))

for name, ok in checks:
    print(f'  {'x' if ok else 'x'} {name}')
passed = sum(1 for _,ok in checks if ok)
print(f'\n  {passed}/{len(checks)} 通过')
"
```

### 推荐运行顺序

```bash
# 1. 先 dry-run 预览，确认脚本行为正确
python scripts/cua_sync_jobs.py             # 预览（自动写入）
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

## 配置文件详解（Agent 须知）

### 配置架构总览 -- template+local 双文件模式

所有配置文件采用 `-template.json`（提交到 git）+ 同名 `.json`（本地自定义，`.gitignore`）模式。运行时优先读本地文件，不存在时用 template 兜底。

```
config/
├── filter-template.json  -->  filter.json (gitignore, 运行时读取)
├── jobs-template.json    -->  jobs.json (sync自动写入, 合并id/category)
├── reply-templates.json  -->  reply.json (gitignore, 运行时读取)
├── scoring.json             (git tracked, 手动编辑)
└── system_prompt.md         (git tracked, 手动编辑)
```

### 按任务判断改哪个文件

| 用户说... | 应该改这个文件 |
|-----------|----------------|
| 加/删学校白名单、调学历要求 | `config/filter.json`（不存在则改 `filter-template.json` 并复制） |
| 调整 DeepSeek 人设/回复策略 | `config/system_prompt.md` |
| 新增岗位评分维度 | `config/scoring.json`（加 job_overrides 或改 category_defaults） |
| 新增岗位话术模板 | 运行 `gen_reply_templates.py --job-id <id> --write` 或手动编辑 `config/reply.json` |
| 新增岗位模板参考（提交到 git） | `config/reply-templates.json` |
| 改岗位 title/requirements（同步来的） | 运行 `cua_sync_jobs.py`（不要手动改 jobs.json） |
| 给现有岗位设 category | `config/jobs-template.json`（添加 id -> category 映射） |
| 新增岗位在 BOSS 发布后需系统识别 | `config/jobs-template.json` 添加完整字段（id, category, title 等） |
| 调整 DeepSeek API 地址/模型 | `.env`（DEEPSEEK_BASE_URL / DEEPSEEK_MODEL） |
| 改自定义学校白名单跑某次脚本 | 命令行参数 `--schools "清华,北大"` |
| 改某次最低学历 | 命令行参数 `--min-degree 硕士` |

### `config/filter.json` -- 筛选条件配置（核心编辑文件）

本地自定义筛选条件。如果不存在（首次部署），运行时自动从 `filter-template.json` 兜底。

```json
{
  "school_whitelist": ["清华大学", "北京大学", ...],
  "min_degree": "本科",
  "degree_rank": {"博士": 4, "硕士": 3, "本科": 2, "大专": 1}
}
```

**运行时加载**: `app/filter_criteria.py` --> `filter.json`（优先）--> `filter-template.json`（兜底）
**共 251 所学校**（140 国内 + 111 海外），全部中文名。
**编辑流程**: `cp config/filter-template.json config/filter.json` -> 编辑 `filter.json` -> 运行时读取。

### `config/jobs.json` -- 岗位配置（自动生成，不要手动改）

sync 脚本自动写入。手动改的内容会被下次 sync 覆盖（id/category 例外，从 template 合并）。

```json
{
  "version": 3,
  "jobs": [
    {
      "id": "dev",
      "title": "开发",
      "category": "tech",
      "requirements": "...",
      "salary": "16K-30K",
      "degree": "本科",
      "location": "广州天河区..."
    }
  ]
}
```

- `cua_sync_jobs.py` 同步 title/requirements/salary/degree/location
- `id` + `category` 从 `jobs-template.json` 合并（template 为权威来源）
- 字段值自动作为模板 `{salary}` `{location}` 等占位符的替换源

### `config/jobs-template.json` -- 岗位元数据模板（手动维护 id/category）

```json
{
  "jobs": [
    {
      "id": "dev",
      "title": "开发",
      "category": "tech",
      "requirements": "...",
      ...
    }
  ]
}
```

sync 脚本解析此文件来获取 `id` -> `category` 映射。新岗位在 BOSS 发布后，如果 sync 提取的 title 能匹配到此文件的 title（模糊匹配），则自动分配 id/category。

**新岗位接入流程**:
1. 在 BOSS 直聘发布岗位
2. 运行 `cua_sync_jobs.py` 同步（自动写入 jobs.json，id 自动生成）
3. 如果 id/category 不对，在 `jobs-template.json` 添加完整字段
4. 再次运行 sync，id/category 会被正确合并

### `config/reply.json` / `reply-templates.json` -- 话术模板

`reply.json` 是本地运行时文件（gitignore），`reply-templates.json` 是 git 参考模板。不存在 `reply.json` 时自动用 template 兜底。

```json
{
  "version": 2,
  "description": "...",
  "jobs": {
    "dev": [
      {
        "id": "dev_salary",
        "name": "问薪资",
        "match_keywords": ["薪资", "工资", "待遇"],
        "reply": "薪资范围{salary}，具体根据能力面议。",
        "priority": 1
      }
    ]
  },
  "categories": { "tech": [...], "nontech": [...] },
  "fallback": [...]
}
```

- `reply.json` 自动生成: `python scripts/gen_reply_templates.py --job-id dev --write`
- 也可以手动编辑 `reply-templates.json` 作为参考，复制到 `reply.json` 使用
- 结构: `jobs.{job_id}`（岗位专属）-> `categories.{tech/nontech}`（类别通用）-> `fallback`（全局兜底）

### `config/system_prompt.md` -- DeepSeek 系统提示词

用 Markdown 维护招聘官人设和策略。编辑即时生效，无需改代码。包含:
- 核心原则（简短自然、推进对话、针对性）
- 对话推进策略（初次接触 -> 了解背景 -> 确认意向 -> 约面试）
- 阶段回复方向表
- 禁忌事项（不重复问、不说废话、不复制粘贴）
- 特殊情况处理（发简历附件/简短回复/表达顾虑）

---

## 脚本详解

### 意图 -> 脚本映射

| 用户说... | 应执行... |
|-----------|-----------|
| 打招呼、主动联系、推荐页、批量打招呼、牛人打招呼、勾搭候选人 | `cua_greeting_loop.py` |
| 回复、沟通、聊天、智能回复、处理未读、批量回复、看消息 | `cua_chat_loop.py` |
| 收集简历、提取微信、收集候选人、批量收集、捞简历、采集 | `cua_collect.py` |
| 同步职位、更新岗位、提取岗位信息、刷新职位列表、岗位配置 | `cua_sync_jobs.py` |
| 生成话术、AI生成模板、自动补充话术 | `gen_reply_templates.py` |
| 不合适、点不合适、buheshi、标为不合适 | `boss_click_buheshi.py` |
| 查数据库、导出、候选人列表、查询候选、数据导出 | `query_db.py` |
| 白名单、学校筛选、学历筛选、筛选条件 | `app/filter_criteria.py` / `config/filter.json` |
| 话术、回复模板、聊天模板、自动回复内容 | `config/reply.json` / `config/reply-templates.json` |
| 岗位配置、职位要求、job config | `config/jobs-template.json`（手动维护映射）|
| 评分、打分、候选人评分、score、评估候选人 | `app/scoring.py`（见下方评分系统节） |
| 干跑、预览、dry run、不实际操作 | 任意脚本加 `--dry-run` |
| 部署到服务器、crontab、定时任务、自动化排期 | 参考下方定时任务节 |

---

## CLI 命令速查

### `cua_greeting_loop.py` -- 推荐页批量打招呼

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

### `cua_chat_loop.py` -- 沟通页批量智能沟通

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

### `cua_collect.py` -- 沟通页批量收集候选人

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

### `cua_sync_jobs.py` -- 职位管理页同步岗位信息

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

### `gen_reply_templates.py` -- AI 生成话术模板

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

### `boss_click_buheshi.py` -- "不合适"点击模块（调试/独立使用）

```bash
# 独立调试
python scripts/boss_click_buheshi.py
```

此脚本被 `cua_collect.py` 和 `cua_chat_loop.py` 作为共享模块 import 使用。触发场景：**学校不在白名单** / **学历不达标**。

**流程**：AX 检测"不合适"-> CGEvent 原生鼠标 hover（触发下拉面板）-> AX 轮询等"标为不合适"面板展开（最多 15s）-> 原生点击 -> AX 验证。

---

## 统一筛选模块 `app/filter_criteria.py`

### 核心接口

```python
# 统一入口 -- 同时检查学校 + 学历
passed, reason = check_candidate("清华大学", "硕士")
# -> (True, None)

passed, reason = check_candidate("某某学院", "本科")
# -> (False, "学校不符 (某某学院 不在白名单)")
```

### 配置加载流程

```
filter.json（优先）--> filter-template.json（兜底）--> 硬编码（最后兜底）
```

### 配置内容

- **学校白名单**: 251 所学校（140 国内 985/211/双一流 + 111 海外名校）
- **学历等级**: 博士(4) > 硕士(3) > 本科(2) > 大专(1)
- **默认最低学历**: 本科

### 导出函数

| 函数 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `check_candidate(school, degree, school_whitelist, min_degree)` | school/degree 字符串 | `(bool, str | None)` | 统一筛选入口 |
| `check_degree(degree, min_degree)` | 学历名 | `bool` | 学历等级比较 |
| `match_school(candidate_school, whitelist)` | 学校名 + 白名单 | `bool` | 精确全中文匹配 |

所有脚本通过 `from app.filter_criteria import check_candidate, check_degree` 导入。
`chat_reply.py` 通过 `from app.filter_criteria import check_degree` 保持向后兼容。

### 可扩展 FilterCriteria

```python
@dataclass
class FilterCriteria:
    school_whitelist: Optional[List[str]] = None
    min_degree: str = "本科"
    min_years: int = 3
    # 预留字段
    age_range: Optional[Tuple[int, int]] = None
    tech_stack: Optional[List[str]] = None
    industry: Optional[List[str]] = None
    job_title_keywords: Optional[List[str]] = None
    exclude_keywords: Optional[List[str]] = None
```

---

## 话术模板系统 `config/reply.json` + `config/reply-templates.json`

### 三层匹配

```
候选人消息
  -> 1. 岗位专属模板 (jobs.{job_id} -- 按 priority 排序匹配)
  -> 2. 类别通用模板 (categories.{tech|nontech} -- 同 category 岗位共享)
  -> 3. 全局兜底模板 (fallback -- 16条通用场景)
```

### 匹配策略

- 按 `priority` 升序匹配，数字越小越优先
- `match_keywords: []` 为空表示纯兜底（仅在无匹配时返回）
- `{salary}` `{location}` `{title}` `{requirements}` `{degree}` 占位符自动从 jobs.json 替换

### 回复生成流程

```python
generate_reply(
    message=候选人消息,
    templates=通用模板,
    candidate_name=候选人称呼,
    history=对话历史,
    job_templates=岗位专属模板,     # reply.json -> jobs.{job_id}
    category_templates=类别模板,    # reply.json -> categories.{tech|nontech}
    fallback_templates=全局兜底,    # reply.json -> fallback
    job_context=岗位描述,
    job=岗位字典（变量替换源）,
    stage_context=阶段上下文约束,
)
```

### DeepSeek 调用细节

- **模型**: 默认 `deepseek-chat`（通过 `DEEPSEEK_MODEL` 环境变量可改）
- **Temperature**: 0.7
- **Max tokens**: 150
- **系统提示词**: `config/system_prompt.md`（编辑即时生效）
- **提示词拼接**: system_prompt + 岗位信息 + 模板方向 + 阶段约束
- **历史管理**: 最多 20 条，去重保序，role 映射（candidate->user, boss->assistant）
- **失败降级**: API 失败 -> 返回模板原文 -> 仍冗余 -> 阶段兜底文本

### 模板生成

`gen_reply_templates.py` 调用 DeepSeek 自动生成：
- prompt 包含：岗位全部字段 + 公司背景 + 覆盖 10 个场景要求
- 输出 JSON 数组，直接写入 `config/reply.json jobs.{job_id}`
- 可以只为单个岗位生成，不影响其他岗位已有模板

---

## 评分系统 (`app/scoring.py` + `config/scoring.json`)

多维度 AI 评分，满分 100。每个岗位可独立配置评分维度和权重，全部维度统一走 DeepSeek 一次 API 调用。

### 快速使用

```bash
# 命令行交互评分（从 candidates.db 读取全部候选人）
python3 app/scoring.py
```

```python
# 代码调用
from app.scoring import evaluate_candidate, format_score_report, load_scoring_config

config = load_scoring_config()
score = evaluate_candidate(
    candidate_data={
        "name": "张三", "school": "华中科技大学", "degree": "硕士",
        "job_position": "高级Java工程师",
        "chat_history": [...], "resume_content": "5年Java经验...",
    },
    job_id="dev", category="tech",
    job_context="开发 -- 需要5-10年Java经验",
)
print(format_score_report(score, verbose=True))
```

### 评分流程

```
resolve_dimensions(job_id, category)  -> 获取维度（岗位覆盖 > 类别默认）
    |
_build_scoring_prompt()  -> 拼接 prompt（候选人信息 + 聊天记录 + 维度清单 + 岗位要求）
    |
_call_deepseek_scoring()  -> 一次 API 调用，返回全部分数+依据+总结
    |
加权汇总: raw_score / 10 x weight  -> 总分（满分100）
    |
format_score_report()  -> 评级 S/A/B/C/D + 每维度分条 + 进度条 + 依据
```

### 维度配置

两层配置，岗位覆盖优先（`config/scoring.json`）：

| 来源 | 适用 | 维度示例 |
|---|---|---|
| tech 默认 | `dev` | 技术深度(35) 项目质量(30) 工具链匹配(15) 教育背景(8) 工作经验(7) 沟通表达(5) |
| nontech 默认 | `annotation` | 行业经验(25) 业绩成果(25) 资源网络(15) 管理能力(15) 教育背景(10) 沟通表达(10) |
| 岗位覆盖 | `annotation-2` | 战略思维(25) 落地执行(25) 学习能力(15) 管理能力(15) 教育背景(10) 沟通表达(10) |

新增岗位只需在 `scoring.json` 加维度配置（权重和=100），无需改代码。

### 输入数据

| 字段 | 来源 | 说明 |
|---|---|---|
| `name` | candidates.db | 候选人姓名 |
| `school` / `degree` | candidates.db | 学校 / 学历 |
| `job_position` | candidates.db | 当前职位 |
| `chat_history` | candidates.db | 聊天记录 JSON（最近 10 条） |
| `resume_content` | candidates.db | 简历全文 |
| `notes` | candidates.db | 备注 |
| `job_context` | jobs.json | 岗位 title + requirements |

### AI 评分细节

- **模型**: DeepSeek（`deepseek-chat`）
- **Temperature**: 0.3（保持一致性）
- **信息不足**: 保守偏低，不凭空猜测
- **API Key 未配**: 全部维度 0 分，errors 提示
- **输出格式**: `{"dimensions": {"key": {"score": 0-10, "evidence": "..."}}, "summary": "综合评价"}`

### 评级标准

| 总分 | 评级 |
|---|---|
| >=85 | S -- 强烈推荐 |
| >=70 | A -- 推荐 |
| >=55 | B -- 可考虑 |
| >=40 | C -- 待定 |
| <40 | D -- 不推荐 |

---

## 定时任务部署

```bash
# 每天早上 8 点自动打招呼
0 8 * * 1-5 cd /path/to/cua-boss-system && python scripts/cua_greeting_loop.py --limit 10

# 每 2 小时检查未读并智能回复
0 */2 * * 1-5 cd /path/to/cua-boss-system && python scripts/cua_chat_loop.py --limit 20

# 每周一同步职位信息
0 9 * * 1 cd /path/to/cua-boss-system && python scripts/cua_sync_jobs.py

# 每天下午收集候选人简历
0 14 * * 1-5 cd /path/to/cua-boss-system && python scripts/cua_collect.py --limit 15
```

**注意**：所有脚本依赖 Chrome 已登录 BOSS 直聘并在对应页面。建议先 `--dry-run` 验证后再部署。

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

## 文件结构

```
cua-boss-system/
├── app/
│   ├── db.py                 # 共享数据库模块(init_db / backup_db / clear_db)
│   ├── filter_criteria.py    # 统一筛选：check_candidate() + 名校白名单 + 学历等级
│   ├── chat_reply.py         # 模板匹配 + DeepSeek(阶段感知+上下文合并) + 岗位检测
│   └── scoring.py            # 候选人评分系统(AI多维度/按岗位自定义权重)
├── config/
│   ├── jobs.json             # 岗位配置（cua_sync_jobs.py 自动同步，gitignore?）
│   ├── jobs-template.json    # 岗位元数据模板（手动维护 id/category，提交到 git）
│   ├── reply.json            # 本地话术配置（gitignore，运行时读取）
│   ├── reply-templates.json  # 话术模板参考（提交到 git）
│   ├── filter.json           # 本地筛选配置（gitignore，运行时读取）
│   ├── filter-template.json  # 筛选条件模板（提交到 git，251所学校）
│   ├── scoring.json          # 评分维度配置（按类别默认/岗位覆盖/权重100）
│   └── system_prompt.md      # DeepSeek 系统提示词（HR招聘专家人设）
├── scripts/
│   ├── boss_click_buheshi.py   # "不合适"点击共享模块（CGEvent原生鼠标）
│   ├── cua_chat_loop.py        # 沟通页批量智能沟通（阶段感知+uid提取+上下文合并）
│   ├── cua_collect.py          # 沟通页批量收集（简历+微信->SQLite）
│   ├── cua_greeting_loop.py    # 推荐页批量主动打招呼
│   ├── cua_sync_jobs.py        # 职位管理页同步岗位信息
│   ├── gen_reply_templates.py  # AI生成话术模板（调用DeepSeek）
│   └── query_db.py             # 数据库查询/统计/CSV导出CLI工具
├── data/
│   ├── candidates.db         # 候选人数据（collect+chat_loop 共享）
│   └── backups/              # DB 备份目录（backup_db() 自动创建）
├── .env                      # DeepSeek API 配置（gitignore）
├── .env.example              # DeepSeek API 配置模板
├── SKILL.md                  # 本文件 -- Agent 操作手册
├── CLAUDE.md                 # Claude 上下文文件
└── README.md                 # 项目说明
```

## 新项目接入完整流程

### 首次部署

```bash
# 0. 检查前置条件（Python 3.10+, cua-driver, Chrome, swiftc）
python3 --version && swiftc --version && cua-driver status && pgrep -x "Google Chrome"

# 1. 配置 DeepSeek
cp .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY

# 2. 配置筛选条件（可选）
cp config/filter-template.json config/filter.json
# 编辑 config/filter.json 自定义学校白名单

# 3. 同步职位（Chrome 需在职位管理页）
python scripts/cua_sync_jobs.py --dry-run  # 预览
python scripts/cua_sync_jobs.py            # 自动写入

# 4. 检查新岗位的 id/category 是否正确
#    如不符，更新 jobs-template.json 后重新 sync

# 5. 生成话术模板
python scripts/gen_reply_templates.py --all --write

# 6. 收集 -> 沟通
python scripts/cua_collect.py --dry-run
python scripts/cua_collect.py --limit 10
python scripts/cua_chat_loop.py --dry-run
python scripts/cua_chat_loop.py --limit 20
```

### 新增岗位

```bash
# 1. 在 BOSS 直聘发布新岗位
# 2. 同步到系统
python scripts/cua_sync_jobs.py
# 3. 检查 jobs-template.json 是否覆盖了新岗位的 id/category
#    如不覆盖 -> 手动添加
# 4. 生成话术模板
python scripts/gen_reply_templates.py --job-id <new-id> --write
# 5. 检查 scoring.json 是否需新增评分维度
```

### 日常维护

```bash
# 定期同步职位（BOSS 上改动了要求后）
python scripts/cua_sync_jobs.py

# 调整筛选条件
# 编辑 config/filter.json

# 优化回复策略
# 编辑 config/system_prompt.md

# 检查候选人数据
python scripts/query_db.py
```
