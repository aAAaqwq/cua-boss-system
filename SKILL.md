# cua-boss-system -- Agent 操作手册

> cua-driver 驱动的 BOSS 直聘自动化。本文档供 Claude/Agent 操作项目使用。

## 项目概述

通过 `cua-driver` CLI 操控 Chrome 的 macOS Accessibility API，实现 BOSS 直聘的批量打招呼、智能沟通、候选人收集和职位同步。

**零 pip 依赖**，纯 Python 标准库。系统依赖：`cua-driver` CLI、`swiftc`（macOS 自带）、Chrome。

> **平台限制**: 仅支持 **macOS 12+ (Monterey 及以上)**，不支持 Linux/Windows。cua-driver 依赖 macOS Accessibility API。
>
> **当前测试环境**: Python 3.14.5 / cua-driver 0.5.1 / Swift 6.3.2 / Chrome 148 / macOS 26.5

---

## 文档索引

本手册按场景拆分。先读本页（流程 + 路由），按需再翻引用文件：

| 文件 | 内容 | 何时读 |
|------|------|--------|
| **SKILL.md**（本页） | 最佳测试实践流程、意图→脚本路由、文件结构 | 每次操作前 |
| [references/setup.md](references/setup.md) | 前置依赖安装、运行前检查清单、推荐运行顺序、新项目接入 | 首次部署 / 环境排障 |
| [references/cli.md](references/cli.md) | 每个脚本的命令/参数/流程、cua-driver 集成要点、常见操作、定时任务 | 查具体命令时 |
| [references/config.md](references/config.md) | 配置双文件模式、按任务选文件、筛选/话术/评分系统详解 | 改配置 / 调评分时 |

---

## 最佳测试实践（端到端验收流程）

> 这是从零到约面试的推荐验收顺序。**步骤 1-3**（环境安装 + 运行前检查）见 [references/setup.md](references/setup.md)；**步骤 4-10** 如下。所有参数均可通过命令行调整，命令细节见 [references/cli.md](references/cli.md)。

| # | 动作 | 命令 / 操作 |
|---|------|------------|
| 0 | **登录授权账号**（🔒 必须，否则所有自动化脚本启动即拒跑）| `python scripts/cloud_sync.py login --email <邮箱> --password <密码>`；账号由管理员后台开通（公开注册已关闭）。已登录可跳过；拥有者配 service_role 可免。见 [数据安全说明](docs/数据安全说明.md)/[权限架构](docs/权限架构.md) |
| 1-3 | **环境就绪**：安装依赖 + 跑「运行前检查清单」 | 见 [references/setup.md](references/setup.md) |
| 4 | **同步岗位信息** | `python scripts/cua_sync_jobs.py --write` |
| 5 | **配置专属话术 + 评分细则**（先查看现有配置，**如有必要**再引导用户调整） | 查看 `config/reply.json`、`config/scoring.json`；按需 `python scripts/gen_reply_templates.py --all --write` 生成话术，编辑 `scoring.json` 调权重 |
| 6 | **配置筛选模板**（先查看现有筛选配置，**如有必要**再引导调整） | 查看 `config/filter.json`；按需 `cp config/filter-template.json config/filter.json` 后编辑学校白名单 / 最低学历 |
| 7 | **运行 boss-pipeline**：打招呼 20 → 收集前 20 → 沟通前 20（默认值，可省略参数） | `python scripts/boss_pipeline.py`（等价 `--greet 20 --collect 20 --chat 20`）。**语义**：greet=成功打招呼人数；collect/chat=联系人列表顶部前 N 个逐个处理(含被筛掉/跳过的) |
| 8 | **看排行榜**：询问筛选评分口径，展示最近 2 天排行榜前 10，**排除已面试过的** | `python scripts/query_db.py --rank --days 2 --top 10` |
| 9 | **预约面试**：与用户确认线上/线下 + 具体时间，再预约 | `python scripts/cua_interview.py --uid <UID> --type 线上 --date 2026-06-20 --time 14:30` |
| 10 | **设为定时任务**（可选） | **不用专门脚本**——由当前 agent 平台用它自己的定时机制，到点调用功能脚本 `boss_pipeline.py`。把用户口语映射成命令即可，例：「每天 9/13/17 点、打招呼到上限、收简历和沟通各 50」→ 平台在三个时刻各跑一次 `python scripts/boss_pipeline.py --greet 999 --collect 50 --chat 50`（`--greet` 设大数即「打到每日上限自动停」）。**前提**：到点电脑已登录且保持会话(防睡眠)、Chrome 已登录 BOSS、cua-driver 权限就绪、已 login（GUI 自动化要求）|

