# cua-boss-system

cua-driver 驱动的 BOSS直聘自动化系统。

## 项目结构

```
cua-boss-system/
├── app/
│   ├── filter_criteria.py    # 名校白名单(985/211/海外) + 学校匹配 + 学历判断
│   └── chat_reply.py         # 模板匹配 + DeepSeek + 岗位检测 + 变量替换
├── config/
│   ├── jobs.json             # 岗位配置(cua_sync_jobs.py 自动同步)
│   └── templates.json        # 话术模板(专属→类别→兜底 三层)
├── scripts/
│   ├── boss_click_buheshi.py   # "不合适"点击共享模块(CGEvent原生鼠标)
│   ├── cua_chat_loop.py        # 沟通页批量智能沟通
│   ├── cua_collect.py          # 沟通页批量收集(简历+微信→SQLite)
│   ├── cua_greeting_loop.py    # 推荐页批量主动打招呼
│   └── cua_sync_jobs.py        # 职位管理页同步岗位信息
├── .env.example              # DeepSeek API 配置模板
├── SKILL.md
├── CLAUDE.md
└── README.md
```

## 依赖

- Python 3.10+（纯标准库）
- `cua-driver` CLI
- `swiftc`（首次运行自动编译 CGEvent 鼠标工具 `/tmp/cua_hid`）
- Chrome（需登录 BOSS直聘）

## 三个脚本

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

流程: 进入聊天页 → 扫描未读 → 逐个审查:
1. 点联系人 → 读对话面板(学校/学历/消息)
2. 上一句是我们发的 → 跳过
3. 学校不在白名单 → 点"不合适"
4. 学历不达标 → 点"不合适"
5. 符合条件 → 岗位检测 → 专属话术 → 输入回复

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

匹配策略: 模板匹配(专属→类别→兜底) → 命中则作 DeepSeek 提示词 → AI 结合上下文生成
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

## 筛选条件 (filter_criteria.py)

- 学校: 985/211/海外名校白名单
- 学历: 博士(4) > 硕士(3) > 本科(2) > 大专(1)
- 打招呼取卡片教育经历**最后一行**(时间最早=本科)，非最高学历

## cua-driver 集成要点

- BOSS聊天页联系人: `<span class="geek-name">` + JS点击
- 职位描述在iframe内: JS读 `iframe.contentDocument.querySelector('textarea').value`
- cua()函数对非JSON返回截断200字: JS必须返回 `JSON.stringify({text: ...})`
- 列表页卡片结构: 岗位名AXLink → 状态StaticText → 编辑AXLink(岗位名在编辑**前面**)
- 页面导航后索引全变: 用标题匹配不用位置索引
- 连续操作触发风控: 每岗间隔3-7秒
