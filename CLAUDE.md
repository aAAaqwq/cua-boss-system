# cua-boss-system

CUA 驱动的 BOSS直聘自动化系统 — 打招呼 + 批量聊天回复。

## 项目结构

```
cua-boss-system/
├── app/
│   ├── filter_criteria.py    # 名校白名单(985/211/海外) + 学校匹配 + 可扩展FilterCriteria
│   └── chat_reply.py         # 模板匹配 + DeepSeek API + 学历判断
├── config/
│   └── chat_templates.json   # 8条话术模板(按priority排序)
├── scripts/
│   ├── cua_greeting_loop.py  # 推荐页打招呼(扫描→筛选→点击)
│   ├── cua_chat_loop.py      # 聊天页批量回复(扫描未读→读对话→匹配模板→发送)
│   └── cua_review_loop.py    # ★ 候选人审查(逐个查看→学校筛选→不合适/回复)
└── CLAUDE.md
```

## 依赖

- Python 3.10+（零 pip 依赖，纯标准库）
- `cua-driver` CLI（macOS Accessibility API 驱动 Chrome）
- Chrome 浏览器（需打开 BOSS直聘页面）

## 两个主脚本

### cua_greeting_loop.py — 推荐牛人打招呼

```bash
python scripts/cua_greeting_loop.py                  # 最多5人
python scripts/cua_greeting_loop.py --dry-run         # 仅预览
python scripts/cua_greeting_loop.py --limit 3         # 最多3人
python scripts/cua_greeting_loop.py --no-refresh      # 不刷新页面
python scripts/cua_greeting_loop.py --schools "清华,北大"  # 自定义学校
```

流程: 启动session → 找Chrome窗口 → 导航到推荐页 → 扫描AX树找候选人 → 学校筛选 → 点击"打招呼" → 检测上限弹窗

### cua_chat_loop.py — 聊天页批量回复

```bash
python scripts/cua_chat_loop.py                       # 最多20人
python scripts/cua_chat_loop.py --dry-run             # 仅预览(输入不发送)
python scripts/cua_chat_loop.py --limit 10            # 最多10人
python scripts/cua_chat_loop.py --min-degree 硕士     # 最低学历
```

流程: 启动session → 找Chrome窗口 → 导航到聊天页 → 扫描AX树未读联系人 → 逐个点击→读对话→学历筛选→模板匹配→输入发送

### cua_review_loop.py — 候选人审查+回复（★ 推荐）

```bash
python scripts/cua_review_loop.py                       # 审查+回复(最多20人)
python scripts/cua_review_loop.py --dry-run             # 仅预览
python scripts/cua_review_loop.py --limit 10            # 最多10人
python scripts/cua_review_loop.py --schools "清华,北大"  # 自定义学校
python scripts/cua_review_loop.py --min-degree 硕士      # 最低学历
python scripts/cua_review_loop.py --scroll-pages 5       # 滚动5页加载更多
```

**逐个审查流程：**

```
扫描未读 → 逐个点击联系人 → 读右侧对话面板
  ├─ 判断上一句是谁发的
  │   ├─ 我们发的 → 🔵 已回复 → 跳过
  │   └─ 候选人发的 → 🟢 待回复 → 继续
  ├─ 提取学校 & 学历
  │   ├─ 学校不在白名单 → 🚫 点"不合适" (右上角按钮)
  │   ├─ 学历不达标 → 📉 点"不合适"
  │   └─ 符合条件 → 继续
  ├─ 匹配话术模板
  └─ 输入回复 (dry-run: 不发送)
```

**与 chat_loop 的核心区别:**

| 维度 | cua_chat_loop | cua_review_loop |
|------|-------------|-----------------|
| 筛选依据 | 学历 (min_degree) | 学校白名单 + 学历 |
| 未回复判断 | 简单检测 latest_message | 判断上一条消息的发送者 |
| 不符合处理 | 跳过 | 点击"不合适" |
| 已回复处理 | 可能重复回复 | 自动跳过 |

## 话术模板体系 (chat_templates.json)

| priority | id | 触发关键词 | 回复 |
|----------|----|-----------|------|
| 1 | greeting_reply | 你好,您好,hi,hello,在吗,请问 | 你好！看到你的简历很感兴趣，方便聊聊这个岗位的具体期望吗？ |
| 2 | salary_ask | 薪资,工资,待遇,薪酬,多少钱 | 薪资根据能力和经验定，我们可以先聊聊你的技术背景～ |
| 3 | resume_sent | 简历,作品集,项目经历,GitHub | 收到，我看一下，稍后回复你～ |
| 4 | interview_ask | 面试,流程,几轮,笔试 | 面试一般1-2轮技术面+1轮HR面… |
| 5 | location_ask | 在哪,地点,地址,城市 | 岗位在北京，可以线上先聊聊～ |
| 6 | time_ask | 实习,多久,时长,到岗,每周 | 实习至少3个月，每周至少4天… |
| 7 | tech_ask | 技术栈,做什么,用什么语言,框架 | 主要是 Python + React，AI 应用方向… |
| 99 | fallback | (空,兜底) | 收到，我稍后看一下回复你～ |

