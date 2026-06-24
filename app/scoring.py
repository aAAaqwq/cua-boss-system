"""
候选人评分模块
============
多维度 AI 评分系统 — 按岗位类别/具体岗位自定义维度和权重（满分100）。

全部维度统一走 DeepSeek AI 评估，一次 API 调用完成。
配置: config/scoring.json
"""
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(__file__).parent.parent / "config"
DEFAULT_SCORING_CONFIG = CONFIG_DIR / "scoring.json"
SCORING_TEMPLATE = CONFIG_DIR / "scoring-template.json"

# ── DeepSeek 瞬时失败重试（与 chat_reply._post_deepseek 同模式）──
API_MAX_RETRIES = 2          # 额外重试次数（网络/429/5xx）
API_RETRY_BASE_DELAY = 1.0   # 指数退避基准秒数：1s, 2s, ...


# ══════════════════════════════════════════════════
# 数据类
# ══════════════════════════════════════════════════

@dataclass
class ScoreDimension:
    """单个评分维度定义"""
    key: str          # "tech_depth"
    name: str         # "技术深度"
    weight: int       # 35（满分100中的权重）
    description: str  # 维度说明


@dataclass
class DimensionScore:
    """单个维度的打分结果"""
    dimension: ScoreDimension
    raw_score: float          # 0-10 原始分
    weighted_score: float     # raw_score / 10 * weight
    evidence: str             # 打分依据（1-2 句中文）


@dataclass
class CandidateScore:
    """候选人完整评分结果"""
    candidate_name: str
    job_id: str
    dimensions: list[DimensionScore] = field(default_factory=list)
    total_score: float = 0.0
    summary: str = ""
    errors: list[str] = field(default_factory=list)
    skipped: bool = False  # True = 未满足评分前置条件（如无简历附件），未调用 DeepSeek

    @property
    def max_score(self) -> int:
        return sum(d.dimension.weight for d in self.dimensions)


# ══════════════════════════════════════════════════
# 配置加载
# ══════════════════════════════════════════════════

def load_scoring_config(config_path: Optional[str] = None) -> dict:
    """加载评分配置文件

    与项目其他配置一致的 template+local 双文件模式：
    优先读 `scoring.json`（本地自定义，gitignore），不存在则用
    `scoring-template.json`（提交到 git 的参考模板）兜底。
    """
    if config_path:
        path = Path(config_path)
    elif DEFAULT_SCORING_CONFIG.exists():
        path = DEFAULT_SCORING_CONFIG
    else:
        path = SCORING_TEMPLATE
    if not path.exists():
        raise FileNotFoundError(
            f"评分配置文件不存在: {DEFAULT_SCORING_CONFIG} / {SCORING_TEMPLATE}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_dimensions(
    job_id: str,
    category: str = "",
    config: Optional[dict] = None,
) -> list[ScoreDimension]:
    """解析岗位的评分维度 — 岗位覆盖优先，否则用类别默认

    返回按权重降序排列的维度列表。
    """
    if config is None:
        config = load_scoring_config()

    # 1. 岗位覆盖优先
    overrides = config.get("job_overrides", {})
    if job_id in overrides:
        dims = overrides[job_id].get("dimensions", [])
        if dims:
            return _parse_dimensions(dims)

    # 2. 类别默认
    defaults = config.get("category_defaults", {})
    if category and category in defaults:
        dims = defaults[category].get("dimensions", [])
        if dims:
            return _parse_dimensions(dims)

    # 3. 兜底：第一个可用的类别
    if defaults:
        first_cat = next(iter(defaults.values()))
        dims = first_cat.get("dimensions", [])
        if dims:
            return _parse_dimensions(dims)

    raise ValueError(
        f"无法解析岗位 '{job_id}'（类别 '{category}'）的评分维度。"
        f"请在 scoring.json 中配置 category_defaults 或 job_overrides。"
    )


def _parse_dimensions(raw: list[dict]) -> list[ScoreDimension]:
    """JSON 维度列表 → ScoreDimension 对象，按权重降序排列"""
    dims = [
        ScoreDimension(
            key=d["key"],
            name=d["name"],
            weight=d["weight"],
            description=d.get("description", ""),
        )
        for d in raw
    ]
    dims.sort(key=lambda d: d.weight, reverse=True)
    return dims


