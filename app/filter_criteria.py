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

# 海外名校 - 美国
US_ELITE_SCHOOLS = [
    "Harvard University", "Massachusetts Institute of Technology", "MIT",
    "Stanford University", "University of California, Berkeley", "UC Berkeley", "UCB",
    "California Institute of Technology", "Caltech",
    "Princeton University", "Yale University",
    "Columbia University", "University of Pennsylvania", "UPenn",
    "Cornell University", "University of Chicago", "UChicago",
    "Duke University", "Northwestern University",
    "Johns Hopkins University", "JHU",
    "University of California, Los Angeles", "UCLA",
    "Carnegie Mellon University", "CMU",
    "University of Michigan", "UMich",
    "New York University", "NYU",
    "University of Washington", "Georgia Institute of Technology", "Georgia Tech",
    "University of Illinois Urbana-Champaign", "UIUC",
    "University of Texas at Austin", "UT Austin",
    "University of Wisconsin-Madison", "Brown University",
    "Dartmouth College", "Rice University", "Vanderbilt University",
]

# 海外名校 - 英国
UK_ELITE_SCHOOLS = [
    "University of Oxford", "Oxford University", "Oxford",
    "University of Cambridge", "Cambridge University", "Cambridge",
    "Imperial College London", "Imperial College",
    "London School of Economics", "LSE",
    "University College London", "UCL",
    "University of Edinburgh", "University of Manchester",
    "King's College London", "University of Bristol",
    "University of Warwick",
]

# 海外名校 - 其他地区
OTHER_ELITE_SCHOOLS = [
    # 瑞士
    "ETH Zurich", "EPFL",
    # 加拿大
    "University of Toronto", "University of British Columbia", "UBC",
    "McGill University", "University of Waterloo",
    # 新加坡
    "National University of Singapore", "NUS",
    "Nanyang Technological University", "NTU",
    # 日本
    "University of Tokyo", "Tokyo University",
    "Kyoto University", "Tokyo Institute of Technology",
    # 香港
    "University of Hong Kong", "HKU",
    "Chinese University of Hong Kong", "CUHK",
    "Hong Kong University of Science and Technology", "HKUST",
    # 澳大利亚
    "University of Melbourne", "Australian National University", "ANU",
    "University of Sydney", "University of New South Wales", "UNSW",
    # 欧洲其他
    "University of Amsterdam", "Technical University of Munich",
    "LMU Munich", "Heidelberg University",
    "Sorbonne University", "PSL University",
    "KU Leuven", "Delft University of Technology",
    "University of Copenhagen", "Karolinska Institute",
    # 韩国
    "Seoul National University", "KAIST",
    "Yonsei University", "Korea University",
]

# 合并全部名校
_HARDCODED_SCHOOLS = DOMESTIC_ELITE_SCHOOLS + US_ELITE_SCHOOLS + UK_ELITE_SCHOOLS + OTHER_ELITE_SCHOOLS
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

    匹配规则:
        中文学校名: 完全相等（避免"电子科技大学"误匹配"桂林电子科技大学"）
        英文学校名: 支持缩写互推 + 包含匹配
        纯大写缩写: "MIT" <-> "Massachusetts Institute of Technology"
    """
    if not candidate_school or not whitelist:
        return False

    school = candidate_school.strip()
    school_lower = school.lower()
    is_chinese = bool(re.search(r'[一-龥]', school))

    for white_school in whitelist:
        white = white_school.strip()
        white_lower = white.lower()

        # 完全匹配
        if school_lower == white_lower:
            return True

        # 中文学校名: 只做完全匹配，不做包含匹配
        if is_chinese:
            continue

        # 英文: 包含匹配 + 缩写匹配
        if white_lower in school_lower or school_lower in white_lower:
            return True

        # 纯大写缩写匹配: "MIT" <-> "Massachusetts Institute of Technology"
        if white.isupper() and len(white) <= 7:
            words = school_lower.replace(',', '').split()
            if len(words) >= 2:
                abbr = ''.join(w[0].upper() for w in words if w[0].isalpha())
                if white.upper() == abbr:
                    return True

    return False
