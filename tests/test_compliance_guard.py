import pytest

from core.compliance import is_clearly_off_topic


@pytest.mark.parametrize(
    "message",
    [
        "今天天气如何",
        "明天会下雨吗",
        "明天足球比赛结果是多少",
        "篮球比赛谁赢了",
        "给我讲个笑话",
        "写一段 Python 代码",
        "明星绯闻八卦",
    ],
)
def test_clear_off_topic_requests_are_rejected(message):
    assert is_clearly_off_topic(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "LPR 下降对房贷有什么影响",
        "我的养老金这个月到账了吗",
        "银行卡丢失后如何挂失",
        "分析今年三季度消费支出",
        "世界杯主题信用卡有哪些权益",
    ],
)
def test_financial_requests_are_not_blocked(message):
    assert is_clearly_off_topic(message) is False
