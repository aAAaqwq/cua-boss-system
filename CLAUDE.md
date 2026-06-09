# cua-boss-system

cua-driver 驱动的 BOSS直聘自动化系统。

## 项目结构

```
cua-boss-system/
├── app/
│   ├── db.py                 # 共享数据库模块(init_db / DB_PATH / schema迁移)
│   ├── filter_criteria.py    # 名校白名单(985/211/海外) + 学校匹配 + 学历判断
│   └── chat_reply.py         # 模板匹配 + DeepSeek(阶段感知+上下文合并) + 岗位检测
├── config/
│   ├── jobs.json             # 岗位配置(cua_sync_jobs.py 自动同步)
│   ├── templates.json        # 话术模板(专属→类别→兜底 三层)
│   └── system_prompt.md      # DeepSeek 系统提示词(HR招聘专家人设，.md维护)
├── scripts/
│   ├── boss_click_buheshi.py   # "不合适"点击共享模块(CGEvent原生鼠标)
│   ├── cua_chat_loop.py        # 沟通页批量智能沟通(阶段感知+uid提取+上下文合并)
│   ├── cua_collect.py          # 沟通页批量收集(简历+微信→SQLite)
│   ├── cua_greeting_loop.py    # 推荐页批量主动打招呼
│   ├── cua_sync_jobs.py        # 职位管理页同步岗位信息
│   └── query_db.py             # 数据库查询/统计/CSV导出
├── data/
│   └── candidates.db         # 候选人数据(collect+chat_loop 共享)
├── .env.example              # DeepSeek API 配置模板
├── SKILL.md
├── CLAUDE.md
└── README.md
```

## 依赖

- **平台**: macOS 12+ (Monterey 及以上)，不支持 Linux/Windows
- Python 3.10+（纯标准库）
- `cua-driver` CLI (≥ 0.5.x)
- `swiftc`（首次运行自动编译 CGEvent 鼠标工具 `/tmp/cua_hid`）
- Chrome（需登录 BOSS直聘）
- **Chrome 设置**: 菜单栏 → 显示 → 开发者 → ☑️ 允许来自 Apple 事件的 JavaScript
- DeepSeek API（必须提前配置，未配置时降级为模板原文，回复质量显著下降）

## 脚本

### cua_greeting_loop.py — 推荐页批量主动打招呼

```bash
python scripts/cua_greeting_loop.py                  # 最多5人
python scripts/cua_greeting_loop.py --dry-run         # 预览
python scripts/cua_greeting_loop.py --limit 3         # 最多3人
python scripts/cua_greeting_loop.py --min-degree 硕士  # 最低学历
```

流程: 进入推荐页 → AX树扫描候选人(学校取教育经历最后一行=本科) → 学校白名单+学历筛选 → 逐个点击打招呼 → 检测上限弹窗

### cua_chat_loop.py — 沟通页批量智能沟通

```bash
python scripts/cua_chat_loop.py                   # 最多20人
python scripts/cua_chat_loop.py --dry-run          # 预览
python scripts/cua_chat_loop.py --limit 10         # 限制人数
python scripts/cua_chat_loop.py --min-degree 硕士   # 最低学历
python scripts/cua_chat_loop.py --schools "清华,北大" # 自定义学校
```

流程:
```
1. start_session()              启动 cua-driver session
2. find_boss_window()           定位 Chrome BOSS窗口
3. navigate_to_chat()           导航到沟通页
4. scan_all_contacts()          AX树扫描联系人列表 → [{name, job, msg, time}, ...]
5. db = sqlite3.connect(DB_PATH) 直连已有 candidates.db
6. 逐个 review_one_candidate(db=db):
   ├─ a. click_contact()        点击联系人 + 提取 data-id → (clicked, uid)
   ├─ b. _clear_input()         Cmd+A + Delete 清空输入框(兼容React)
   ├─ c. read_conversation()    读右侧面板(AX树):
   │     ├─ 学校 / 学历 / info_line
   │     ├─ chat_history: [{role, content}, ...]（含 system 消息）
   │     └─ last_sender → "boss" | "candidate"
   ├─ d. _load_candidate_context(db, uid, name)
   │     查 DB: has_resume, has_wechat, wechat, status, db_chat_history
   ├─ e. _compute_stage(ctx, chat_history)
   │     ├─ ready_for_interview  (简历+微信都有 → 推动约面试)
   │     ├─ has_resume_no_wechat (有简历 → 不问简历，聊岗位/微信)
   │     ├─ has_wechat_no_resume (有微信 → 不问微信，聊岗位细节)
   │     ├─ awaiting_response    (已请求等回复 → 不重复请求)
   │     └─ early_stage         (新对话 → 无约束)
   ├─ f. 已回复?                 last_sender == "boss" → 跳过
   ├─ g. 学校筛选               match_school() ✗ → click_buheshi()
   ├─ h. 学历筛选               check_degree() ✗ → click_buheshi()
   ├─ i. 消息检查               无候选人消息 → 跳过
   ├─ j. 智能回复:
   │     ├─ detect_job()        消息+职位 → 匹配 job_id
   │     ├─ 合并 DB+AX 聊天历史(去重保序, 最近20条)
   │     └─ generate_reply():
   │           ├─ 模板匹配(专属→类别→兜底) → template_hint
   │           ├─ DeepSeek(system_prompt.md + 阶段约束 + 岗位信息 + 历史)
   │           └─ 降级: DeepSeek不可用 → 模板原文
   ├─ k. _reply_redundant()     回复还在问简历/微信 → 阶段兜底文本替换
   ├─ l. type_reply()           _clear_input() + cua type 输入(dry-run不发送)
   └─ 返回 result (含 uid + chat_history)
7. _save_chat_history()         聊天记录 upsert → candidates.db
   ├─ 有 uid → WHERE uid = ? 精准匹配
   └─ 无 uid → WHERE name = ? fallback
8. check_limit_popup()          每轮检测上限弹窗 → 遇到则终止
9. 随机间隔 1.5-3s              防风控
```