# ══════════════════════════════════════════════════
# 评级 + 输入上限（统一从 scoring.json 读，改配置即生效）
# ══════════════════════════════════════════════════

_DEFAULT_GRADES = [
    {"min": 85, "label": "S", "desc": "强烈推荐"},
    {"min": 70, "label": "A", "desc": "推荐"},
    {"min": 55, "label": "B", "desc": "可考虑"},
    {"min": 40, "label": "C", "desc": "待定"},
    {"min": 0,  "label": "D", "desc": "不推荐"},
]
_DEFAULT_LIMITS = {"resume_max_chars": 4000, "chat_max_turns": 30, "rescore_window_days": 2}

# 评分系统提示词（HR 简历评分专家人设 + 评分准则 + 输出要求）维护在 .md 中，
# 改文件即生效、无需动代码（与 chat 的 config/system_prompt.md 同模式）。
SCORING_PROMPT_FILE = CONFIG_DIR / "scoring_prompt.md"

# 兜底：.md 文件不存在时使用的最简提示词（仅作安全网，正常走 .md）
_FALLBACK_SCORING_PROMPT = (
    "你是一位顶尖的 HR 简历筛选与评分专家，结合岗位要求(JD)深入剖析候选人的项目经历、"
    "量化成果与岗位匹配度。评分严谨、有区分度、拒绝凭空拔高，信息不足时给偏低分。"
    "只返回 JSON，不要额外解释。"
)

# 模块级缓存：评分提示词只读一次盘（批量评分循环中避免反复 I/O）
_scoring_prompt_cache: Optional[str] = None


def grade(total: float, config: Optional[dict] = None) -> str:
    """总分 → 评级标签（如 "S 强烈推荐"）。阈值统一读 scoring.json 的 grades。

    全项目唯一的评级口径入口；report/排行榜都走这里，改 scoring.json 即生效。
    """
    grades = (config or {}).get("grades") or _DEFAULT_GRADES
    for g in sorted(grades, key=lambda x: x.get("min", 0), reverse=True):
        if total >= g.get("min", 0):
            return f"{g.get('label', '')} {g.get('desc', '')}".strip()
    return ""


def input_limits(config: Optional[dict] = None) -> dict:
    """传给 DeepSeek 的输入上限 + 重新评分窗口（带默认兜底）。"""
    limits = dict(_DEFAULT_LIMITS)
    limits.update((config or {}).get("input_limits") or {})
    return limits


def load_scoring_prompt() -> str:
    """从 config/scoring_prompt.md 加载评分系统提示词（缓存，首次读盘）。

    维护 HR 评分专家人设 + 评分准则 + 输出要求，修改 .md 即时生效无需改代码。
    文件不存在时降级为 `_FALLBACK_SCORING_PROMPT`。
    """
    global _scoring_prompt_cache
    if _scoring_prompt_cache is None:
        if SCORING_PROMPT_FILE.exists():
            _scoring_prompt_cache = SCORING_PROMPT_FILE.read_text(encoding="utf-8").strip()
        else:
            _scoring_prompt_cache = _FALLBACK_SCORING_PROMPT
    return _scoring_prompt_cache


# ══════════════════════════════════════════════════
# 统一的候选人详细数据：DB 行 → 标准字典 → 统一文本块
# （match_best_job 判断岗位 与 评分 共用同一份输入，喂给 DeepSeek 的数据一致）
# ══════════════════════════════════════════════════

_CANDIDATE_FIELDS = (
    "uid", "name", "job_position", "school", "degree",
    "resume_content", "resume_filename", "has_resume",
    "wechat", "has_wechat", "phone", "email", "notes",
    "chat_history", "status",
)


def build_candidate_data(row) -> dict:
    """candidates 表的一行（sqlite3.Row / dict）→ 标准候选人数据字典。

    统一字段集合，供 match_best_job / evaluate_candidate(_auto) 共用。
    """
    def _get(key):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return row.get(key) if isinstance(row, dict) else None
    return {k: _get(k) for k in _CANDIDATE_FIELDS}


