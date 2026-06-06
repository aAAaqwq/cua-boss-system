# cua-boss-system

cua-driver 驱动的 BOSS直聘自动化系统。

## 项目结构

```
cua-boss-system/
├── app/
│   ├── filter_criteria.py    # 名校白名单(985/211/海外) + 学校匹配 + FilterCriteria
│   └── chat_reply.py         # 模板匹配 + DeepSeek API + 学历判断 + 岗位检测
├── config/
│   ├── jobs.json             # 岗位配置(cua_sync_jobs.py 自动同步)
│   └── chat_templates.json   # 话术模板(按priority排序)
├── scripts/
│   ├── boss_click_buheshi.py   # "不合适"点击共享模块(6策略降级)
│   ├── cua_chat_loop.py    # 沟通页批量智能沟通(学校筛选+不合适+岗位感知回复)
│   ├── cua_collect.py      # 沟通页批量收集(简历+微信→SQLite)
│   ├── cua_greeting_loop.py  # 推荐页批量主动打招呼(学校筛选+学历筛选)
│   └── cua_sync_jobs.py          # 职位管理页职位信息同步(提取→覆盖写入jobs.json)
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

## 话术模板 (chat_templates.json)

| priority | id | 关键词 | 回复 |
|----------|----|--------|------|
| 1 | greeting_reply | 你好,您好,hi,hello,在吗,请问 | ... |
| 2 | salary_ask | 薪资,工资,待遇... | ... |
| 99 | fallback | (空,兜底) | 收到，我稍后看一下回复你～ |

策略: 岗位模板 → 通用模板 → DeepSeek API → fallback

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
