"""Deterministic calculations used by the student workspace."""

from decimal import Decimal, ROUND_HALF_UP
import re


def weighted_score(rows_text: str):
    rows = [line.strip() for line in rows_text.splitlines() if line.strip()]
    if rows and re.split(r"[,，\s]+", rows[0]) == ["学分", "成绩"]:
        rows.pop(0)
    if not 1 <= len(rows) <= 500:
        raise ValueError("请输入 1–500 行课程数据。")

    total_credits = Decimal(0)
    total_points = Decimal(0)
    for line_no, row in enumerate(rows, 1):
        values = [value.strip() for value in re.split(r"[,，\t]", row)] if re.search(r"[,，\t]", row) else row.split()
        if len(values) != 2 or any(not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,4})?", v) for v in values):
            raise ValueError(f"第 {line_no} 条课程请填写两个数字，顺序为学分、成绩。")
        credits, score = map(Decimal, values)
        if not Decimal(0) < credits <= Decimal(100) or not Decimal(0) <= score <= Decimal(100):
            raise ValueError(f"第 {line_no} 条课程的学分须大于 0 且不超过 100，成绩须在 0–100 之间。")
        total_credits += credits
        total_points += credits * score

    return {
        "course_count": len(rows),
        "credits": format(total_credits.normalize(), "f"),
        "weighted_score": format((total_points / total_credits).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"),
    }
