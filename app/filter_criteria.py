"""
筛选条件模块 — 从 config/filter.json 加载配置，不存在则用 filter-template.json 兜底

包含:
    - 名校白名单 (国内 + 海外)
    - 可扩展的 FilterCriteria 数据类
    - 学校匹配 + 学历判断
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

FILTER_CONFIG_DIR = Path(__file__).parent.parent / "config"

# ── 加载筛选配置：filter.json 优先 → filter-template.json 兜底 ──
_filter_path = FILTER_CONFIG_DIR / "filter.json"
if not _filter_path.exists():
    _filter_path = FILTER_CONFIG_DIR / "filter-template.json"

_filter_data = {}
if _filter_path.exists():
    try:
        _filter_data = json.loads(_filter_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError):
        pass

# 学历等级
DEGREE_RANK = _filter_data.get("degree_rank", {"博士": 4, "硕士": 3, "本科": 2, "大专": 1})
# 默认最低学历
DEFAULT_MIN_DEGREE = _filter_data.get("min_degree", "本科")


# ========== 名校白名单 ==========

# 国内名校 (完整 985 + 211 工程高校)
DOMESTIC_ELITE_SCHOOLS = [
    # ── 985 高校 (39所) ──
    "清华大学", "北京大学", "浙江大学", "复旦大学",
    "上海交通大学", "华中科技大学", "武汉大学", "中山大学",
    "南京大学", "西安交通大学", "哈尔滨工业大学", "中国科学技术大学",
    "中国人民大学", "同济大学", "北京航空航天大学", "北京理工大学",
    "天津大学", "南开大学", "东南大学", "厦门大学", "四川大学",
    "电子科技大学", "华南理工大学", "中南大学", "湖南大学",
    "北京师范大学", "华东师范大学", "吉林大学", "大连理工大学",
    "西北工业大学", "重庆大学", "山东大学", "兰州大学",
    "中国农业大学", "西北农林科技大学", "中央民族大学",
    "国防科技大学", "中国海洋大学", "东北大学",
    # ── 211 高校 (非985部分，73所) ──
    "北京交通大学", "北京工业大学", "北京科技大学", "北京化工大学",
    "北京邮电大学", "北京林业大学", "北京外国语大学", "中国传媒大学",
    "中央财经大学", "对外经济贸易大学", "中国政法大学", "中央音乐学院",
    "北京中医药大学", "华北电力大学", "中国矿业大学（北京）", "中国石油大学（北京）",
    "中国地质大学（北京）",
    "河北工业大学",
    "上海外国语大学", "上海财经大学", "上海大学", "东华大学",
    "苏州大学", "南京航空航天大学", "南京理工大学", "中国矿业大学",
    "河海大学", "江南大学", "南京农业大学", "中国药科大学", "南京师范大学",
    "安徽大学", "合肥工业大学",
    "福州大学", "南昌大学",
    "郑州大学", "太原理工大学",
    "武汉理工大学", "华中农业大学", "华中师范大学", "中南财经政法大学",
    "湖南师范大学",
    "广东工业大学",
    "华南师范大学", "暨南大学", "广西大学",
    "西南交通大学", "西南财经大学", "四川农业大学",
    "贵州大学", "云南大学",
    "西藏大学",
    "西北大学", "西安电子科技大学", "长安大学", "陕西师范大学",
    "青海大学", "宁夏大学", "新疆大学", "石河子大学",
    "海南大学",
    "内蒙古大学",
    "辽宁大学", "大连海事大学", "东北师范大学", "延边大学",
    "东北林业大学", "东北农业大学", "哈尔滨工程大学",
    "中国石油大学（华东）", "中国地质大学（武汉）",
    # ── 双一流 非211（第二轮2022）──
    # 综合
    "中国科学院大学", "南方科技大学", "上海科技大学", "山西大学", "湘潭大学", "河南大学",
    # 医药
    "北京协和医学院", "南京医科大学", "南方医科大学", "广州医科大学",
    "上海中医药大学", "南京中医药大学", "广州中医药大学", "成都中医药大学", "天津中医药大学",
    # 农林
    "华南农业大学", "南京林业大学", "上海海洋大学",
    # 理工
    "南京邮电大学", "南京信息工程大学", "天津工业大学", "成都理工大学", "西南石油大学",
    # 艺体政法
    "中央美术学院", "中国美术学院", "中央戏剧学院",
    "上海音乐学院", "中国音乐学院",
    "外交学院", "中国人民公安大学",
]

# 海外名校（仅中文名，匹配 BOSS 直聘显示）
OVERSEAS_ELITE_SCHOOLS = [
    # ── 美国 ──
    "哈佛大学", "麻省理工学院", "斯坦福大学", "加州大学伯克利分校",
    "加州理工学院", "普林斯顿大学", "耶鲁大学", "哥伦比亚大学",
    "宾夕法尼亚大学", "康奈尔大学", "芝加哥大学", "杜克大学",
    "西北大学", "约翰霍普金斯大学", "加州大学洛杉矶分校",
    "卡内基梅隆大学", "密歇根大学安娜堡分校", "纽约大学",
    "华盛顿大学", "佐治亚理工学院", "伊利诺伊大学香槟分校",
    "德克萨斯大学奥斯汀分校", "威斯康星大学麦迪逊分校",
    "布朗大学", "达特茅斯学院", "莱斯大学", "范德堡大学",
    "南加州大学", "北卡罗来纳大学教堂山分校",
    # ── 英国 ──
    "牛津大学", "剑桥大学", "帝国理工学院", "伦敦政治经济学院",
    "伦敦大学学院", "爱丁堡大学", "曼彻斯特大学",
    "伦敦国王学院", "布里斯托大学", "华威大学",
    "格拉斯哥大学", "伯明翰大学", "南安普顿大学",
    "利兹大学", "杜伦大学", "谢菲尔德大学", "圣安德鲁斯大学",
    "诺丁汉大学", "伦敦玛丽女王大学",
    # ── 瑞士 ──
    "苏黎世联邦理工学院", "洛桑联邦理工学院",
    # ── 加拿大 ──
    "多伦多大学", "英属哥伦比亚大学", "麦吉尔大学",
    "滑铁卢大学", "阿尔伯塔大学", "蒙特利尔大学",
    # ── 新加坡 ──
    "新加坡国立大学", "南洋理工大学",
    # ── 日本 ──
    "东京大学", "京都大学", "东京工业大学",
    "大阪大学", "东北大学（日本）", "名古屋大学",
    "早稻田大学", "庆应义塾大学",
    # ── 香港 ──
    "香港大学", "香港中文大学", "香港科技大学",
    "香港城市大学", "香港理工大学", "香港浸会大学",
    # ── 澳大利亚 ──
    "墨尔本大学", "澳大利亚国立大学", "悉尼大学",
    "新南威尔士大学", "昆士兰大学", "莫纳什大学",
    "西澳大学", "阿德莱德大学",
    # ── 欧洲其他 ──
    "阿姆斯特丹大学", "慕尼黑工业大学", "慕尼黑大学",
    "海德堡大学", "索邦大学", "巴黎文理研究大学",
    "鲁汶大学", "代尔夫特理工大学", "哥本哈根大学",
    "卡罗林斯卡学院", "苏黎世大学", "柏林洪堡大学",
    "柏林自由大学", "巴黎综合理工学院", "隆德大学",
    "奥斯陆大学", "赫尔辛基大学", "乌普萨拉大学",
    "都柏林圣三一学院", "维也纳大学", "日内瓦大学",
    # ── 韩国 ──
    "首尔国立大学", "韩国科学技术院", "延世大学",
    "高丽大学", "成均馆大学", "浦项科技大学",
    # ── 其他 ──
    "莫斯科国立大学", "圣彼得堡国立大学",
    "奥克兰大学", "特拉维夫大学",
]

# 合并全部名校
_HARDCODED_SCHOOLS = DOMESTIC_ELITE_SCHOOLS + OVERSEAS_ELITE_SCHOOLS
# 从 JSON 配置加载，文件不存在或无字段时回退到硬编码
ALL_ELITE_SCHOOLS = _filter_data.get("school_whitelist", _HARDCODED_SCHOOLS)


# ========== 可扩展筛选条件 ==========

@dataclass
class FilterCriteria:
    """可扩展的筛选条件

    当前支持的维度:
        - school_whitelist: 学校白名单
        - min_degree: 最低学历
        - min_years: 最低工作年限
    后续可扩展:
        - age_range: 年龄范围 (min, max)
        - tech_stack: 技术栈要求
        - industry: 行业经验
        - job_title_keywords: 职位关键词
    """
    school_whitelist: Optional[List[str]] = None
    min_degree: str = "本科"
    min_years: int = 3
    # ---- 预留扩展字段 ----
    age_range: Optional[Tuple[int, int]] = None       # (min_age, max_age)
    tech_stack: Optional[List[str]] = None              # ["Python", "React", ...]
    industry: Optional[List[str]] = None                # ["互联网", "金融", ...]
    job_title_keywords: Optional[List[str]] = None      # ["工程师", "产品经理", ...]
    exclude_keywords: Optional[List[str]] = None        # 排除关键词

    def get_active_filters(self) -> List[str]:
        """返回已激活的筛选维度名"""
        active = []
        if self.school_whitelist:
            active.append("school")
        if self.min_degree:
            active.append("degree")
        if self.min_years is not None:
            active.append("years")
        if self.age_range:
            active.append("age")
        if self.tech_stack:
            active.append("tech_stack")
        if self.industry:
            active.append("industry")
        if self.job_title_keywords:
            active.append("job_title")
        return active


# ========== 学校匹配 ==========

def match_school(candidate_school: str, whitelist: list) -> bool:
    """检查候选人的学校是否匹配白名单中的任一学校

    全中文匹配，完全相等（避免"电子科技大学"误匹配"桂林电子科技大学"）
    """
    if not candidate_school or not whitelist:
        return False

    school = candidate_school.strip()

    for white_school in whitelist:
        if school == white_school.strip():
            return True

    return False


# ══════════════════════════════════════════════════
# 统一筛选入口 — 所有脚本共用
# ══════════════════════════════════════════════════

def check_degree(degree: str, min_degree: str = "本科") -> bool:
    """学历是否达到最低要求（等级比较：硕士≥本科）"""
    return DEGREE_RANK.get(degree, 0) >= DEGREE_RANK.get(min_degree, 0)


def check_candidate(
    school: str,
    degree: str,
    school_whitelist: list = None,
    min_degree: str = "本科",
) -> tuple:
    """统一候选人筛选：学校白名单 + 学历等级

    返回: (passed, fail_reason)
    """
    if school_whitelist is None:
        school_whitelist = ALL_ELITE_SCHOOLS

    if not school or not match_school(school, school_whitelist):
        return False, f"学校不符 ({school or '未知'} 不在白名单)"

    if degree and not check_degree(degree, min_degree):
        return False, f"学历不达标 ({degree} < {min_degree})"

    return True, None
