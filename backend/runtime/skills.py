# -*- coding: utf-8 -*-
"""
Muse Agent Runtime — 可插拔 Skills 技能包管理系统
将特定行业/高校业务规则从底座硬编码中彻底剥离，以标准 Markdown + YAML Frontmatter 技能包按需挂载、在线编辑或扩展。
"""
import os
import re
from typing import Any, Dict, List


BUILTIN_SKILLS: List[Dict[str, str]] = [
    {
        "slug": "python-data-analyst",
        "title": "数据分析与交互式看板生成",
        "description": "使用 Python 沙箱精确处理工作区 Excel/CSV/文本表格数据，并通过 Artifact 画布输出交互式 HTML 可视化报表。",
        "enabled": "true",
        "body": (
            "仅在用户涉及成绩单核算、表格统计、财务计算或数据可视化时触发：\n"
            "1. 严禁心算任何加权均值、GPA 或汇总数字，必须调用 `python_sandbox` 编写 Python 脚本直接读取 `FILES_DIR` 下的真实文件精确计算；\n"
            "2. 若适合可视化展示（如学分完成度看板、多方案收益对比表、趋势图），调用 `artifact_create`（`kind=\"html\"`）生成一份极简干净、自包含内联 CSS/SVG 的交互式 HTML 看板。"
        ),
    },
    {
        "slug": "deep-web-research",
        "title": "全网多源深度检索与核验",
        "description": "结合 web_search 与 web_fetch（含页面内真实导航链接 links 提取）深入阅读原文，输出带真实出处的结论。",
        "enabled": "true",
        "body": (
            "当用户询问外部事实、最新公告、院系专业设置、技术对比或行业调研时：\n"
            "1. 搜索词保持简短精炼（2～4 个核心词，避免堆砌过长同义词）；\n"
            "2. 对目标门户或官网调用 `web_fetch` 时，善用返回结果中的 `links`（页面内真实超链接清单）顺藤摸瓜进入「学院简介 / 机构设置 / 专业介绍」等二级页面，严禁凭空猜测不存在的子域名或路径；\n"
            "3. 回答中以 `[标题](URL)` 标注已核实的真实链接；若个别页面暂时不可达，综合已抓取到的权威线索与可靠知识直接给出清晰答复，不要陷入重复检索死循环。"
        ),
    },
    {
        "slug": "htu-academic-advisor",
        "title": "河南师范大学学业与院系顾问包",
        "description": "面向河师大场景的可插拔领域技能包：内置官网真实栏目路径、博约通识学分结构与综测推免核算指引（非相关问题静默不触发）。",
        "enabled": "true",
        "body": (
            "仅在用户明确询问河南师范大学（河师大）相关院系、专业、通知或学业政策时触发（日常闲聊或其他话题严禁主动提及）：\n"
            "1. **河师大真实官网结构（已核验可达，严禁盲猜 `xxx.htu.edu.cn` 子域名）**：\n"
            "   - 学校主页：`https://www.htu.edu.cn/`（教学科研机构导航页可通过抓主页 `links` 获取）\n"
            "   - 教务处：`https://www.htu.edu.cn/teaching/`（教务通知栏：`https://www.htu.edu.cn/teaching/3251/list.htm`）\n"
            "   - 软件学院：`https://www.htu.edu.cn/sw/`\n"
            "   - 计算机与信息工程学院：`https://www.htu.edu.cn/cs/`（通知栏：`https://www.htu.edu.cn/cs/1006/list.htm`）\n"
            "   查询河师大某学院简介或开设专业时，直接 `web_fetch` 对应学院主页（如 `https://www.htu.edu.cn/sw/`），再从返回的 `links` 中挑选「学院简介」「专业介绍」「本科生教育」链接二次抓取即可快速拿到权威原文。\n"
            "2. **校内特色教务规则参考**：优先调用 `search_knowledge` 检索培养方案与综测推免文档；注意博约通识总分 ≥ 18 学分（经典 ≥ 4、核心 ≥ 12、百花 ≥ 2，线上课需学习通绑定“河南师范大学”），辅修要求第一学期 GPA ≥ 2.50 且无不及格。"
        ),
    },
    {
        "slug": "schedule-ics-builder",
        "title": "日程拆解与原生日历生成",
        "description": "将长期目标或截止事项拆解为具体时间块，并生成可一键导入手机/电脑日历的标准 RFC 5545 .ics Artifact。",
        "enabled": "true",
        "body": (
            "仅当用户希望安排复习计划、备赛排期或导出日历提醒时触发：\n"
            "1. 先调用 `memory_recall` 了解用户的空闲时段或作息偏好；\n"
            "2. 调用 `artifact_create`（`kind=\"ics\"`，`filename=\"schedule.ics\"`）生成符合 RFC 5545 规范的 `VCALENDAR` / `VEVENT`（含 `VALARM` 提前提醒）供用户一键下载导入系统日历。"
        ),
    },
]