> **说明**：第 8 步的排行榜会对窗口内尚未评分的候选人**懒调用 DeepSeek 评分并缓存到 DB**（`score`/`scored_at`），重复运行不会重复扣费；`--rescore` 可强制重算。第 9 步预约成功后会把面试写回 DB（`status=interviewed`），从而被第 8 步自动排除、并被面试提醒读取。

> **关于 pipeline**：本项目已不再使用独立的 `boss-full-pipeline` skill，全流程整合进 `scripts/boss_pipeline.py` 单脚本（参数化）。

---

## 意图 -> 脚本映射

| 用户说... | 应执行... |
|-----------|-----------|
| 登录、认证、绑定账号、没账号、提示需要登录、脚本拒跑/退出 | `cloud_sync.py login`（许可门禁：未登录则所有自动化脚本拒跑） |
| 完整流程、全自动、一键、全套、pipeline、从打招呼到沟通 | `boss_pipeline.py`（打招呼→收集→沟通，参数化） |
| 排行榜、评分排名、最近几天最佳候选人、谁最合适、top10 | `query_db.py --rank` |
| 约面试、预约面试、发面试邀请、安排线上/线下面试 | `cua_interview.py` |
| 面试提醒、今天有哪些面试、明天面试、提醒我面试 | `interview_reminder.py` |
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
| 评分、打分、候选人评分、score、评估候选人 | `app/scoring.py`（见 [references/config.md](references/config.md) 评分系统节） |
| 干跑、预览、dry run、不实际操作 | 任意脚本加 `--dry-run` |
| 部署到服务器、crontab、定时任务、自动化排期 | 见 [references/cli.md](references/cli.md) 定时任务节 |

> 命令与参数细节见 [references/cli.md](references/cli.md)；改配置/调评分见 [references/config.md](references/config.md)。

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
│   ├── boss_pipeline.py        # 全流程编排（打招呼→收集→沟通）
│   ├── cua_chat_loop.py        # 沟通页批量智能沟通（阶段感知+uid提取+上下文合并）
│   ├── cua_collect.py          # 沟通页批量收集（简历+微信->SQLite）
│   ├── cua_greeting_loop.py    # 推荐页批量主动打招呼
│   ├── cua_sync_jobs.py        # 职位管理页同步岗位信息
│   ├── cua_interview.py        # 预约面试（写回 DB）
│   ├── interview_reminder.py   # 面试提醒（纯读 DB，不操作 Chrome）
│   ├── gen_reply_templates.py  # AI生成话术模板（调用DeepSeek）
│   └── query_db.py             # 数据库查询/统计/CSV导出 + --rank 评分排行榜
├── data/
│   ├── candidates.db         # 候选人数据（collect+chat_loop 共享）
│   └── backups/              # DB 备份目录（backup_db() 自动创建）
├── references/
│   ├── setup.md              # 安装 + 运行前检查 + 新项目接入
│   ├── cli.md                # 命令速查 + 脚本详解 + 定时任务
│   └── config.md            # 配置详解 + 筛选/话术/评分系统
├── .env                      # DeepSeek API 配置（gitignore）
├── .env.example              # DeepSeek API 配置模板
├── SKILL.md                  # 本文件 -- Agent 操作手册（索引）
├── CLAUDE.md                 # Claude 上下文文件
└── README.md                 # 项目说明
```