**关键特性**:
- **阶段感知**: 读取 DB 中 has_resume/has_wechat，不重复问已有信息，推动对话前进
- **上下文合并**: DB 历史聊天 + AX 树实时聊天合并去重，传给 DeepSeek 做上下文
- **uid 提取**: 点击联系人时从 DOM data-id 提取用户唯一标识，跨脚本精准匹配
- **系统提示词**: `config/system_prompt.md` 维护 HR 专家人设，修改即时生效
- **输入框清空**: 模拟 Cmd+A + Delete 键盘操作，兼容 React/Vue 框架
- **冗余兜底**: DeepSeek 回复仍问已有信息时，自动替换为阶段兜底文本

### cua_collect.py — 沟通页批量收集（简历+微信→SQLite）

```bash
python scripts/cua_collect.py --dry-run
python scripts/cua_collect.py --limit 10
python scripts/cua_collect.py --min-degree 硕士
```

流程: 进入聊天页 → AX树扫描联系人 → 逐个审查 → 提取uid+简历+微信 → upsert到candidates.db

**与 chat_loop 协作**: collect 写入 `has_resume`/`has_wechat`/`uid`，chat_loop 读取做阶段感知。共享 `app/db.py` 的 `init_db()` 保证表结构一致。

### cua_sync_jobs.py — 职位管理页职位信息同步

```bash
python scripts/cua_sync_jobs.py             # 预览
python scripts/cua_sync_jobs.py --write     # 提取+写入
python scripts/cua_sync_jobs.py --limit 3   # 调试
```

流程: 进入职位管理 → 扫描开放中岗位(同名去重) → 逐个点编辑:
- title: AXTextField(最短中文)
- requirements: JS直读iframe内textarea(绕过AX树200字截断)
- salary/degree/location: AXStaticText/AXTextField

## 话术模板 (templates.json)

模板按 `job_id` 组织，每个岗位有专属话术 + 全局 `fallback` 兜底。

| 岗位 | job_id | 模板数 | 特色场景 |
|------|--------|--------|----------|
| 开发 | dev | 10 | 技术栈/经验/架构/远程/到岗 |
| 营销总监 | annotation | 11 | KPI/KOL/内容渠道/AI背景 |
| CEO助理 | annotation-2 | 11 | 岗位定位/成长路径/期权/强度 |
| tech类别 | — | 7 | 技术栈/架构/经验/远程/开源/AI/项目 |
| nontech类别 | — | 7 | KPI/战略/成长/管理/数据/资源/创业 |
| 兜底 | fallback | 16 | 通用场景全覆盖 |

匹配策略: 模板匹配(专属→类别→兜底) → 命中则作 DeepSeek 提示词方向 → AI 结合上下文生成
                                    ↓ DeepSeek 未配置/失败
                                  降级返回模板原文

## DeepSeek API 配置 (可选)

```bash
cp .env.example .env  # 填入 DEEPSEEK_API_KEY
```

配置优先级: `.env` 文件 → 环境变量。未配置时自动降级为模板原文。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | (无) | API 密钥，必须 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |

## 系统提示词 (config/system_prompt.md)

用 Markdown 维护招聘官人设和策略，修改即时生效无需改代码。包含:
- 核心原则（简短自然、推进对话、针对性）
- 对话推进策略（初次接触→了解背景→确认意向→约面试）
- 阶段回复方向表
- 禁忌事项（不重复问、不说废话、不复制粘贴）
- 特殊情况处理（发简历附件/简短回复/表达顾虑）

## 共享数据库 (app/db.py)

`init_db()` 统一管理表结构和迁移，所有脚本通过 `from app.db import init_db, DB_PATH` 共用。

candidates 表核心字段:
- `uid` — BOSS 用户唯一标识（DOM data-id），跨脚本匹配键
- `has_resume` / `has_wechat` — collect 写入，chat_loop 读取做阶段感知
- `chat_history` — 聊天记录 JSON，chat_loop 写入
- `status` — collected / replied / unsuitable

## 筛选条件 (filter_criteria.py)

- 学校: 985/211/海外名校白名单
- 学历: 博士(4) > 硕士(3) > 本科(2) > 大专(1)
- 打招呼取卡片教育经历**最后一行**(时间最早=本科)，非最高学历

## cua-driver 集成要点

- BOSS聊天页联系人: `<span class="geek-name">` + JS点击 + `data-id` 提取uid
- 职位描述在iframe内: JS读 `iframe.contentDocument.querySelector('textarea').value`
- cua()函数对非JSON返回截断200字: JS必须返回 `JSON.stringify({status, uid})`
- 列表页卡片结构: 岗位名AXLink → 状态StaticText → 编辑AXLink(岗位名在编辑**前面**)
- 页面导航后索引全变: 用标题匹配不用位置索引
- 连续操作触发风控: 每步间隔1.5-3s随机
- 输入框清空: Cmd+A + Delete 模拟键盘操作，兼容 React/Vue 框架
