# -*- coding: utf-8 -*-
"""
Muse (知行学枢) — 零算术幻觉确定性学业精算与决策引擎
严格依据河南师范大学《计算机/软件类本科培养方案(2024版)》《综合素质测评与推免实施细则(2026版)》
与《教育部榜单学科竞赛分级认定目录》，使用 Python Decimal 实现：
  1. 成绩单多格式智能解析（Excel / CSV / 文本多列）与课程模块自动归类；
  2. 4.0 分制 GPA、百分制加权平均分与 160 学分毕业红黄绿灯审计（含博约通识三子模块）；
  3. 「What-If」重修 vs 学科竞赛 vs 前沿认证 综测/推免 ROI 沙盘推演；
  4. 校园公开通知「千人千面」资格自动对账与缺口预警；
  5. 标准 RFC 5545 `.ics` 避课日程包与自包含交互式 HTML 体检报告生成。
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from html import escape
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
import uuid


# ---- 1. 权威培养方案基线（河南师范大学计算机/软件类 160 学分基线） ----
CURRICULUM_MODULES: List[Dict[str, Any]] = [
    {
        "key": "general_required",
        "label": "通识必修课",
        "short": "通识必修",
        "required": 30.0,
        "group": "general",
        "note": "思政、大学英语、大学体育、军训与国家安全教育等",
    },
    {
        "key": "boyue_classic",
        "label": "博约通识 · 经典类",
        "short": "博约经典",
        "required": 4.0,
        "group": "boyue",
        "note": "河师大博约通识子模块硬性下限 ≥ 4.0 学分（高频漏修项）",
    },
    {
        "key": "boyue_core",
        "label": "博约通识 · 核心类",
        "short": "博约核心",
        "required": 12.0,
        "group": "boyue",
        "note": "河师大博约通识子模块硬性下限 ≥ 12.0 学分",
    },
    {
        "key": "boyue_baihua",
        "label": "博约通识 · 百花类",
        "short": "博约百花",
        "required": 2.0,
        "group": "boyue",
        "note": "河师大博约通识子模块硬性下限 ≥ 2.0 学分（线上课须学习通绑定河师大）",
    },
    {
        "key": "major_required",
        "label": "学科基础与专业必修",
        "short": "专业必修",
        "required": 72.0,
        "group": "major",
        "note": "高数、离散、数据结构、计组、操作系统、计网、数据库、软工等核心课",
    },
    {
        "key": "major_elective",
        "label": "专业方向选修课",
        "short": "专业选修",
        "required": 22.0,
        "group": "major",
        "note": "人工智能大模型、云计算、分布式系统、网络安全、移动开发等方向课",
    },
    {
        "key": "practice",
        "label": "集中实践与毕业设计",
        "short": "集中实践",
        "required": 14.0,
        "group": "practice",
        "note": "综合课程设计、工程实训、专业实习与毕业论文（设计）",
    },
    {
        "key": "innovation",
        "label": "创新创业实践学分",
        "short": "创新创业",
        "required": 4.0,
        "group": "practice",
        "note": "可通过教育部榜单竞赛、大创结题、华为 HCIA/HCIP 认证、软著或论文认定",
    },
]

MODULE_MAP: Dict[str, Dict[str, Any]] = {m["key"]: m for m in CURRICULUM_MODULES}

CATEGORY_ALIASES: Dict[str, str] = {
    "通识必修": "general_required",
    "通识必修课": "general_required",
    "公共必修": "general_required",
    "general_required": "general_required",
    "博约经典": "boyue_classic",
    "博约通识经典": "boyue_classic",
    "经典类": "boyue_classic",
    "boyue_classic": "boyue_classic",
    "博约核心": "boyue_core",
    "博约通识核心": "boyue_core",
    "核心类": "boyue_core",
    "boyue_core": "boyue_core",
    "博约百花": "boyue_baihua",
    "博约通识百花": "boyue_baihua",
    "百花类": "boyue_baihua",
    "boyue_baihua": "boyue_baihua",
    "专业必修": "major_required",
    "学科必修": "major_required",
    "学科基础": "major_required",
    "必修": "major_required",
    "major_required": "major_required",
    "专业选修": "major_elective",
    "方向选修": "major_elective",
    "专选": "major_elective",
    "选修": "major_elective",
    "major_elective": "major_elective",
    "集中实践": "practice",
    "实践": "practice",
    "实训": "practice",
    "课程设计": "practice",
    "毕业设计": "practice",
    "practice": "practice",
    "创新创业": "innovation",
    "创新学分": "innovation",
    "创新实践": "innovation",
    "竞赛认定": "innovation",
    "innovation": "innovation",
}

# ---- 2. 综测/推免竞赛与认证加分目录（严格对应 02/03 知识库文档） ----
COMPETITION_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "huawei_ict_nat1",
        "name": "第十一届华为ICT大赛 · 全国总决赛一等奖",
        "level": "国家级 A 类榜单",
        "raw_bonus": 24.0,
        "zongce_gain": 3.60,
        "innovation_credits": 3.0,
        "est_hours": 95,
        "difficulty": "高",
        "citation": "《综测与推免实施细则(2026)》第二章第四条",
    },
    {
        "id": "huawei_ict_nat2",
        "name": "第十一届华为ICT大赛 · 全国总决赛二等奖",
        "level": "国家级 A 类榜单",
        "raw_bonus": 18.0,
        "zongce_gain": 2.70,
        "innovation_credits": 2.5,
        "est_hours": 80,
        "difficulty": "中高",
        "citation": "《综测与推免实施细则(2026)》第二章第四条",
    },
    {
        "id": "huawei_ict_prov1",
        "name": "第十一届华为ICT大赛 · 省级一等奖 / 晋级中国总决赛",
        "level": "省级 A 类榜单",
        "raw_bonus": 14.0,
        "zongce_gain": 2.10,
        "innovation_credits": 2.0,
        "est_hours": 55,
        "difficulty": "中",
        "citation": "《综测与推免实施细则(2026)》第二章第四条",
    },
    {
        "id": "huawei_ict_prov2",
        "name": "第十一届华为ICT大赛 · 省级二等奖",
        "level": "省级 A 类榜单",
        "raw_bonus": 10.0,
        "zongce_gain": 1.50,
        "innovation_credits": 1.5,
        "est_hours": 40,
        "difficulty": "中低",
        "citation": "《综测与推免实施细则(2026)》第二章第四条",
    },
    {
        "id": "huawei_hcip",
        "name": "华为云开发者 / HCIP 前沿技术高级认证",
        "level": "院级前沿认证激励",
        "raw_bonus": 8.0,
        "zongce_gain": 1.20,
        "innovation_credits": 1.5,
        "est_hours": 32,
        "difficulty": "中低",
        "citation": "《综测与推免实施细则(2026)》第二章第三/四条",
    },
    {
        "id": "prov_dachuang",
        "name": "省级大学生创新创业训练计划结题（主持）",
        "level": "省级科研实践",
        "raw_bonus": 8.0,
        "zongce_gain": 1.20,
        "innovation_credits": 2.0,
        "est_hours": 65,
        "difficulty": "中",
        "citation": "《综测与推免实施细则(2026)》第二章第四条",
    },
    {
        "id": "software_copyright",
        "name": "国家计算机软件著作权登记（第一作者）",
        "level": "知识产权成果",
        "raw_bonus": 6.0,
        "zongce_gain": 0.90,
        "innovation_credits": 1.0,
        "est_hours": 24,
        "difficulty": "低",
        "citation": "《综测与推免实施细则(2026)》第二章第四条",
    },
    {
        "id": "ccf_c_paper",
        "name": "CCF-C 及以上高水平学术论文（第一作者）",
        "level": "高水平学术成果",
        "raw_bonus": 20.0,
        "zongce_gain": 3.00,
        "innovation_credits": 3.0,
        "est_hours": 140,
        "difficulty": "极高",
        "citation": "《综测与推免实施细则(2026)》第二章第四条",
    },
]

COMPETITION_MAP: Dict[str, Dict[str, Any]] = {c["id"]: c for c in COMPETITION_CATALOG}

# 公开可编辑的匿名示范模板（不含任何虚构姓名）
SAMPLE_TRANSCRIPT_CSV = """课程名称,学分,成绩,课程模块
高等数学A(上),5.0,86,专业必修
高等数学A(下),5.0,83,专业必修
线性代数与概率统计,4.0,88,专业必修
离散数学,3.5,81,专业必修
程序设计基础(C/Python),4.0,92,专业必修
数据结构与算法,4.0,74,专业必修
计算机组成原理,4.0,79,专业必修
操作系统原理,3.5,85,专业必修
计算机网络,3.5,87,专业必修
数据库系统原理,3.5,90,专业必修
软件工程导论,3.0,88,专业必修
人工智能与大模型应用,3.0,94,专业选修
云计算与分布式系统,2.5,91,专业选修
Python数据分析与可视化,2.0,93,专业选修
大学英语(1-4),12.0,84,通识必修
思想政治理论课群,12.0,89,通识必修
大学体育与军训,6.0,86,通识必修
博约经典：中华哲学名著导读,2.0,91,博约经典
博约核心：科学精神与人工智能伦理,4.0,88,博约核心
博约核心：数字经济与现代社会,4.0,85,博约核心
综合软件工程课程实训,4.0,90,集中实践
校级程序设计竞赛认定,1.5,95,创新创业"""


def _dec(val: Any) -> Decimal:
    return Decimal(str(val))


def _q2(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q4(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def score_to_gpa_4(score: Decimal) -> Decimal:
    """严格依据《综测与推免实施细则(2026)》第二章第二条 4.0 分制绩点映射"""
    s = float(score)
    if s >= 90:
        return Decimal("4.0")
    if s >= 85:
        return Decimal("3.7")
    if s >= 82:
        return Decimal("3.3")
    if s >= 78:
        return Decimal("3.0")
    if s >= 75:
        return Decimal("2.7")
    if s >= 72:
        return Decimal("2.3")
    if s >= 68:
        return Decimal("2.0")
    if s >= 64:
        return Decimal("1.5")
    if s >= 60:
        return Decimal("1.0")
    return Decimal("0.0")


def infer_module_key(course_name: str, explicit_category: str = "") -> str:
    """根据显式标签或课程名称关键词自动归类培养方案模块"""
    raw_cat = re.sub(r"[\s·\-_/()（）]", "", explicit_category or "").lower()
    for alias, key in CATEGORY_ALIASES.items():
        clean_alias = re.sub(r"[\s·\-_/()（）]", "", alias).lower()
        if raw_cat and (raw_cat == clean_alias or clean_alias in raw_cat):
            return key

    name = (course_name or "").strip()
    if any(k in name for k in ("博约经典", "经典导读", "名著导读")):
        return "boyue_classic"
    if any(k in name for k in ("博约百花", "百花", "艺术鉴赏", "美育", "音乐欣赏")):
        return "boyue_baihua"
    if any(k in name for k in ("博约核心", "博约通识", "博约", "通识选修")):
        return "boyue_core"
    if any(k in name for k in ("创新", "创业", "竞赛", "大创", "软著", "认证", "HCIA", "HCIP")):
        return "innovation"
    if any(k in name for k in ("实训", "实习", "课程设计", "毕业设计", "毕业论文", "实践")):
        return "practice"
    if any(k in name for k in ("英语", "思政", "马原", "毛概", "近代史", "习概", "体育", "军训", "心理", "形势与政策")):
        return "general_required"
    if any(k in name for k in ("选修", "大模型", "云计算", "可视化", "前端", "移动开发", "安全", "深度学习", "机器学习")):
        return "major_elective"
    return "major_required"


def parse_courses_text(rows_text: str) -> List[Dict[str, Any]]:
    """
    智能解析多格式成绩单文本（支持 CSV / TSV / 空格分隔）：
      - 2列：学分, 成绩
      - 3列：课程名称, 学分, 成绩
      - 4列：课程名称, 学分, 成绩, 课程模块
    """
    raw_lines = [ln.strip() for ln in (rows_text or "").splitlines() if ln.strip()]
    if not raw_lines:
        raise ValueError("请输入至少 1 条课程记录，或点击「填入示范成绩单」快速体验。")
    if len(raw_lines) > 500:
        raise ValueError("单次最多支持解析 500 门课程记录。")

    # 自动跳过表头行
    first_tokens = [t.strip() for t in re.split(r"[,，\t]+", raw_lines[0]) if t.strip()]
    if any(h in "".join(first_tokens) for h in ("课程", "学分", "成绩", "绩点", "模块")):
        if not any(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", t) for t in first_tokens):
            raw_lines.pop(0)

    if not raw_lines:
        raise ValueError("表头下方未检测到有效课程数据行。")

    courses: List[Dict[str, Any]] = []
    num_re = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,4})?$")

    for idx, line in enumerate(raw_lines, start=1):
        if re.search(r"[,，\t]", line):
            parts = [p.strip() for p in re.split(r"[,，\t]", line)]
        else:
            parts = line.split()
        parts = [p for p in parts if p != ""]

        name = ""
        credit_str = ""
        score_str = ""
        cat_str = ""

        if len(parts) == 2:
            name = f"课程 #{idx}"
            credit_str, score_str = parts[0], parts[1]
        elif len(parts) == 3:
            if num_re.match(parts[0]) and num_re.match(parts[1]):
                credit_str, score_str, cat_str = parts[0], parts[1], parts[2]
                name = f"课程 #{idx}"
            else:
                name, credit_str, score_str = parts[0], parts[1], parts[2]
        elif len(parts) >= 4:
            name, credit_str, score_str, cat_str = parts[0], parts[1], parts[2], parts[3]
        else:
            raise ValueError(f"第 {idx} 行格式无法识别（需至少包含学分与成绩两列）：{line[:40]}")

        if not num_re.match(credit_str) or not num_re.match(score_str):
            raise ValueError(f"第 {idx} 行「{name}」的学分或成绩不是合法数字（学分={credit_str}, 成绩={score_str}）。")

        credits = _dec(credit_str)
        score = _dec(score_str)
        if not Decimal(0) < credits <= Decimal(100):
            raise ValueError(f"第 {idx} 行「{name}」学分须在 (0, 100] 区间内。")
        if not Decimal(0) <= score <= Decimal(100):
            raise ValueError(f"第 {idx} 行「{name}」成绩须在 [0, 100] 区间内。")

        mod_key = infer_module_key(name, cat_str)
        gpa_pt = score_to_gpa_4(score)
        passed = score >= Decimal(60)
        courses.append({
            "index": idx,
            "name": name[:64],
            "credits": float(credits),
            "score": float(score),
            "gpa": float(gpa_pt),
            "module": mod_key,
            "module_label": MODULE_MAP[mod_key]["short"],
            "passed": passed,
        })

    return courses


def audit_academic_profile(
    courses: List[Dict[str, Any]],
    grade: int = 2024,
    cet4: int = 520,
    cet6: int = 440,
    volunteer_hours: float = 24.0,
    moral_score: float = 88.0,
    innovation_raw: float = 6.0,
    target_direction: str = "保研",
) -> Dict[str, Any]:
    """
    确定性计算：
      - 百分制加权平均分、4.0 GPA、已修/挂科学分
      - 160 学分与博约通识三子模块红黄绿灯审计
      - 当前综测总分及距离推免参考安全线（86.80 分）差值
      - 识别最值得重修刷分的低分高学分候选课程
    """
    if not courses:
        raise ValueError("课程列表为空，无法执行审计。")

    total_credits = Decimal(0)
    passed_credits = Decimal(0)
    weighted_score_sum = Decimal(0)
    weighted_gpa_sum = Decimal(0)
    fail_courses: List[Dict[str, Any]] = []
    module_earned: Dict[str, Decimal] = {m["key"]: Decimal(0) for m in CURRICULUM_MODULES}

    for c in courses:
        cr = _dec(c["credits"])
        sc = _dec(c["score"])
        gp = score_to_gpa_4(sc)
        total_credits += cr
        weighted_score_sum += cr * sc
        weighted_gpa_sum += cr * gp
        if sc >= Decimal(60):
            passed_credits += cr
            mod_k = c.get("module") if c.get("module") in module_earned else "major_required"
            module_earned[mod_k] += cr
        else:
            fail_courses.append(c)

    weighted_avg = _q2(weighted_score_sum / total_credits)
    gpa_4 = _q4(weighted_gpa_sum / total_credits)
    gpa_4_2dp = _q2(weighted_gpa_sum / total_credits)

    # 模块审计
    modules_report: List[Dict[str, Any]] = []
    alerts: List[Dict[str, str]] = []
    total_required = Decimal("160.0")

    for mod in CURRICULUM_MODULES:
        req = _dec(mod["required"])
        earned = module_earned[mod["key"]]
        gap = max(Decimal(0), req - earned)
        pct = min(100.0, float((earned / req) * Decimal(100))) if req > 0 else 100.0
        if gap == 0:
            status = "ok"
        elif mod["group"] == "boyue" and earned == 0:
            status = "danger"
        elif pct < 50:
            status = "warn"
        else:
            status = "progress"

        modules_report.append({
            "key": mod["key"],
            "label": mod["label"],
            "short": mod["short"],
            "group": mod["group"],
            "required": float(req),
            "earned": float(earned),
            "gap": float(gap),
            "percent": round(pct, 1),
            "status": status,
            "note": mod["note"],
        })

        if mod["group"] == "boyue" and gap > 0:
            alerts.append({
                "level": "danger" if earned == 0 else "warn",
                "title": f"{mod['label']}尚缺 {float(gap):.1f} 学分",
                "detail": f"当前已修 {float(earned):.1f} / 要求 {float(req):.1f} 学分。{mod['note']}。",
                "citation": "《本科培养方案(2024版)》第二章第三条",
            })

    boyue_earned = module_earned["boyue_classic"] + module_earned["boyue_core"] + module_earned["boyue_baihua"]
    boyue_gap = max(Decimal(0), Decimal("18.0") - boyue_earned)

    if module_earned["innovation"] < Decimal("4.0"):
        inno_gap = float(Decimal("4.0") - module_earned["innovation"])
        alerts.append({
            "level": "warn",
            "title": f"创新创业学分尚缺 {inno_gap:.1f} 学分",
            "detail": "可通过第十一届华为ICT大赛获奖（认定 1.5–3.0 学分）、华为 HCIP 认证或大创结题快速补齐。",
            "citation": "《本科培养方案(2024版)》第三章第六条",
        })

    if fail_courses:
        alerts.insert(0, {
            "level": "danger",
            "title": f"检测到 {len(fail_courses)} 门不及格课程",
            "detail": "不及格记录将直接影响辅修报名与推免/奖学金一票否决条款，需优先安排补考或重修。",
            "citation": "《本科培养方案(2024版)》第一章第二条",
        })

    if cet4 < 425:
        alerts.append({
            "level": "danger",
            "title": f"大学英语四级（{cet4}）未达推免 425 分红线",
            "detail": "非外语专业推免与多数院级奖学金均要求 CET-4 ≥ 425 分，建议下一次考期优先冲刺过线。",
            "citation": "《综测与推免实施细则(2026)》第三章第五条",
        })
    elif target_direction == "保研" and cet6 < 450:
        alerts.append({
            "level": "info",
            "title": f"建议冲刺六级 ≥ 450 分（当前 {cet6}）",
            "detail": "高竞争院校推免夏令营与复试普遍看重 CET-6 成绩。",
            "citation": "《综测与推免实施细则(2026)》第三章第五条",
        })

    # 综测总分确定性核算：S = G*0.75 + M*0.10 + min(15, I*0.15)
    moral_dec = min(Decimal("100"), max(Decimal("0"), _dec(moral_score)))
    inno_raw_dec = max(Decimal("0"), _dec(innovation_raw))
    inno_contrib = min(Decimal("15.00"), _q2(inno_raw_dec * Decimal("0.15")))
    zongce_total = _q2(weighted_avg * Decimal("0.75") + moral_dec * Decimal("0.10") + inno_contrib)
    tuijian_line = Decimal("86.80")
    gap_to_tuijian = _q2(tuijian_line - zongce_total)

    # 辅修与推免资格自查
    minor_eligible = (gpa_4 >= Decimal("2.50")) and (len(fail_courses) == 0)
    tuijian_ready = (len(fail_courses) == 0) and (cet4 >= 425) and (zongce_total >= tuijian_line)

    # 筛选最值得重修刷分的候选课程（高学分单科、低分；优先 2.0–6.0 学分单学期核心课）
    retake_candidates = []
    for c in courses:
        if c["score"] < 85 and 2.0 <= c["credits"] <= 6.0:
            # 模拟重修至 92 分在推免 0.3/0.7 折算规则下的实际有效分提升
            eff_new = _dec(c["score"]) * Decimal("0.3") + Decimal("92") * Decimal("0.7")
            delta_weighted = _q4((eff_new - _dec(c["score"])) * _dec(c["credits"]) / total_credits)
            delta_zongce = _q2(delta_weighted * Decimal("0.75"))
            retake_candidates.append({
                "name": c["name"],
                "credits": c["credits"],
                "score": c["score"],
                "eff_after_92": float(_q2(eff_new)),
                "zongce_gain_at_92": float(delta_zongce),
            })
    retake_candidates.sort(key=lambda x: x["zongce_gain_at_92"], reverse=True)

    return {
        "summary": {
            "course_count": len(courses),
            "total_credits": float(total_credits),
            "passed_credits": float(passed_credits),
            "required_total_credits": float(total_required),
            "graduation_pct": round(min(100.0, float((passed_credits / total_required) * Decimal(100))), 1),
            "weighted_avg": float(weighted_avg),
            "gpa_4": float(gpa_4_2dp),
            "gpa_4_precise": float(gpa_4),
            "fail_count": len(fail_courses),
            "boyue_earned": float(boyue_earned),
            "boyue_required": 18.0,
            "boyue_gap": float(boyue_gap),
            "moral_score": float(moral_dec),
            "innovation_raw": float(inno_raw_dec),
            "innovation_contrib": float(inno_contrib),
            "zongce_total": float(zongce_total),
            "tuijian_safe_line": float(tuijian_line),
            "gap_to_tuijian": float(gap_to_tuijian),
            "minor_eligible": minor_eligible,
            "tuijian_ready": tuijian_ready,
            "grade": grade,
            "cet4": cet4,
            "cet6": cet6,
            "volunteer_hours": volunteer_hours,
            "target_direction": target_direction,
        },
        "modules": modules_report,
        "alerts": alerts,
        "retake_candidates": retake_candidates[:5],
        "courses": courses,
    }


def simulate_what_if_scenarios(
    current_weighted_avg: float,
    total_credits: float,
    moral_score: float = 88.0,
    current_innovation_raw: float = 6.0,
    retake_course_name: str = "数据结构与算法",
    retake_credits: float = 4.0,
    retake_old_score: float = 74.0,
    retake_target_score: float = 92.0,
    retake_est_hours: float = 60.0,
    selected_competition_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    确定性「What-If」学业沙盘推演引擎：
      严格依据《综测与推免实施细则(2026版)》：
      - 综测公式：S = G*0.75 + M*0.10 + min(15, I*0.15)
      - 推免重修折算：有效新成绩 = 原成绩*0.3 + 重修成绩*0.7
      - 量化对比「重修刷分」与「各类榜单竞赛/前沿认证/科研软著」的净收益与每 10 小时 ROI
    """
    tc = max(Decimal("1.0"), _dec(total_credits))
    g0 = _dec(current_weighted_avg)
    m0 = min(Decimal("100"), max(Decimal("0"), _dec(moral_score)))
    i0 = max(Decimal("0"), _dec(current_innovation_raw))
    tuijian_line = Decimal("86.80")

    base_inno_contrib = min(Decimal("15.00"), _q2(i0 * Decimal("0.15")))
    base_zongce = _q2(g0 * Decimal("0.75") + m0 * Decimal("0.10") + base_inno_contrib)

    scenarios: List[Dict[str, Any]] = []

    # 1. 重修方案推演
    rc = max(Decimal("0.5"), _dec(retake_credits))
    old_s = _dec(retake_old_score)
    tgt_s = max(old_s, min(Decimal("100"), _dec(retake_target_score)))
    eff_score = _q2(old_s * Decimal("0.3") + tgt_s * Decimal("0.7"))
    delta_g = (eff_score - old_s) * rc / tc
    new_g = _q2(g0 + delta_g)
    new_z_retake = _q2(new_g * Decimal("0.75") + m0 * Decimal("0.10") + base_inno_contrib)
    gain_retake = _q2(new_z_retake - base_zongce)
    hrs_retake = max(Decimal("1"), _dec(retake_est_hours))
    roi_retake = _q2((gain_retake / hrs_retake) * Decimal("10"))

    scenarios.append({
        "id": "retake_course",
        "kind": "retake",
        "title": f"重修《{retake_course_name}》({float(rc):.1f}学分: {float(old_s):.0f}->{float(tgt_s):.0f})",
        "detail": f"按推免折算（0.3×初修 + 0.7×重修 = {float(eff_score):.2f}分），加权均分 {float(g0):.2f} -> {float(new_g):.2f}",
        "new_zongce": float(new_z_retake),
        "zongce_gain": float(gain_retake),
        "est_hours": float(hrs_retake),
        "roi_per_10h": float(roi_retake),
        "innovation_credits_gain": 0.0,
        "reaches_tuijian": new_z_retake >= tuijian_line,
        "citation": "《综测与推免实施细则(2026)》第三章第五条",
    })

    # 2. 学科竞赛 / 前沿认证方案推演
    pick_ids = selected_competition_ids or ["huawei_ict_prov1", "huawei_ict_nat1", "huawei_hcip", "software_copyright"]
    for cid in pick_ids:
        item = COMPETITION_MAP.get(cid)
        if not item:
            continue
        new_i = i0 + _dec(item["raw_bonus"])
        new_inno_contrib = min(Decimal("15.00"), _q2(new_i * Decimal("0.15")))
        new_z = _q2(g0 * Decimal("0.75") + m0 * Decimal("0.10") + new_inno_contrib)
        gain = _q2(new_z - base_zongce)
        hrs = max(Decimal("1"), _dec(item["est_hours"]))
        roi = _q2((gain / hrs) * Decimal("10"))
        capped_note = "（已触及创新素质分 15.00 分上限）" if new_inno_contrib == Decimal("15.00") else ""
        scenarios.append({
            "id": cid,
            "kind": "competition",
            "title": item["name"],
            "detail": f"原始素质加分 +{item['raw_bonus']:.0f} 分（折合综测 +{float(gain):.2f} 分）· 认定 {item['innovation_credits']:.1f} 创新学分{capped_note}",
            "new_zongce": float(new_z),
            "zongce_gain": float(gain),
            "est_hours": float(hrs),
            "roi_per_10h": float(roi),
            "innovation_credits_gain": item["innovation_credits"],
            "reaches_tuijian": new_z >= tuijian_line,
            "citation": item["citation"],
        })

    # 3. 组合拳推荐（最高 ROI 竞赛 + 认证 / 重修）
    comp_only = [s for s in scenarios if s["kind"] == "competition"]
    best_roi = max(scenarios, key=lambda x: (x["roi_per_10h"], x["zongce_gain"]))
    best_gain = max(scenarios, key=lambda x: (x["zongce_gain"], x["roi_per_10h"]))

    # 生成组合方案：最佳单项竞赛 + 课程重修
    combo_comp = max(comp_only, key=lambda x: x["roi_per_10h"]) if comp_only else None
    if combo_comp:
        c_meta = COMPETITION_MAP[combo_comp["id"]]
        combo_inno = min(Decimal("15.00"), _q2((i0 + _dec(c_meta["raw_bonus"])) * Decimal("0.15")))
        combo_z = _q2(new_g * Decimal("0.75") + m0 * Decimal("0.10") + combo_inno)
        combo_gain = _q2(combo_z - base_zongce)
        combo_hrs = hrs_retake + _dec(c_meta["est_hours"])
        combo_roi = _q2((combo_gain / combo_hrs) * Decimal("10"))
        scenarios.append({
            "id": "combo_optimal",
            "kind": "combo",
            "title": f"组合策略：{combo_comp['title'].split('·')[-1].strip()} + 重修提分",
            "detail": f"双线推进：加权均分升至 {float(new_g):.2f} 且叠加 {c_meta['innovation_credits']:.1f} 创新学分",
            "new_zongce": float(combo_z),
            "zongce_gain": float(combo_gain),
            "est_hours": float(combo_hrs),
            "roi_per_10h": float(combo_roi),
            "innovation_credits_gain": c_meta["innovation_credits"],
            "reaches_tuijian": combo_z >= tuijian_line,
            "citation": "《综测与推免实施细则(2026)》第一/二/三章",
        })

    scenarios_sorted = sorted(scenarios, key=lambda x: (x["roi_per_10h"], x["zongce_gain"]), reverse=True)
    return {
        "baseline": {
            "weighted_avg": float(g0),
            "total_credits": float(tc),
            "moral_score": float(m0),
            "innovation_raw": float(i0),
            "innovation_contrib": float(base_inno_contrib),
            "zongce_total": float(base_zongce),
            "tuijian_safe_line": float(tuijian_line),
            "gap_to_tuijian": float(_q2(tuijian_line - base_zongce)),
        },
        "scenarios": scenarios_sorted,
        "best_roi_id": best_roi["id"],
        "best_gain_id": best_gain["id"],
        "recommendation": (
            f"精力 ROI 最优路径为「{best_roi['title']}」（每投入 10 小时预计提升综测 +{best_roi['roi_per_10h']:.2f} 分，"
            f"综测总分升至 {best_roi['new_zongce']:.2f} 分）；总分增量最高路径为「{best_gain['title']}」（+{best_gain['zongce_gain']:.2f} 分）。"
        ),
    }


