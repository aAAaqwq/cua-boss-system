"""
聊天回复模块
============
模板匹配 + DeepSeek API + 学历判断 + 岗位感知

零 pip 依赖，纯标准库。

配置:
  config/reply-templates.json — 话术模板参考（提交到 git）
  config/reply.json          — 本地话术配置（gitignore，运行时读取）
  config/jobs.json           — 岗位定义（同步自 BOSS）
  config/jobs-template.json  — 岗位元数据模板（手动维护 id）
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# ── DeepSeek 调用参数（集中配置，便于调优）──
DEEPSEEK_TIMEOUT = 30            # 单次请求超时（秒）
DEEPSEEK_MAX_TOKENS = 256        # 留足空间避免中文回复被截在半句
DEEPSEEK_TEMPERATURE = 0.6       # 略低于 0.7，招聘话术更稳定一致
API_MAX_RETRIES = 2              # 瞬时失败（网络/429/5xx）的额外重试次数
API_RETRY_BASE_DELAY = 1.0       # 指数退避基准秒数：1s, 2s, ...
JOB_CONTEXT_MAX_CHARS = 220      # 岗位信息注入上限，避免长 requirements 撑爆 prompt
REPLY_MAX_CHARS = 140            # 回复软上限（系统提示词要求 ≤80 字，此为兜底裁剪）
HISTORY_MAX_TURNS = 20           # 传给 DeepSeek 的最近对话轮数

# ── 学历等级（从 filter_criteria 导入，保持向后兼容）──
from app.filter_criteria import check_degree, DEGREE_RANK  # noqa: E402


# ══════════════════════════════════════════════════
# 配置加载 — templates.json + jobs.json 双文件模式
# 兼容旧格式: jobs.json 内嵌 templates（v4-v5）
# ══════════════════════════════════════════════════

CONFIG_DIR = Path(__file__).parent.parent / "config"
DEFAULT_JOBS_CONFIG = CONFIG_DIR / "jobs.json"
DEFAULT_TEMPLATES_CONFIG = CONFIG_DIR / "reply.json"
TEMPLATES_FALLBACK = CONFIG_DIR / "reply-templates.json"


def _sort_templates(templates: list[dict]) -> list[dict]:
    """按 priority 排序模板列表"""
    return sorted(templates, key=lambda t: t.get("priority", 99))


def load_jobs_config(config_path: Optional[str] = None) -> dict:
    """加载岗位配置 + 话术模板

    读取 reply.json（本地，gitignore）→ 不存在则用 reply-templates.json 兜底

    返回:
      {
        "jobs": [
          {
            "id": "开发",  # id = 岗位名（由 cua_sync_jobs.gen_id 规范化生成）
            "title": "开发",
            "requirements": "...", "location": "广州", "salary": "16K-30K", "degree": "本科",
            "templates": [...],          # 岗位专属，按 priority 排序
            "category_templates": [...], # 类别通用 (tech/nontech)，运行时按岗位名推断
          },
          ...
        ],
        "fallback_templates": [...],
        "mode": "templates" | "jobs"
      }
    """
    config_dir = Path(config_path).parent if config_path else CONFIG_DIR
    jobs_path = Path(config_path) if config_path else DEFAULT_JOBS_CONFIG
    # reply.json 优先（本地自定义），不存在则用 reply-templates.json 兜底
    templates_path = config_dir / "reply.json"
    if not templates_path.exists():
        templates_path = config_dir / "reply-templates.json"

    # ── 模式1: reply.json + jobs.json 双文件 ──
    if templates_path.exists() and jobs_path.exists():
        try:
            tpl_data = json.loads(templates_path.read_text(encoding="utf-8"))
            job_data = json.loads(jobs_path.read_text(encoding="utf-8"))
            tpl_jobs = tpl_data.get("jobs", {})
            tpl_categories = tpl_data.get("categories", {})
            fallback = _sort_templates(tpl_data.get("fallback", []))

            jobs = []
            for j in job_data.get("jobs", []):
                jid = j.get("id", "")
                category = infer_category(j.get("title", ""), j.get("requirements", ""))
                job = dict(j)
                job["templates"] = _sort_templates(tpl_jobs.get(jid, []))
                job["category_templates"] = _sort_templates(tpl_categories.get(category, []))
                jobs.append(job)

            return {
                "jobs": jobs,
                "fallback_templates": fallback,
                "mode": "templates",
            }
        except (json.JSONDecodeError, KeyError):
            pass

    # ── 模式2: jobs.json 内嵌 templates（v4-v5 兼容）──
    if jobs_path.exists():
        try:
            data = json.loads(jobs_path.read_text(encoding="utf-8"))
            if "jobs" in data:
                jobs = []
                for job in data["jobs"]:
                    j = dict(job)
                    j["templates"] = _sort_templates(job.get("templates", []))
                    jobs.append(j)
                return {
                    "jobs": jobs,
                    "fallback_templates": _sort_templates(data.get("fallback_templates", [])),
                    "mode": "jobs",
                }
        except (json.JSONDecodeError, KeyError):
            pass

    # ── 最终兜底 ──
    return {
        "jobs": [],
        "fallback_templates": [
            {"id": "fallback", "reply": "收到，我稍后看一下回复你～", "match_keywords": [], "priority": 99}
        ],
        "mode": "minimal",
    }


# ══════════════════════════════════════════════════
# 岗位检测 — 从候选人消息/职位推断目标岗位
# ══════════════════════════════════════════════════

def infer_category(title: str, requirements: str = "") -> str:
    """根据岗位标题和需求推断类别: tech / nontech

    >>> infer_category("Python开发工程师")
    'tech'
    >>> infer_category("营销总监")
    'nontech'
    """
    combined = f"{title} {requirements}".lower()
    tech_keywords = [
        "开发", "工程师", "java", "python", "前端", "后端", "全栈",
        "架构", "算法", "数据", "运维", "测试", "devops", "sre",
        "android", "ios", "flutter", "react", "vue", "node",
        "golang", "rust", "c++", "程序员", "coding", "技术",
    ]
    for kw in tech_keywords:
        if kw in combined:
            return "tech"
    return "nontech"


def _job_match_tokens(title: str, requirements: str) -> tuple[set, set]:
    """从岗位名/要求自动提取匹配 token —— 取代旧的硬编码 role_keywords。

    - eng: 英文/数字词(>=2)，覆盖 java/spring/kol/saas 等技术或业务术语
    - cn:  岗位名的中文 bigram + 短整词，适配无空格的中文标题
    全部来自岗位自身字段，不依赖任何写死的 id→关键词 映射。
    """
    blob = f"{title} {requirements}".lower()
    eng = {t for t in re.findall(r"[a-z0-9]{2,}", blob)}
    cn = set()
    for run in re.findall(r"[一-鿿]+", title):
        if len(run) <= 4:
            cn.add(run)  # 短词整体（如「开发」「营销总监」）
        for i in range(len(run) - 1):
            cn.add(run[i:i + 2])  # bigram（如「开发」「发工」…）
    return eng, cn


def detect_job(
    candidate_message: str,
    candidate_job: str = "",
    jobs: list[dict] = None,
) -> Optional[str]:
    """根据候选人消息和「沟通职位」，推断对应哪个招聘岗位

    匹配信号（均来自岗位自身字段或配置，无硬编码 id→关键词 映射）:
      1. 沟通职位(job_position) 与岗位名直接对应 —— 最强信号。
         job_position 本就是 BOSS 岗位名，而 id 也=岗位名，故通常精确命中。
      2. 配置可选 match_keywords —— 业务关键词放进 jobs 配置而非代码。
      3. 岗位名/要求与对话文本的 token 重叠（英文词 + 中文 bigram）。

    返回 job_id 或 None（无法判断时）。
    """
    if not jobs:
        return None

    combined = f"{candidate_job} {candidate_message}".lower()
    cand_job = re.sub(r"\s+", " ", candidate_job.strip().lower())

    job_scores = []
    for job in jobs:
        title = re.sub(r"\s+", " ", (job.get("title") or "").strip().lower())
        if not title:
            continue
        jid = job.get("id", "")
        score = 0

        # 1. 沟通职位 ↔ 岗位名 直接对应（最强信号）
        if cand_job and (cand_job == title or cand_job in title or title in cand_job):
            score += 10

        # 2. 配置可选 match_keywords（业务关键词移入配置）
        for kw in job.get("match_keywords", []):
            if kw and str(kw).lower() in combined:
                score += 3

        # 3. token 重叠（自动提取，适配中英文）
        eng, cn = _job_match_tokens(title, job.get("requirements", ""))
        score += sum(2 for t in eng if t in combined)
        score += sum(1 for bg in cn if bg in combined)

        if score > 0:
            job_scores.append((score, jid))

    if job_scores:
        job_scores.sort(reverse=True)
        return job_scores[0][1]

    return None


# ══════════════════════════════════════════════════
# 模板变量替换 — 将 {salary}/{location} 等替换为 jobs.json 字段
# ══════════════════════════════════════════════════

TEMPLATE_VARS = ["salary", "location", "title", "requirements", "degree"]


def _substitute_vars(reply: str, job: dict = None) -> str:
    """替换模板中的 {field} 占位符为 job 对应字段值

    >>> _substitute_vars("薪资{salary}", {"salary": "16K-30K"})
    '薪资16K-30K'
    """
    if not job:
        return reply
    result = reply
    for var in TEMPLATE_VARS:
        placeholder = "{" + var + "}"
        if placeholder in result:
            value = job.get(var, "")
            if value:
                result = result.replace(placeholder, value)
    return result


# ══════════════════════════════════════════════════
# 模板匹配
# ══════════════════════════════════════════════════

def match_template(
    message: str,
    templates: list[dict],
    fallback_templates: list[dict] = None,
    job: dict = None,
) -> Optional[str]:
    """关键词匹配：先在 templates 中找，再在 fallback_templates 中找

    返回匹配到的 reply 文本（已替换 {变量}），或 None
    """
    if not message:
        return None

    all_templates = list(templates)
    if fallback_templates:
        all_templates.extend(fallback_templates)
    # 按 priority 排序
    all_templates.sort(key=lambda t: t.get("priority", 99))

    msg_lower = message.lower()
    for tpl in all_templates:
        keywords = tpl.get("match_keywords", [])
        if not keywords:
            continue  # 跳过纯兜底（空关键词）
        for kw in keywords:
            if kw.lower() in msg_lower:
                return _substitute_vars(tpl["reply"], job)
    return None


def get_fallback_reply(templates: list[dict], fallback_templates: list[dict] = None) -> str:
    """获取兜底回复：取 match_keywords 为空的第一条"""
    for tpl in templates:
        if not tpl.get("match_keywords"):
            return tpl["reply"]
    if fallback_templates:
        for tpl in fallback_templates:
            if not tpl.get("match_keywords"):
                return tpl["reply"]
    return "收到，我稍后看一下回复你～"


# ══════════════════════════════════════════════════
# ══════════════════════════════════════════════════
# DeepSeek API — 结合模板提示词 + 聊天上下文智能生成回复
# ══════════════════════════════════════════════════

# API 配置优先级: 环境变量 > .env 文件
#   DEEPSEEK_API_KEY   — API 密钥（必须）
#   DEEPSEEK_BASE_URL  — 接口地址（默认 https://api.deepseek.com）
#   DEEPSEEK_MODEL     — 模型名（默认 deepseek-chat）

SYSTEM_PROMPT_FILE = Path(__file__).parent.parent / "config" / "system_prompt.md"

# 兜底: 文件不存在时使用的最简提示词
_FALLBACK_PROMPT = (
    "你是BOSS直聘上的招聘官。\n"
    "规则：回复不超过80字，简洁自然。根据对话上下文针对性回复，不要泛泛而谈。"
)


# 模块级缓存：系统提示词只读一次盘（批量循环中避免反复 I/O）
_system_prompt_cache: Optional[str] = None


def _load_system_prompt() -> str:
    """从 config/system_prompt.md 加载系统提示词（缓存，首次读盘）"""
    global _system_prompt_cache
    if _system_prompt_cache is None:
        if SYSTEM_PROMPT_FILE.exists():
            _system_prompt_cache = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        else:
            _system_prompt_cache = _FALLBACK_PROMPT
    return _system_prompt_cache


# 模块级标志：.env 只解析一次
_env_loaded = False


def _load_env_file() -> None:
    """从项目根目录 .env 文件加载环境变量（不覆盖已有值，仅解析一次）"""
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
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
    """获取 DeepSeek 配置，返回 {api_key, base_url, model}"""
    _load_env_file()
    return {
        "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
    }


# 模块级缓存：首次检查后记住结果，避免重复读 .env
_deepseek_configured: Optional[bool] = None


def check_deepseek_configured() -> bool:
    """检查 DeepSeek API 是否已配置，未配置时打印醒目警告（仅首次调用）"""
    global _deepseek_configured
    if _deepseek_configured is not None:
        return _deepseek_configured
    cfg = _get_deepseek_config()
    _deepseek_configured = bool(cfg["api_key"])
    if not _deepseek_configured:
        print("=" * 60)
        print("⚠️  DeepSeek API 未配置！所有智能回复将降级为模板原文")
        print("    cp .env.example .env 并填入 DEEPSEEK_API_KEY")
        print("=" * 60)
    return _deepseek_configured


def _truncate(text: str, limit: int) -> str:
    """硬截断，超长加省略号"""
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _trim_reply(reply: str, limit: int = REPLY_MAX_CHARS) -> str:
    """回复软裁剪：超长时尽量在句末断句，避免发出半句"""
    reply = reply.strip()
    if len(reply) <= limit:
        return reply
    window = reply[:limit]
    for sep in ("。", "！", "？", "～", "\n", "."):
        idx = window.rfind(sep)
        if idx >= limit * 0.5:  # 断点不能太靠前，否则信息量不足
            return window[: idx + 1].strip()
    return window.rstrip() + "…"


def _build_messages(
    candidate_name: str,
    candidate_message: str,
    history: Optional[list[dict]],
) -> list[dict]:
    """组装 messages：角色映射 + 去掉与最新消息重复的末尾轮次 + 合并相邻同角色

    BOSS 脚本 role → OpenAI role：candidate→user, boss→assistant,
    system→assistant(包成[系统通知])。最新候选人消息单独作为末尾 user 消息，
    若历史末尾已是同一句则剔除，避免重复发送。
    """
    latest = candidate_message.strip()
    turns: list[tuple[str, str]] = []
    for turn in (history or [])[-HISTORY_MAX_TURNS:]:
        raw = turn.get("role", "user")
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        if raw == "candidate":
            role = "user"
        elif raw == "boss":
            role = "assistant"
        elif raw == "system":
            role, content = "assistant", f"[系统通知: {content}]"
        else:
            role = raw if raw in ("user", "assistant") else "user"
        turns.append((role, content))

    # 去掉与最新消息完全相同的末尾候选人轮次（否则会发两遍）
    if turns and turns[-1][0] == "user" and turns[-1][1] == latest:
        turns.pop()

    # 末尾追加格式化的最新消息
    turns.append(("user", f"候选人({candidate_name})说：{candidate_message}"))

    # 合并相邻同角色消息（DeepSeek 对连续同角色不友好）
    coalesced: list[tuple[str, str]] = []
    for role, content in turns:
        if coalesced and coalesced[-1][0] == role:
            coalesced[-1] = (role, coalesced[-1][1] + "\n" + content)
        else:
            coalesced.append((role, content))

    return [{"role": r, "content": c} for r, c in coalesced]


def _post_deepseek(req: urllib.request.Request) -> tuple[Optional[dict], str]:
    """带指数退避重试的 POST：网络错误/429/5xx 重试，4xx(非429)直接放弃"""
    last_err = ""
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=DEEPSEEK_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8")), ""
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            # 客户端错误(除限流)不可重试
            if e.code != 429 and 400 <= e.code < 500:
                return None, last_err
        except Exception as e:  # noqa: BLE001 — 网络层异常统一重试
            last_err = str(e)
        if attempt < API_MAX_RETRIES:
            time.sleep(API_RETRY_BASE_DELAY * (2 ** attempt))
    return None, last_err


def call_deepseek(
    candidate_name: str,
    candidate_message: str,
    history: Optional[list[dict]] = None,
    job_context: str = "",
    template_hint: str = "",
    stage_context: str = "",
) -> tuple[Optional[str], str]:
    """调用 DeepSeek API 生成回复

    参数:
      template_hint: 匹配到的模板文本，作为「建议回复方向」注入 system prompt
      stage_context: 对话阶段上下文，告知 DeepSeek 当前阶段和禁忌
    返回: (reply, error_msg)
    """
    cfg = _get_deepseek_config()
    if not cfg["api_key"]:
        return None, "DEEPSEEK_API_KEY not set"

    system = _load_system_prompt()
    if job_context:
        system += f"\n\n---\n当前招聘的岗位信息: {_truncate(job_context, JOB_CONTEXT_MAX_CHARS)}"
    if template_hint:
        system += f"\n建议回复方向: {template_hint}"
    if stage_context:
        system += f"\n对话阶段上下文:\n{stage_context}"

    messages = [{"role": "system", "content": system}]
    messages.extend(_build_messages(candidate_name, candidate_message, history))

    payload = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": DEEPSEEK_TEMPERATURE,
        "max_tokens": DEEPSEEK_MAX_TOKENS,
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

    data, err = _post_deepseek(req)
    if data is None:
        return None, err
    try:
        return _trim_reply(data["choices"][0]["message"]["content"]), ""
    except (KeyError, IndexError, TypeError) as e:
        return None, f"unexpected response: {e}"


# ══════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════

# ── 模板匹配层级（用于回复来源可观测性）──
MATCH_JOB = "job"            # ① 岗位专属模板
MATCH_CATEGORY = "category"  # ② 类别通用模板 (tech/nontech)
MATCH_FALLBACK = "fallback"  # ③ 全局兜底模板
MATCH_NONE = "none"          # 三层均未命中关键词

# 回复来源
SOURCE_DEEPSEEK = "deepseek"            # AI 生成
SOURCE_TEMPLATE = "template"            # DeepSeek 不可用/失败，降级为模板原文
SOURCE_FINAL_FALLBACK = "final_fallback"  # 无模板命中且无 AI，最终兜底文本


def generate_reply(
    message: str,
    templates: list[dict],
    candidate_name: str = "",
    history: Optional[list[dict]] = None,
    job_templates: list[dict] = None,
    category_templates: list[dict] = None,
    fallback_templates: list[dict] = None,
    job_context: str = "",
    job: dict = None,
    stage_context: str = "",
) -> dict:
    """生成回复 — 模板匹配 + DeepSeek 智能生成

    流程:
      1. 关键词匹配模板（岗位专属 → 类别通用 → 全局兜底）
      2. 命中模板 → 用模板文本作为 DeepSeek 提示词，AI 结合上下文生成回复
      3. DeepSeek 不可用/失败 → 降级返回模板原文
      4. 无模板匹配 → DeepSeek 仅凭岗位上下文生成
      5. 全部失败 → 最终兜底文本

    参数:
      message: 候选人最新消息
      templates: 旧版通用模板（兼容）
      candidate_name: 候选人称呼
      history: 对话历史 [{"role":"assistant","content":"..."}, ...]
      job_templates: 当前岗位专属模板
      category_templates: 当前岗位类别模板 (tech/nontech)
      fallback_templates: 全局兜底模板
      job_context: 岗位描述文本 (传给 DeepSeek)
      job: 岗位字典 (含 salary/location/title 等，用于模板变量替换)

    返回:
      {
        "reply": str,             # 最终回复文本
        "match_layer": MATCH_*,   # 命中的模板层级（job/category/fallback/none）
        "source": SOURCE_*,       # 回复来源（deepseek/template/final_fallback）
      }
    """
    # 1. 匹配模板 — 三层 fallback，记录命中层级
    template_hint = None
    match_layer = MATCH_NONE
    if job_templates:
        template_hint = match_template(message, job_templates, job=job)
        if template_hint:
            match_layer = MATCH_JOB
    if not template_hint and category_templates:
        template_hint = match_template(message, category_templates, job=job)
        if template_hint:
            match_layer = MATCH_CATEGORY
    if not template_hint:
        all_fallback = list(fallback_templates or [])
        template_hint = match_template(message, templates, all_fallback, job=job)
        if template_hint:
            match_layer = MATCH_FALLBACK

    # 2. 尝试 DeepSeek 智能生成（模板作提示词 + 聊天上下文）
    if check_deepseek_configured():
        reply, err = call_deepseek(
            candidate_name, message, history,
            job_context=job_context,
            template_hint=template_hint or "",
            stage_context=stage_context,
        )
        if reply:
            return {"reply": reply, "match_layer": match_layer, "source": SOURCE_DEEPSEEK}
        # API 调用失败（非配置问题）→ 警告
        print(f"    ⚠ DeepSeek 调用失败: {err}")

    # 3. DeepSeek 不可用 — 降级返回模板原文
    if template_hint:
        return {"reply": template_hint, "match_layer": match_layer, "source": SOURCE_TEMPLATE}

    # 4. 最终兜底
    fallback_reply = get_fallback_reply(
        job_templates or [],
        (fallback_templates or []) + templates,
    )
    return {"reply": fallback_reply, "match_layer": MATCH_NONE, "source": SOURCE_FINAL_FALLBACK}