def has_resume_content(candidate_data: dict) -> bool:
    """候选人是否有简历附件内容（评分前置条件）。

    简历附件是评分的主要依据，无内容则不评分。仅看 resume_content 实际文本，
    不依赖 has_resume 标志位（标志位可能为真但抓取内容为空）。
    """
    return bool((candidate_data.get("resume_content") or "").strip())


def _normalize_chat(chat_history) -> list[dict]:
    if isinstance(chat_history, str):
        try:
            chat_history = json.loads(chat_history)
        except (json.JSONDecodeError, TypeError):
            return []
    return [t for t in (chat_history or []) if isinstance(t, dict)]


def format_candidate_block(candidate_data: dict, config: Optional[dict] = None) -> str:
    """候选人详细数据 → 统一文本块（评分 / 岗位判断 prompt 共用）。

    简历截断长度、聊天条数由 scoring.json 的 input_limits 控制。
    """
    lim = input_limits(config)
    resume = (candidate_data.get("resume_content") or "")[: lim["resume_max_chars"]]
    chat = _normalize_chat(candidate_data.get("chat_history"))[-lim["chat_max_turns"]:]
    chat_lines = [f"[{t.get('role', '?')}] {t.get('content', '')}" for t in chat]
    chat_text = "\n".join(chat_lines) if chat_lines else "（无聊天记录）"

    wechat = candidate_data.get("wechat") or (
        "已交换" if candidate_data.get("has_wechat") else "无")
    notes = candidate_data.get("notes") or "无"

    return f"""- 姓名: {candidate_data.get('name', '未知')}
- 当前/应聘职位: {candidate_data.get('job_position', '') or '未知'}
- 学校 / 学历: {candidate_data.get('school', '') or '未知'} / {candidate_data.get('degree', '') or '未知'}
- 微信: {wechat}
- 备注: {notes}
- 简历:
{resume or '（未提供简历）'}
- 聊天记录（最近 {lim['chat_max_turns']} 条）:
{chat_text}"""


# ══════════════════════════════════════════════════
# DeepSeek API
# ══════════════════════════════════════════════════

def _load_env_file() -> None:
    """从项目 .env 加载环境变量（不覆盖已有值）"""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _get_deepseek_config() -> dict:
    """获取 DeepSeek 配置"""
    _load_env_file()
    return {
        "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
    }


# ══════════════════════════════════════════════════
# AI 评分 — 构建 prompt + 调用 DeepSeek
# ══════════════════════════════════════════════════

def _build_scoring_prompt(
    dimensions: list[ScoreDimension],
    candidate_data: dict,
    job_context: str = "",
    config: Optional[dict] = None,
) -> str:
    """构建包含全部维度的评分 prompt（候选人详细数据走统一格式块）"""

    candidate_block = format_candidate_block(candidate_data, config)

    dim_lines = [
        f"- **{d.name}**（权重 {d.weight}/100）: {d.description}" for d in dimensions
    ]
    dim_text = "\n".join(dim_lines)

    # 维度 key 模板（用于返回 JSON）
    dim_keys_str = ", ".join(
        f'"{d.key}": {{"score": 7, "evidence": "理由"}}' for d in dimensions
    )

    # 人设/评分准则/输出要求统一在 config/scoring_prompt.md（system 消息）；
    # 这里只拼装本次评分的动态数据 + 精确到维度 key 的 JSON 返回模板。
    return f"""请结合下面的「岗位要求」与候选人的「项目经历」，对各维度逐一打分。

## 岗位要求
{job_context or '（未提供）'}

## 候选人信息
{candidate_block}

## 评分维度（每个 0-10 分）
{dim_text}

## 严格返回 JSON（不要 markdown 代码块包裹）
{{"dimensions": {{ {dim_keys_str} }}, "summary": "一句话综合评价"}}"""


