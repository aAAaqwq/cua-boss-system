# 配置文件与核心系统详解

> 配置架构（template+local 双文件）、按任务选文件、筛选/话术/评分三大系统的内部细节。
> 安装见 [setup.md](setup.md)，命令速查见 [cli.md](cli.md)。

## 配置文件详解（Agent 须知）

### 配置架构总览 -- template+local 双文件模式

所有配置文件采用 `-template.json`（提交到 git）+ 同名 `.json`（本地自定义，`.gitignore`）模式。运行时优先读本地文件，不存在时用 template 兜底。

```
config/
├── filter-template.json   -->  filter.json (gitignore, 运行时读取)
├── jobs-template.json     -->  jobs.json (sync自动写入, 合并id/category)
├── reply-templates.json   -->  reply.json (gitignore, 运行时读取)
├── scoring-template.json  -->  scoring.json (gitignore, 运行时读取)
└── system_prompt.md          (git tracked, 手动编辑)
```

### 按任务判断改哪个文件

| 用户说... | 应该改这个文件 |
|-----------|----------------|
| 加/删学校白名单、调学历要求 | `config/filter.json`（不存在则改 `filter-template.json` 并复制） |
| 调整 DeepSeek 人设/回复策略 | `config/system_prompt.md` |
| 新增岗位评分维度 | `config/scoring.json`（不存在则 `cp config/scoring-template.json config/scoring.json` 后改 job_overrides 或 category_defaults） |
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

> **配置文件**：`scoring.json`（本地，gitignore，运行时优先）+ `scoring-template.json`（提交到 git 的参考模板，兜底）。首次自定义：`cp config/scoring-template.json config/scoring.json` 后编辑。

> **统一可改的评分细则**：维度/权重(`category_defaults`/`job_overrides`)、评级线(`grades`)、传给 DeepSeek 的输入上限(`input_limits`: `resume_max_chars`/`chat_max_turns`/`rescore_window_days`) 全在 `scoring.json` 一个文件，改这里即生效，无需动代码。评级口径全项目走 `scoring.grade()` 单一入口。

> **岗位自动判断（默认）**：评分前先让 **DeepSeek 从开放岗位列表中判断候选人最匹配的岗位**（`match_best_job` / `evaluate_candidate_auto`），再按该岗位类别取维度、用其 requirements 作上下文评分——比纯关键词匹配更准。`query_db.py --rank` 默认走此路径；`--job-id` 可强制指定岗位跳过判断。判断失败（无 key / 无法判断）则回退按 `job_position` 推断类别。

### 快速使用

```bash
# 评分入口 = 排行榜命令（DeepSeek 自行判断岗位 + 评分 + 缓存）
# 注: app/scoring.py 是库模块无 CLI，评分走 query_db --rank 或代码调用
python3 scripts/query_db.py --rank --days 2 --top 10
```

```python
# 代码调用 A：DeepSeek 自行判断岗位后评分（推荐）
from app.scoring import evaluate_candidate_auto, load_scoring_config, format_score_report
from app.chat_reply import load_jobs_config

config = load_scoring_config()
jobs = load_jobs_config().get("jobs", [])
score = evaluate_candidate_auto(
    candidate_data={
        "name": "张三", "school": "华中科技大学", "degree": "硕士",
        "job_position": "高级Java工程师",
        "chat_history": [...], "resume_content": "5年Java经验...",
    },
    jobs=jobs,            # 模型从这些开放岗位里挑最匹配的
    config=config,
)
print(format_score_report(score, verbose=True))

# 代码调用 B：显式指定岗位（跳过模型判断）
from app.scoring import evaluate_candidate
score = evaluate_candidate(candidate_data={...}, job_id="ai-fullstack",
                           category="tech", job_context="...", config=config)
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