def _parse_skill_md(slug: str, raw: str) -> Dict[str, Any]:
    meta = {"slug": slug, "title": slug, "description": "", "enabled": True}
    body = raw.strip()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.DOTALL)
    if m:
        fm, body = m.group(1), m.group(2).strip()
        for ln in fm.splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "title":
                    meta["title"] = v
                elif k == "description":
                    meta["description"] = v
                elif k == "enabled":
                    meta["enabled"] = v.lower() in ("true", "1", "yes")
    return {**meta, "body": body}


def _serialize_skill_md(title: str, description: str, enabled: bool, body: str) -> str:
    en_str = "true" if enabled else "false"
    return f"---\ntitle: {title}\ndescription: {description}\nenabled: {en_str}\n---\n\n{body.strip()}\n"


class SkillManager:
    def __init__(self, skills_dir: str, install_defaults: bool = True):
        self.skills_dir = skills_dir
        os.makedirs(self.skills_dir, exist_ok=True)
        if install_defaults:
            self._ensure_defaults()

    def _ensure_defaults(self):
        existing = {f for f in os.listdir(self.skills_dir) if f.endswith(".md")}
        for sk in BUILTIN_SKILLS:
            fn = f"{sk['slug']}.md"
            if fn not in existing:
                path = os.path.join(self.skills_dir, fn)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(_serialize_skill_md(sk["title"], sk["description"], sk["enabled"] == "true", sk["body"]))

    def list_skills(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for fn in sorted(os.listdir(self.skills_dir)):
            if not fn.endswith(".md"):
                continue
            slug = fn[:-3]
            raw = open(os.path.join(self.skills_dir, fn), encoding="utf-8").read()
            items.append(_parse_skill_md(slug, raw))
        return items

    def save_skill(self, slug: str, title: str, description: str, enabled: bool, body: str) -> Dict[str, Any]:
        clean_slug = re.sub(r"[^a-zA-Z0-9_\-]", "-", slug.strip().lower()).strip("-") or "custom-skill"
        path = os.path.join(self.skills_dir, f"{clean_slug}.md")
        raw = _serialize_skill_md(title.strip() or clean_slug, description.strip(), enabled, body)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        return _parse_skill_md(clean_slug, raw)

    def toggle_skill(self, slug: str) -> List[Dict[str, Any]]:
        path = os.path.join(self.skills_dir, f"{slug}.md")
        if os.path.isfile(path):
            parsed = _parse_skill_md(slug, open(path, encoding="utf-8").read())
            self.save_skill(slug, parsed["title"], parsed["description"], not parsed["enabled"], parsed["body"])
        return self.list_skills()

    def delete_skill(self, slug: str) -> List[Dict[str, Any]]:
        path = os.path.join(self.skills_dir, f"{slug}.md")
        if os.path.isfile(path):
            os.remove(path)
        return self.list_skills()

    def build_active_skills_prompt(self) -> str:
        enabled = [s for s in self.list_skills() if s.get("enabled")]
        if not enabled:
            return ""
        blocks = [f"### [按需参考技能：{s['title']}]\n{s['body']}" for s in enabled]
        return "\n\n".join(blocks)