def _call_deepseek_scoring(
    dimensions: list[ScoreDimension],
    candidate_data: dict,
    job_context: str = "",
    config: Optional[dict] = None,
) -> tuple[dict[str, tuple[float, str]], str, list[str]]:
    """调用 DeepSeek 批量评估全部维度

    返回: ({dim_key: (raw_score, evidence)}, summary, errors)
    """
    cfg = _get_deepseek_config()
    errors = []

    if not cfg["api_key"]:
        msg = "DEEPSEEK_API_KEY 未配置 — 无法评分"
        errors.append(msg)
        return {}, "", errors

    prompt = _build_scoring_prompt(dimensions, candidate_data, job_context, config)
    dim_keys = {d.key for d in dimensions}

    payload = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": load_scoring_prompt()},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2600,  # 4维度×中文依据+总结的JSON易超长；1200会被截断成非法JSON→评分失败
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{cfg['base_url']}/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    # 带指数退避重试：网络错误/429/5xx **以及评分 JSON 被截断/非法**都重试。
    # （DeepSeek 偶发返回被截断的不完整 JSON——HTTP 200 但 content 不完整，不是网络错，
    #   故把内容 JSON 解析也放进重试循环，否则截断一次就永久评分失败。）
    last_err = ""
    result = None
    raw_text = ""
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            raw_text = data["choices"][0]["message"]["content"].strip()
            txt = raw_text
            if txt.startswith("```"):  # 去掉 markdown 代码块包裹
                lines = txt.split("\n")
                txt = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            result = json.loads(txt)   # 模型评分 JSON；截断→JSONDecodeError→重试
            break
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code != 429 and 400 <= e.code < 500:  # 4xx(非429)不可重试
                break
        except json.JSONDecodeError:
            last_err = f"返回非JSON(疑似截断): {raw_text[:120]}"
        except Exception as e:  # noqa: BLE001 — 网络层异常统一重试
            last_err = str(e)
        if attempt < API_MAX_RETRIES:
            time.sleep(API_RETRY_BASE_DELAY * (2 ** attempt))

    if result is None:
        errors.append(f"DeepSeek 评分失败: {last_err}")
        return {}, "", errors

    # 提取各维度分数
    dim_results = result.get("dimensions", {})
    scores = {}
    for key in dim_keys:
        entry = dim_results.get(key, {})
        if isinstance(entry, dict) and "score" in entry:
            try:
                raw = max(0.0, min(10.0, float(entry["score"])))
            except (ValueError, TypeError):
                scores[key] = (0.0, "AI 返回的该维度分数非法")
                errors.append(f"维度 '{key}' 分数非法: {entry.get('score')!r}")
                continue
            evidence = entry.get("evidence", "")
            scores[key] = (raw, evidence)
        else:
            scores[key] = (0.0, "AI 未返回该维度分数")
            errors.append(f"维度 '{key}' 未在 AI 响应中找到")

    summary = result.get("summary", "")
    return scores, summary, errors


# ══════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════

def evaluate_candidate(
    candidate_data: dict,
    job_id: str = "",
    category: str = "",
    job_context: str = "",
    config: Optional[dict] = None,
) -> CandidateScore:
    """评估单个候选人

    参数:
      candidate_data:
        - name: 候选人姓名（必须）
        - school: 学校名
        - degree: 学历
        - job_position: 当前职位
        - chat_history: 聊天记录（list[dict] 或 JSON 字符串）
        - resume_content: 简历文本
        - notes: 备注
      job_id: 岗位 id（用于解析评分维度）
      category: 岗位类别（tech/nontech，job_id 无覆盖时使用）
      job_context: 岗位描述文本（传给 AI 做参考）
      config: 评分配置字典，为 None 时自动加载

    返回: CandidateScore
    """
    if config is None:
        config = load_scoring_config()

    name = candidate_data.get("name", "未知")

    # 1. 解析维度
    try:
        dimensions = resolve_dimensions(job_id, category, config)
    except ValueError as e:
        return CandidateScore(candidate_name=name, job_id=job_id, errors=[str(e)])

    # 2. 全部维度走 AI 评分
    ai_scores, summary, errors = _call_deepseek_scoring(
        dimensions, candidate_data, job_context, config,
    )

    # 3. 组装结果
    results = []
    for dim in dimensions:
        raw, evidence = ai_scores.get(dim.key, (0.0, "评估不可用"))
        results.append(DimensionScore(
            dimension=dim,
            raw_score=raw,
            weighted_score=round(raw / 10 * dim.weight, 1),
            evidence=evidence,
        ))

    total = sum(d.weighted_score for d in results)

    return CandidateScore(
        candidate_name=name,
        job_id=job_id,
        dimensions=sorted(results, key=lambda d: d.dimension.weight, reverse=True),
        total_score=round(total, 1),
        summary=summary,
        errors=errors,
    )


