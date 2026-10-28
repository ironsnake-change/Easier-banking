"""Deterministic domain guardrails shared by API preflight and the agent."""

from __future__ import annotations

import re

FINANCIAL_CONTEXT = (
    "银行", "账户", "余额", "转账", "汇款", "账单", "流水", "收支", "消费", "收入", "支出",
    "理财", "基金", "股票", "利率", "利息", "存款", "存款保险", "汇率", "通胀", "lpr", "贷款",
    "信用卡", "银行卡", "卡片", "锁定", "解锁", "口袋", "异常交易", "盗刷", "诈骗", "反诈",
    "代扣", "订阅", "退订", "自动扣费", "安全", "风控", "风险", "验证码", "人脸", "审计",
    "哈希", "回单", "收款人", "联系人", "存钱", "养老金", "退休金", "工资", "手续费", "预算",
    "报销", "分账", "aa", "本系统", "怎么用", "功能", "客服", "支付", "还款", "余额宝",
)
OFF_TOPIC_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"(?:写|编写|生成|调试).{0,12}(?:python|java|c++|javascript|代码|程序)",
    r"(?:游戏|电竞).{0,12}(?:攻略|怎么玩|出装|阵容|通关)",
    r"(?:讲|说|编).{0,8}(?:笑话|故事|段子)",
    r"(?:娱乐八卦|明星绯闻|体育赛事|足球比分|篮球比赛)",
    r"(?:代写|写一篇).{0,12}(?:作文|小说|情书|非金融|论文)",
))
STANDARD_REFUSAL = "抱歉，我是您的私人金融助理。受银行安全与合规准则限制，我仅支持办理账户交易、财务分析及解答金融通识问题，暂不支持处理与金融业务无关的请求。"
ELDER_REFUSAL = "实在不好意思呀，我是专门帮您管钱、查账和防诈骗的银行小助手，刚才这个跟咱们账户和金融没关系的问题，按规定我没办法回答。您看要不要先查查账单或者办点银行业务？"

def is_clearly_off_topic(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    has_financial_context = any(term.casefold() in value.casefold() for term in FINANCIAL_CONTEXT)
    return not has_financial_context and any(pattern.search(value) for pattern in OFF_TOPIC_PATTERNS)

def refusal_text(elder_mode: bool, name: str = "") -> str:
    if elder_mode:
        elder_name = str(name or "您好").strip()
        return f"{elder_name}，{ELDER_REFUSAL}"
    return STANDARD_REFUSAL

__all__ = ["is_clearly_off_topic", "refusal_text", "STANDARD_REFUSAL", "ELDER_REFUSAL"]