def match_opportunities(
    notices_path: str,
    grade: int = 2024,
    gpa_4: float = 3.55,
    cet4: int = 520,
    cet6: int = 440,
    volunteer_hours: float = 24.0,
    fail_count: int = 0,
    reference_date: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    校园机会雷达「千人千面」资格确定性对账引擎：
    自动比对每条校园/竞赛通知的硬性门槛，输出：
      - eligible (100% 达标可直接申报)
      - near (仅差 1 项条件即可达标，提前补齐预警)
      - locked (差 2 项及以上或不满足年级条件)
    """
    if not os.path.isfile(notices_path):
        return {"counts": {"eligible": 0, "near": 0, "locked": 0}, "items": []}

    raw_list = json.load(open(notices_path, encoding="utf-8"))
    now_dt = reference_date or datetime.now()
    matched: List[Dict[str, Any]] = []
    counts = {"eligible": 0, "near": 0, "locked": 0}

    for n in raw_list:
        rules = n.get("rules") or {}
        gaps: List[str] = []
        passed_checks: List[str] = []

        allowed_grades = rules.get("allowed_grades") or []
        if allowed_grades and grade not in allowed_grades:
            gaps.append(f"年级要求 {allowed_grades}（当前 {grade} 级）")
        elif allowed_grades:
            passed_checks.append(f"年级符合 ({grade}级)")

        min_gpa = float(rules.get("min_gpa") or 0.0)
        if min_gpa > 0:
            if gpa_4 < min_gpa:
                gaps.append(f"GPA 差 {min_gpa - gpa_4:.2f}（需 ≥ {min_gpa:.2f}，当前 {gpa_4:.2f}）")
            else:
                passed_checks.append(f"GPA 达标 ({gpa_4:.2f} ≥ {min_gpa:.2f})")

        min_cet4 = int(rules.get("min_cet4") or 0)
        if min_cet4 > 0:
            if cet4 < min_cet4:
                gaps.append(f"四级差 {min_cet4 - cet4} 分（需 ≥ {min_cet4}）")
            else:
                passed_checks.append(f"四级达标 ({cet4})")

        min_cet6 = int(rules.get("min_cet6") or 0)
        if min_cet6 > 0:
            if cet6 < min_cet6:
                gaps.append(f"六级差 {min_cet6 - cet6} 分（需 ≥ {min_cet6}）")
            else:
                passed_checks.append(f"六级达标 ({cet6})")

        min_vol = float(rules.get("min_volunteer_hours") or 0.0)
        if min_vol > 0:
            if volunteer_hours < min_vol:
                gaps.append(f"志愿时长差 {min_vol - volunteer_hours:.1f}h（需 ≥ {min_vol:.0f}h）")
            else:
                passed_checks.append(f"志愿时长达标 ({volunteer_hours:.0f}h)")

        if rules.get("no_fail_required") and fail_count > 0:
            gaps.append(f"要求无不及格记录（当前 {fail_count} 门）")
        elif rules.get("no_fail_required"):
            passed_checks.append("无挂科记录")

        # 截止倒计时计算
        dl_str = str(n.get("deadline", "")).strip()
        days_left: Optional[int] = None
        if dl_str:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(dl_str, fmt)
                    days_left = (dt.date() - now_dt.date()).days
                    break
                except ValueError:
                    continue

        if not gaps:
            tier = "eligible"
            tier_label = "100% 达标可申报"
        elif len(gaps) == 1 and not any("年级要求" in g for g in gaps):
            tier = "near"
            tier_label = "差 1 项条件预警"
        else:
            tier = "locked"
            tier_label = "暂未满足门槛"

        counts[tier] += 1
        matched.append({
            "notice_id": n.get("notice_id", ""),
            "title": n.get("title", ""),
            "source_dept": n.get("source_dept", ""),
            "category": n.get("category", ""),
            "source_url": n.get("source_url", ""),
            "publish_date": n.get("publish_date", ""),
            "deadline": dl_str,
            "days_left": days_left,
            "benefit_tag": n.get("benefit_tag", ""),
            "tier": tier,
            "tier_label": tier_label,
            "passed_checks": passed_checks,
            "gaps": gaps,
            "required_materials": n.get("required_materials") or [],
            "prep_tasks": n.get("prep_tasks") or [],
        })

    order = {"eligible": 0, "near": 1, "locked": 2}
    matched.sort(key=lambda x: (order[x["tier"]], x["days_left"] if x["days_left"] is not None else 999))
    return {"counts": counts, "items": matched}


def _ics_escape(val: str) -> str:
    return (
        str(val or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def build_ics_calendar(
    title: str,
    events: List[Dict[str, Any]],
    reminder_minutes: int = 30,
) -> str:
    """生成严格符合 RFC 5545 规范的 .ics 日历文件（含 VALARM 提醒与 CRLF 换行）"""
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Muse Zhixing Academic Agent//HTU AgentArts//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(title)}",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for idx, ev in enumerate(events, start=1):
        uid = f"muse-{uuid.uuid4().hex[:10]}-{idx}@zhixing.agent"
        summary = _ics_escape(ev.get("summary") or f"学业计划 #{idx}")
        desc = _ics_escape(ev.get("description") or "")
        loc = _ics_escape(ev.get("location") or "河南师范大学 · 知行学枢排期")
        dtstart = str(ev.get("dtstart") or "")
        dtend = str(ev.get("dtend") or "")
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID=Asia/Shanghai:{dtstart}",
            f"DTEND;TZID=Asia/Shanghai:{dtend}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            f"LOCATION:{loc}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"TRIGGER:-PT{max(5, int(reminder_minutes))}M",
            f"DESCRIPTION:{summary}",
            "END:VALARM",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def generate_opportunity_ics(
    notice_title: str,
    deadline_str: str,
    prep_tasks: List[Dict[str, Any]],
    required_materials: List[str],
    free_hour_start: int = 19,
) -> Tuple[str, List[Dict[str, Any]]]:
    """根据截止日期与备赛任务自动避开白天满课时段，排入晚间空闲块并构建标准 .ics"""
    dl_dt = datetime.now() + timedelta(days=7)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dl_dt = datetime.strptime((deadline_str or "").strip(), fmt)
            break
        except ValueError:
            continue

    now_dt = datetime.now()
    events: List[Dict[str, Any]] = []
    materials_txt = "；".join(required_materials) if required_materials else "按通知原文要求准备申报材料"

    if not prep_tasks:
        prep_tasks = [
            {"step": f"核对材料清单与资格门槛：{notice_title[:28]}", "est_hours": 1.5, "days_before_deadline": 3},
            {"step": f"定稿并提交申报材料：{notice_title[:28]}", "est_hours": 1.5, "days_before_deadline": 1},
        ]

    for task in prep_tasks:
        days_before = int(task.get("days_before_deadline") or 2)
        est_h = float(task.get("est_hours") or 1.5)
        target_day = dl_dt.date() - timedelta(days=days_before)
        if target_day < now_dt.date():
            target_day = now_dt.date() + timedelta(days=1)
        start_dt = datetime(target_day.year, target_day.month, target_day.day, free_hour_start, 30)
        end_dt = start_dt + timedelta(minutes=int(est_h * 60))
        events.append({
            "summary": f"[知行排期] {task.get('step', '阶段推进')}",
            "dtstart": start_dt.strftime("%Y%m%dT%H%M%S"),
            "dtend": end_dt.strftime("%Y%m%dT%H%M%S"),
            "description": f"关联事项：{notice_title}\n材料清单：{materials_txt}",
        })

    # 添加截止提醒事件
    dl_start = dl_dt - timedelta(hours=1)
    events.append({
        "summary": f"[截止红线] {notice_title[:45]}",
        "dtstart": dl_start.strftime("%Y%m%dT%H%M%S"),
        "dtend": dl_dt.strftime("%Y%m%dT%H%M%S"),
        "description": f"最终提交截止时间：{deadline_str}\n请核验全部材料：{materials_txt}",
    })

    ics_content = build_ics_calendar(f"知行学枢 · {notice_title[:24]}", events)
    return ics_content, events


def render_academic_report_html(audit: Dict[str, Any], what_if: Optional[Dict[str, Any]] = None) -> str:
    """生成自包含、高颜值、可在右侧 Artifact Canvas 沙箱直接渲染的交互式 HTML 学业体检看板"""
    s = audit["summary"]
    modules = audit["modules"]
    alerts = audit["alerts"]

    # SVG 环形图参数
    pct = s["graduation_pct"]
    circumference = 2 * 3.14159 * 46
    dash_offset = circumference * (1 - pct / 100.0)

    mod_rows = []
    for m in modules:
        color = "#15803D" if m["status"] == "ok" else ("#DC2626" if m["status"] == "danger" else "#D97706")
        badge = "已修满" if m["status"] == "ok" else f"缺 {m['gap']:.1f} 学分"
        mod_rows.append(
            f'<div class="mod-row">'
            f'<div class="mod-top"><span class="mod-name">{escape(m["label"])}</span>'
            f'<span class="mod-meta">{m["earned"]:.1f} / {m["required"]:.1f} · <strong style="color:{color}">{badge}</strong></span></div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{m["percent"]}%;background:{color};"></div></div>'
            f'</div>'
        )

    alert_cards = []
    for a in alerts:
        bg = "#FEF2F2" if a["level"] == "danger" else ("#FFFBEB" if a["level"] == "warn" else "#ECFDF5")
        bd = "#FECACA" if a["level"] == "danger" else ("#FDE68A" if a["level"] == "warn" else "#A7F3D0")
        fg = "#991B1B" if a["level"] == "danger" else ("#92400E" if a["level"] == "warn" else "#065F46")
        alert_cards.append(
            f'<div class="alert-box" style="background:{bg};border-color:{bd};color:{fg};">'
            f'<div class="alert-title">{escape(a["title"])}</div>'
            f'<div class="alert-desc">{escape(a["detail"])}</div>'
            f'<div class="alert-cite">依据：{escape(a["citation"])}</div>'
            f'</div>'
        )
    if not alert_cards:
        alert_cards.append(
            '<div class="alert-box" style="background:#ECFDF5;border-color:#A7F3D0;color:#065F46;">'
            '<div class="alert-title">核心学分与资格门槛健康</div>'
            '<div class="alert-desc">当前未检测到挂科或博约通识零修读红灯风险，请保持稳定冲刺。</div>'
            '</div>'
        )

    what_if_html = ""
    if what_if and what_if.get("scenarios"):
        sc_rows = []
        max_gain = max((sc["zongce_gain"] for sc in what_if["scenarios"]), default=1.0) or 1.0
        for sc in what_if["scenarios"][:5]:
            w = min(100, int((sc["zongce_gain"] / max_gain) * 100))
            sc_rows.append(
                f'<div class="sc-row">'
                f'<div class="mod-top"><span class="mod-name">{escape(sc["title"])}</span>'
                f'<span class="mod-meta">综测 +{sc["zongce_gain"]:.2f} · 10h ROI {sc["roi_per_10h"]:.2f}</span></div>'
                f'<div class="bar-track"><div class="bar-fill" style="width:{w}%;background:#17594A;"></div></div>'
                f'</div>'
            )
        what_if_html = (
            '<section class="card">'
            '<h2>What-If 综测与推免 ROI 沙盘对比</h2>'
            f'<p class="sub">{escape(what_if.get("recommendation", ""))}</p>'
            + "".join(sc_rows)
            + '</section>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>知行学枢 · 学业确定性体检报告</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Inter", sans-serif;
    background: #F6F5F0; color: #13221D; padding: 20px; line-height: 1.55;
  }}
  .wrap {{ max-width: 760px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }}
  .hero {{
    background: linear-gradient(135deg, #0E342A 0%, #1B5443 100%);
    color: #F2F8F5; border-radius: 16px; padding: 22px 24px;
    display: flex; align-items: center; justify-content: space-between; gap: 18px;
  }}
  .hero h1 {{ font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }}
  .hero p {{ font-size: 12.5px; color: #CBE3D7; margin-top: 4px; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }}
  .kpi {{
    background: #FFFFFF; border: 1px solid #E2E6DF; border-radius: 12px; padding: 13px 14px;
  }}
  .kpi-label {{ font-size: 11.5px; color: #5C6B63; }}
  .kpi-val {{ font-size: 22px; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; color: #0F2F26; }}
  .kpi-sub {{ font-size: 11px; color: #6E7D75; margin-top: 2px; }}
  .card {{ background: #FFFFFF; border: 1px solid #E2E6DF; border-radius: 14px; padding: 18px; }}
  .card h2 {{ font-size: 15px; font-weight: 650; margin-bottom: 10px; color: #0F2F26; }}
  .sub {{ font-size: 12.5px; color: #52635B; margin-bottom: 12px; }}
  .mod-row, .sc-row {{ margin-bottom: 11px; }}
  .mod-top {{ display: flex; justify-content: space-between; font-size: 12.5px; margin-bottom: 5px; gap: 8px; }}
  .mod-name {{ font-weight: 600; }}
  .mod-meta {{ color: #5A6B63; font-variant-numeric: tabular-nums; }}
  .bar-track {{ height: 7px; border-radius: 99px; background: #ECEFE9; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 99px; transition: width 0.4s ease; }}
  .alert-box {{ border: 1px solid; border-radius: 10px; padding: 11px 13px; margin-bottom: 9px; }}
  .alert-title {{ font-size: 13px; font-weight: 650; }}
  .alert-desc {{ font-size: 12px; margin-top: 3px; }}
  .alert-cite {{ font-size: 11px; opacity: 0.78; margin-top: 4px; }}
  .foot {{ font-size: 11.5px; color: #6C7A73; text-align: center; padding: 4px 0 8px; }}
  @media (max-width: 600px) {{ .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }} .hero {{ flex-direction: column; align-items: flex-start; }} }}
</style>
</head>
<body>
<div class="wrap">
  <section class="hero">
    <div>
      <h1>知行学枢 · 学业确定性体检报告</h1>
      <p>目标方向：{escape(s["target_direction"])} · {s["grade"]}级 · 基于 Python Decimal 确定性规则引擎核算（零算术幻觉）</p>
    </div>
    <svg width="108" height="108" viewBox="0 0 112 112" aria-label="毕业学分进度">
      <circle cx="56" cy="56" r="46" fill="none" stroke="rgba(255,255,255,0.16)" stroke-width="9"/>
      <circle cx="56" cy="56" r="46" fill="none" stroke="#8CE4B8" stroke-width="9" stroke-linecap="round"
        stroke-dasharray="{circumference:.2f}" stroke-dashoffset="{dash_offset:.2f}" transform="rotate(-90 56 56)"/>
      <text x="56" y="53" text-anchor="middle" fill="#FFFFFF" font-size="18" font-weight="700">{pct:.1f}%</text>
      <text x="56" y="70" text-anchor="middle" fill="#CBE3D7" font-size="10">毕业总学分</text>
    </svg>
  </section>

  <section class="kpi-grid">
    <div class="kpi"><div class="kpi-label">4.0 制加权 GPA</div><div class="kpi-val">{s["gpa_4"]:.2f}</div><div class="kpi-sub">精确 {s["gpa_4_precise"]:.4f}</div></div>
    <div class="kpi"><div class="kpi-label">百分制加权均分</div><div class="kpi-val">{s["weighted_avg"]:.2f}</div><div class="kpi-sub">{s["course_count"]} 门课 · {s["passed_credits"]:.1f} 学分</div></div>
    <div class="kpi"><div class="kpi-label">当前综测预估</div><div class="kpi-val">{s["zongce_total"]:.2f}</div><div class="kpi-sub">距推免线 {s["tuijian_safe_line"]:.2f} 差 {max(0.0, s["gap_to_tuijian"]):.2f}</div></div>
    <div class="kpi"><div class="kpi-label">博约通识学分</div><div class="kpi-val">{s["boyue_earned"]:.1f}/18</div><div class="kpi-sub">尚缺 {s["boyue_gap"]:.1f} 学分</div></div>
  </section>

  <section class="card">
    <h2>培养方案分模块进度（160 学分基线）</h2>
    {"".join(mod_rows)}
  </section>

  <section class="card">
    <h2>隐形风险与资格红黄绿灯预警</h2>
    {"".join(alert_cards)}
  </section>

  {what_if_html}

  <div class="foot">本报告由知行学枢确定性精算引擎生成，供选课与申报前自查参考，最终认定以学院教务办与学工办公示为准。</div>
</div>
</body>
</html>"""


def get_academic_catalog() -> Dict[str, Any]:
    return {
        "curriculum_modules": CURRICULUM_MODULES,
        "competitions": COMPETITION_CATALOG,
        "sample_transcript_csv": SAMPLE_TRANSCRIPT_CSV,
        "formula": "S_total = G_academic * 0.75 + M_moral * 0.10 + min(15, I_innovation * 0.15)",
        "retake_rule": "推免重修折算：有效成绩 = 初修成绩 * 0.30 + 重修成绩 * 0.70",
        "tuijian_safe_line": 86.80,
        "agentarts_topology": {
            "controller": {
                "name": "中央控制器 (Planner & Router)",
                "role": "多轮上下文路由、复杂学业咨询拆解与子智能体协同调度",
            },
            "agents": [
                {
                    "id": "policy",
                    "name": "校情与政策溯源 Agent",
                    "tools": ["search_knowledge (FTS5/Rerank)", "web_search", "web_fetch"],
                    "desc": "强制输出培养方案/综测条款原文出处与真实官网链接，零政策编造",
                },
                {
                    "id": "analyst",
                    "name": "学情诊断与规则精算 Agent",
                    "tools": ["python_sandbox", "academic_engine (Decimal)"],
                    "desc": "执行 160 学分审计、博约通识排查与 What-If 综测推演，零算术幻觉",
                },
                {
                    "id": "action",
                    "name": "机会雷达与日程排期 Agent",
                    "tools": ["sync_feeds", "artifact_create (.ics / HTML)"],
                    "desc": "自动对账校内通知门槛，生成避开满课日的 RFC 5545 日历与交互看板",
                },
                {
                    "id": "critic",
                    "name": "反思核查与记忆进化 Agent",
                    "tools": ["memory_store", "memory_recall"],
                    "desc": "核验条款年份、数值一致性与免责边界，沉淀个人动态成长画像",
                },
            ],
        },
    }