def match_best_job(
    candidate_data: dict,
    jobs: list[dict],
    config: Optional[dict] = None,
) -> str:
    """让 DeepSeek 从开放岗位列表中判断候选人最匹配的岗位名。

    把开放岗位（岗位名/要求摘要）+ 候选人信息交给模型自行判断，
    比纯关键词匹配更准。返回岗位名（唯一键）；无 API key / 调用失败 /
    模型返回非法岗位名时返回 ""（调用方据此回退）。
    """
    if not jobs:
        return ""
    cfg = _get_deepseek_config()
    if not cfg["api_key"]:
        return ""

    valid_ids = []
    job_lines = []
    for j in jobs:
        jid = j.get("title", "")  # 岗位名即唯一键
        if not jid:
            continue
        valid_ids.append(jid)
        reqs = (j.get("requirements", "") or "").replace("\n", " ")[:200]
        job_lines.append(f'- 岗位="{jid}" | 要求摘要: {reqs}')
    jobs_text = "\n".join(job_lines)

    # 与评分共用同一份候选人详细数据块（统一输入）
    candidate_block = format_candidate_block(candidate_data, config)

    prompt = f"""你是招聘分流专家。下面是公司开放的岗位列表，请判断候选人最适合哪个岗位。

## 开放岗位
{jobs_text}

## 候选人信息
{candidate_block}

只返回 JSON（不要解释、不要 markdown 代码块）: {{"job_id": "最匹配岗位名(须与上面岗位名完全一致)", "reason": "一句话理由"}}
若实在无法判断，job_id 返回空字符串。"""

    payload = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": "你是招聘分流专家。只返回 JSON，不要额外解释。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{cfg['base_url']}/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw = data["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        result = json.loads(raw)
        jid = (result.get("job_id") or "").strip()
        return jid if jid in valid_ids else ""
    except Exception:
        return ""


def match_job_by_position(
    candidate_data: dict,
    jobs: list[dict],
    config: Optional[dict] = None,
) -> str:
    """根据候选人「沟通的职位」(job_position) 匹配 jobs.json 岗位 id。

    每段沟通都绑定一个 BOSS 招聘岗位（job_position），评分应针对候选人实际
    沟通的那个岗位，而非让模型从全部岗位里重新猜「最合适」的。

    流程：先用关键词匹配 detect_job(job_position + 最近候选人消息)，命中即返回；
    未命中（如 job_position 缺失/无关键词）才回退 DeepSeek match_best_job。
    返回 job_id，全部失败时返回 ""。
    """
    if not jobs:
        return ""

    from app.chat_reply import detect_job  # 延迟导入，避免循环依赖

    job_pos = (candidate_data.get("job_position") or "").strip()
    # 取最近一条候选人消息辅助关键词匹配
    chat = _normalize_chat(candidate_data.get("chat_history"))
    last_cand_msg = next(
        (t.get("content", "") for t in reversed(chat)
         if t.get("role") == "candidate"),
        "",
    )

    jid = detect_job(last_cand_msg, job_pos, jobs) or ""
    if jid:
        return jid

    # 沟通职位无法用关键词匹配 → 回退到 DeepSeek 自行判断最匹配岗位
    return match_best_job(candidate_data, jobs, config)


def evaluate_candidate_auto(
    candidate_data: dict,
    jobs: list[dict],
    config: Optional[dict] = None,
) -> CandidateScore:
    """端到端评分：按候选人「沟通的职位」匹配 jobs.json 岗位，再按该岗位维度评分。

    前置条件：必须有简历附件内容（has_resume_content），否则跳过评分（skipped=True，
    不调用 DeepSeek）—— 简历是主要评分依据。
    岗位匹配：match_job_by_position（沟通职位关键词优先，失败回退 DeepSeek 判断）；
    匹配成功 → 用该岗位 requirements 作上下文、按其类别取维度；
    匹配失败 → 回退按候选人 job_position 文本推断类别评分。
    """
    if config is None:
        config = load_scoring_config()

    name = candidate_data.get("name", "未知")

    # 前置条件：无简历附件内容 → 跳过评分（不调用 DeepSeek）
    if not has_resume_content(candidate_data):
        return CandidateScore(
            candidate_name=name, job_id="", skipped=True,
            errors=["未提供简历附件内容，跳过评分"],
        )

    jid = match_job_by_position(candidate_data, jobs, config)
    job = next((j for j in jobs if j.get("title") == jid), None)

    from app.chat_reply import infer_category  # 延迟导入，避免循环依赖

    if job:
        category = infer_category(job.get("title", ""), job.get("requirements", ""))
        job_ctx = f"{job.get('title', '')} — {job.get('requirements', '')}"
    else:
        job_pos = candidate_data.get("job_position", "")
        category = infer_category(job_pos)
        job_ctx = job_pos

    return evaluate_candidate(
        candidate_data, job_id=jid, category=category,
        job_context=job_ctx, config=config,
    )


def evaluate_candidates(
    candidates: list[dict],
    job_id: str = "",
    category: str = "",
    job_context: str = "",
    config: Optional[dict] = None,
) -> list[CandidateScore]:
    """批量评估候选人，按总分降序排列"""
    if config is None:
        config = load_scoring_config()

    results = [
        evaluate_candidate(c, job_id, category, job_context, config)
        for c in candidates
    ]
    results.sort(key=lambda s: s.total_score, reverse=True)
    return results


# ══════════════════════════════════════════════════
# 格式化输出
# ══════════════════════════════════════════════════

def format_score_report(
    score: CandidateScore, verbose: bool = False, config: Optional[dict] = None,
) -> str:
    """生成可读的评分报告"""
    lines = []
    total = score.total_score
    job = score.job_id or "（未指定）"
    grade_label = grade(total, config)

    lines.append(f"{'='*60}")
    lines.append(f"  候选人: {score.candidate_name}")
    lines.append(f"  岗位:   {job}")
    lines.append(f"  总分:   {total:.1f} / {score.max_score}  ({grade_label})")
    lines.append(f"{'='*60}")

    for d in score.dimensions:
        bar = _score_bar(d.raw_score)
        lines.append(
            f"  {d.dimension.name:<10s}  "
            f"{d.raw_score:4.1f}/10  ×{d.dimension.weight:>3d}%  = {d.weighted_score:5.1f}  {bar}"
        )
        if verbose and d.evidence:
            lines.append(f"            依据: {d.evidence}")

    if score.summary:
        lines.append(f"\n  💬 综合评价: {score.summary}")

    if score.errors:
        lines.append(f"\n  ⚠ 警告:")
        for err in score.errors:
            lines.append(f"    - {err}")

    lines.append("")
    return "\n".join(lines)


def _score_bar(score: float, width: int = 10) -> str:
    """分数可视化进度条"""
    filled = int(round(score / 10 * width))
    return "█" * filled + "░" * (width - filled)


def format_batch_report(
    scores: list[CandidateScore],
    top_n: int = 10,
    verbose: bool = False,
    config: Optional[dict] = None,
) -> str:
    """批量评分汇总报告"""
    if not scores:
        return "（无候选人数据）"

    lines = [
        f"\n{'='*60}",
        f"  候选人评分汇总（共 {len(scores)} 人，展示前 {min(top_n, len(scores))} 名）",
        f"{'='*60}",
        f"  {'排名':<4s} {'姓名':<12s} {'总分':>6s}  {'评级':<10s}",
        f"  {'-'*50}",
    ]

    for i, s in enumerate(scores[:top_n]):
        lines.append(
            f"  {i+1:<4d} {s.candidate_name[:12]:<12s} "
            f"{s.total_score:>6.1f}  {grade(s.total_score, config):<10s}"
        )

    if verbose:
        for s in scores[:top_n]:
            lines.append(format_score_report(s, verbose=True, config=config))

    return "\n".join(lines)
