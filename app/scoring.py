"""
候选人评分模块
============
多维度 AI 评分系统 — 按岗位类别/具体岗位自定义维度和权重（满分100）。

全部维度统一走 DeepSeek AI 评估，一次 API 调用完成。
配置: config/scoring.json
"""
import json
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(__file__).parent.parent / "config"
DEFAULT_SCORING_CONFIG = CONFIG_DIR / "scoring.json"


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

    @property
    def max_score(self) -> int:
        return sum(d.dimension.weight for d in self.dimensions)


# ══════════════════════════════════════════════════
# 配置加载
# ══════════════════════════════════════════════════

def load_scoring_config(config_path: Optional[str] = None) -> dict:
    """加载评分配置文件"""
    path = Path(config_path) if config_path else DEFAULT_SCORING_CONFIG
    if not path.exists():
        raise FileNotFoundError(f"评分配置文件不存在: {path}")
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
) -> str:
    """构建包含全部维度的评分 prompt"""

    name = candidate_data.get("name", "未知")
    job_position = candidate_data.get("job_position", "未知")
    school = candidate_data.get("school", "未知")
    degree = candidate_data.get("degree", "未知")

    # 聊天记录（最近 10 条）
    chat_history = candidate_data.get("chat_history") or []
    if isinstance(chat_history, str):
        try:
            chat_history = json.loads(chat_history)
        except (json.JSONDecodeError, TypeError):
            chat_history = []
    chat_lines = []
    for turn in (chat_history or [])[-10:]:
        if isinstance(turn, dict):
            role = turn.get("role", "?")
            content = turn.get("content", "")
            chat_lines.append(f"[{role}] {content}")
    chat_text = "\n".join(chat_lines) if chat_lines else "（无聊天记录）"

    # 简历
    resume = candidate_data.get("resume_content", "")
    resume_text = (resume or "（未提供简历）")[:800]

    # 备注
    notes = candidate_data.get("notes", "")
    if notes:
        resume_text += f"\n备注: {notes}"

    # 维度列表
    dim_lines = []
    for d in dimensions:
        dim_lines.append(f"- **{d.name}**（权重 {d.weight}/100）: {d.description}")
    dim_text = "\n".join(dim_lines)

    # 维度 key 模板（用于返回 JSON）
    dim_keys_str = ", ".join(
        f'"{d.key}": {{"score": 7, "evidence": "理由"}}' for d in dimensions
    )

    return f"""你是招聘评分专家。请根据候选人信息，对以下维度逐一打分。

## 岗位要求
{job_context or '（未提供）'}

## 候选人信息
- 姓名: {name}
- 当前职位: {job_position}
- 学校: {school}
- 学历: {degree}
- 简历: {resume_text}

## 聊天记录（最近对话）
{chat_text}

## 评分维度（每个 0-10 分）
{dim_text}

## 评分要求
- 每个维度给 0-10 的整数分
- 信息不足时保守估计（偏低），不要凭空猜测
- 每维度提供 1-2 句中文打分依据
- 提供一句话综合评价
- 严格返回 JSON（不要 markdown 代码块包裹）:
{{"dimensions": {{ {dim_keys_str} }}, "summary": "一句话综合评价"}}"""


def _call_deepseek_scoring(
    dimensions: list[ScoreDimension],
    candidate_data: dict,
    job_context: str = "",
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

    prompt = _build_scoring_prompt(dimensions, candidate_data, job_context)
    dim_keys = {d.key for d in dimensions}

    payload = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": "你是招聘评分专家。只返回 JSON，不要额外解释。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1200,
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
            raw_text = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        errors.append(f"DeepSeek API 调用失败: {e}")
        return {}, "", errors

    # 解析 JSON（处理 markdown 包裹）
    try:
        if raw_text.startswith("```"):
            lines = raw_text.split("\n")
            raw_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        errors.append(f"DeepSeek 返回非 JSON: {raw_text[:200]}")
        return {}, "", errors

    # 提取各维度分数
    dim_results = result.get("dimensions", {})
    scores = {}
    for key in dim_keys:
        entry = dim_results.get(key, {})
        if isinstance(entry, dict) and "score" in entry:
            raw = max(0, min(10, int(entry["score"])))
            evidence = entry.get("evidence", "")
            scores[key] = (float(raw), evidence)
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
        dimensions, candidate_data, job_context,
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

def format_score_report(score: CandidateScore, verbose: bool = False) -> str:
    """生成可读的评分报告"""
    lines = []
    total = score.total_score
    job = score.job_id or "（未指定）"

    if total >= 85:
        grade = "S — 强烈推荐"
    elif total >= 70:
        grade = "A — 推荐"
    elif total >= 55:
        grade = "B — 可考虑"
    elif total >= 40:
        grade = "C — 待定"
    else:
        grade = "D — 不推荐"

    lines.append(f"{'='*60}")
    lines.append(f"  候选人: {score.candidate_name}")
    lines.append(f"  岗位:   {job}")
    lines.append(f"  总分:   {total:.1f} / {score.max_score}  ({grade})")
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
        total = s.total_score
        if total >= 85:
            grade = "S 强烈推荐"
        elif total >= 70:
            grade = "A 推荐"
        elif total >= 55:
            grade = "B 可考虑"
        elif total >= 40:
            grade = "C 待定"
        else:
            grade = "D 不推荐"
        lines.append(f"  {i+1:<4d} {s.candidate_name[:12]:<12s} {total:>6.1f}  {grade:<10s}")

    if verbose:
        for s in scores[:top_n]:
            lines.append(format_score_report(s, verbose=True))

    return "\n".join(lines)
