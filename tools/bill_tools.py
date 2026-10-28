"""Bill sandbox analysis, abnormal detection, and health-report tools."""

from __future__ import annotations

from datetime import datetime

from core.sandbox import run_in_sandbox, verify_numerical_grounding
from ._common import DEFAULT_USER_ID, db_session, make_result, parse_period


def _bills(connection, user_id: str, category: str | None, start: str | None, end: str | None) -> list[dict]:
    clauses = ["user_id=?"]
    params = [user_id]
    if category:
        clauses.append("category=?")
        params.append(category)
    if start:
        clauses.append("occurred_at>=?")
        params.append(start)
    if end:
        clauses.append("occurred_at<=?")
        params.append(end)
    return [dict(row) for row in connection.execute(
        f"SELECT occurred_at, direction, category, merchant, amount, is_abnormal, abnormal_reason, status, remark FROM transactions WHERE {' AND '.join(clauses)} ORDER BY occurred_at", params
    ).fetchall()]


def analyze_bills_in_sandbox(metric_type: str = "summary", category: str = None,
                             month_or_range: str = None, custom_python_expr: str = None,
                             user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "analyze_bills_in_sandbox"
    start = end = None
    if month_or_range:
        start, end = parse_period(month_or_range)
    try:
        with db_session(db_path) as connection:
            bills = _bills(connection, user_id, category, start, end)
        if custom_python_expr:
            code = custom_python_expr
        else:
            code = _metric_code(metric_type)
        sandbox = run_in_sandbox(code, {"bills": bills})
        if not sandbox["success"]:
            return make_result(tool, "账单分析", f"账单计算未执行：{sandbox['error']}", status="blocked", risk_level="RED", risk_reason="代码未通过 AST 白名单或执行限制。", data={"executed_sandbox_code": code, "ast_verified": False, "error": sandbox["error"]})
        numbers = _flatten_numbers(sandbox["result"])
        return make_result(tool, "账单分析", "已完成账单统计计算。", data={"result": sandbox["result"], "executed_sandbox_code": code, "ast_verified": True, "metric_type": metric_type, "category": category, "month_or_range": month_or_range}, ground_truth_numbers=numbers)
    except Exception as exc:
        return make_result(tool, "账单分析", f"账单分析失败：{exc}", status="error", risk_level="RED", risk_reason="沙箱计算异常。")


def _metric_code(metric_type: str) -> str:
    if metric_type == "monthly_income_expense":
        return "income = sum(b['amount'] for b in bills if b['direction'] == 'IN')\nexpense = sum(b['amount'] for b in bills if b['direction'] == 'OUT')\nresult = {'income': income, 'expense': expense, 'net': income - expense}"
    if metric_type == "daily_average":
        return "expense = sum(b['amount'] for b in bills if b['direction'] == 'OUT')\nresult = {'total_expense': expense, 'days': 30, 'daily_average': expense / 30 if expense else 0}"
    if metric_type == "max_single":
        return "outs = [b for b in bills if b['direction'] == 'OUT']\nresult = {'max_single': max((b['amount'] for b in outs), default=0)}"
    if metric_type == "category_ratio":
        return "totals = {}\nfor b in bills:\n    if b['direction'] == 'OUT':\n        totals[b['category']] = totals.get(b['category'], 0) + b['amount']\ngrand = sum(totals.values())\nresult = {'totals': totals, 'ratios': {k: (v / grand if grand else 0) for k, v in totals.items()}}"
    return "totals = {}\nfor b in bills:\n    if b['direction'] == 'OUT':\n        totals[b['category']] = totals.get(b['category'], 0) + b['amount']\nresult = totals"


def _flatten_numbers(value) -> list:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_flatten_numbers(item))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_flatten_numbers(item))
        return out
    return []


def detect_abnormal_transactions(user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "detect_abnormal_transactions"
    try:
        with db_session(db_path) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM transactions WHERE user_id=? AND is_abnormal=1 ORDER BY occurred_at DESC", (user_id,)
            ).fetchall()]
            all_out = [dict(row) for row in connection.execute(
                "SELECT amount FROM transactions WHERE user_id=? AND direction='OUT' ORDER BY occurred_at", (user_id,)
            ).fetchall()]
        amounts = [row["amount"] for row in all_out]
        score = len(rows) * 40
        findings = []
        for row in rows:
            suggestion = "建议立即调用临时锁卡或挂失工具。"
            if "重复扣费" in row["abnormal_reason"]:
                suggestion = "建议核对订阅并联系商户退款，必要时取消代扣。"
            findings.append({"transaction_id": row["transaction_id"], "occurred_at": row["occurred_at"], "merchant": row["merchant"], "amount": row["amount"], "reason": row["abnormal_reason"], "suggested_action": suggestion, "score_contribution": 40})
        return make_result(tool, "账单分析", f"发现 {len(rows)} 笔异常交易，风险评分 {score} 分。", risk_level="RED" if rows else "GREEN", risk_reason="命中凌晨境外高危消费、短时重复扣费等多维规则。" if rows else "未发现已标记异常交易。", data={"abnormal_transactions": findings, "risk_score": score, "count": len(rows), "suggested_actions": ["临时锁卡", "挂失补卡", "联系商户退款"]}, ground_truth_numbers=[len(rows), score, *[row["amount"] for row in rows]])
    except Exception as exc:
        return make_result(tool, "账单分析", f"异常识别失败：{exc}", status="error", risk_level="RED", risk_reason="异常扫描异常。")


def generate_bill_report(period: str = "本月", user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "generate_bill_report"
    try:
        start, end = parse_period(period)
        with db_session(db_path) as connection:
            bills = _bills(connection, user_id, None, start, end)
        income = round(sum(b["amount"] for b in bills if b["direction"] == "IN"), 2)
        expense = round(sum(b["amount"] for b in bills if b["direction"] == "OUT"), 2)
        totals: dict[str, float] = {}
        for bill in bills:
            if bill["direction"] == "OUT":
                totals[bill["category"]] = round(totals.get(bill["category"], 0) + bill["amount"], 2)
        top3 = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:3]
        abnormal = [b for b in bills if b["is_abnormal"]]
        save_rate = round((income - expense) / income, 4) if income > 0 else 0.0
        advice = []
        if save_rate < 0.2:
            advice.append("结余率偏低，建议减少休闲娱乐支出并关注订阅续费。")
        else:
            advice.append("收支结构整体稳健，可考虑将闲钱转入低风险理财。")
        if abnormal:
            advice.append("检测到异常交易，请优先核实并锁卡。")
        summary_voice = f"{period}收入{income:.2f}元，支出{expense:.2f}元，结余率{round(save_rate*100,1)}%。"
        return make_result(tool, "账单分析", f"{period}账单报告：收入 {income:.2f} 元，支出 {expense:.2f} 元。", elder_voice_text=summary_voice, data={"period": period, "total_income": income, "total_expense": expense, "net": round(income - expense, 2), "save_rate": save_rate, "top3_categories": [{"category": k, "amount": v} for k, v in top3], "abnormal_count": len(abnormal), "advice": advice}, ground_truth_numbers=[income, expense, round(income - expense, 2), save_rate, *[v for _, v in top3]])
    except Exception as exc:
        return make_result(tool, "账单分析", f"报告生成失败：{exc}", status="error", risk_level="RED", risk_reason="账单报告生成异常。")