策略: 模板优先 → DeepSeek API 兜底 → fallback 模板

## cua-driver 集成关键细节

### BOSS直聘聊天页 DOM 结构

联系人名字在 `<span class="geek-name">` 中，不可通过 AX 树点击（AXStaticText 无 AXPress），必须用 JS 点击:

```javascript
// 找到名字 span → 逐级向上找 cursor:pointer 的父元素 → click()
var el = document.querySelector('.geek-name');  // textContent === '严彭杰'
el.click();  // span 自身就是 cursor:pointer
```

### AX 树联系人列表解析规则

```
未读              ← 开始标记
批量              ← 跳过
数字(1-99)        ← 未读条数(触发保存上一个联系人)
HH:MM / 昨天 / 前天 ← 时间
中文2-4字         ← 候选人姓名
短文本(<20字)     ← 职位
长文本(>5字)      ← 消息预览
```

### 核心操作模式

1. **窗口定位**: `list_apps` → 匹配 `com.google.Chrome` → `list_windows` → 匹配 `zhipin` 标题
2. **页面导航**: `page.execute_javascript` 设 `window.location.href`，然后 `wait_for_page` 等 AX 树稳定
3. **联系人点击**: JS 遍历 DOM 找 `.geek-name` → `click()`
4. **输入框定位**: AX 树找 `AXTextArea` → 取 `element_index` → `type_text`
5. **Dry-run 关键**: 只调 `type_text`，不调 `press_key`；正常模式 `type_text` 后跟 `press_key: return`
6. **限制检测**: JS 扫描弹窗/toast 文本 → 匹配 `LIMIT_KEYWORDS` → JS 移除 DOM + Escape 关闭
7. **滚动**: `scroll({direction: "down", amount: N})` 合成 PageDown

### 已验证的 cua-driver 工具调用

```bash
cua-driver start_session '{"session": "boss-dry-run"}'
cua-driver list_apps                           # 找 Chrome PID
cua-driver list_windows '{"pid": 15320}'       # 找 BOSS直聘 window_id
cua-driver get_window_state '{"pid":15320,"window_id":12503}'  # 快照 AX 树
cua-driver page '{"pid":15320,"window_id":12503,"action":"execute_javascript","javascript":"..."}'
cua-driver scroll '{"pid":15320,"window_id":12503,"direction":"down","amount":5}'
cua-driver type_text '{"pid":15320,"window_id":12503,"element_index":312,"text":"回复内容"}'
```

### 常见问题

- **Shell 中文字符转义**: 通过 `python3 -c` 构造 JSON 传参，避免 shell 直接处理中文
- **窗口切换**: Chrome 标签页切换后 window 不变但内容变，需重新 `get_window_state` 确认
- **离屏窗口**: `list_windows` 可能返回多个离屏窗口，优先 `is_on_screen=True` 的
- **AXStaticText 不可点击**: BOSS直聘联系人名是 AXStaticText，必须 JS DOM 点击

## 筛选条件 (filter_criteria.py)

- **学校白名单**: 39所985 + 73所211 + 美/英/港/新/加/澳/欧名校
- **学历等级**: 博士(4) > 硕士(3) > 本科(2) > 大专(1)
- **FilterCriteria**: 可扩展 dataclass，预留 age_range/tech_stack/industry 等字段
- **学校匹配**: 中文完全匹配，英文支持缩写互推（MIT ↔ Massachusetts Institute of Technology）

## 实际运行记录

### 2026-06-05 Dry-Run 验证

通过 cua-driver 驱动 Chrome BOSS直聘聊天页，成功完成 10 个联系人的 dry-run 批量回复:

1. 定位窗口: pid=15320, window_id=12503, 共 907 个 AX 元素，16 个沟通中
2. 滚动: `scroll(amount=5)` 和 `scroll(amount=10)` 逐步展开联系人列表
3. JS 点击: 通过 `.geek-name` span 定位并点击，成功率 10/10
4. 输入: `type_text(element_index)` 将模板回复填入输入框，**未按 Enter**
5. 处理结果: 10人全部输入成功，覆盖 greeting_reply / time_ask / tech_ask / resume_sent / fallback 五种模板
