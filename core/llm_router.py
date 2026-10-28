"""Optional cloud model routing with deterministic offline intent planning."""

from __future__ import annotations

import re
import time
from typing import Any

from core import config
from core.gateway_guard import cloud_guard
from core.privacy import redact_sensitive_text


class LLMRouter:
    def __init__(self, timeout: float = 7.0):
        self.timeout = max(1.0, min(float(timeout), 8.0))
        self.last_channel = "local_rule_fallback"
        self.last_latency_ms: float | None = None
        self._gateway_latency_samples: list[float] = []
        self.last_fallback_reason: str | None = None
        self.cloud_guard = cloud_guard

    @property
    def average_gateway_latency_ms(self) -> float | None:
        if not self._gateway_latency_samples:
            return None
        return round(sum(self._gateway_latency_samples) / len(self._gateway_latency_samples), 1)

    def route_channel(self, force_local_fallback: bool = False) -> str:
        if force_local_fallback:
            return "local_rule_fallback"
        if config.gateway_configured():
            return "tencent_gateway"
        if config.DEEPSEEK_BASE_URL and config.DEEPSEEK_API_KEY and not config.DEEPSEEK_API_KEY.startswith("YOUR_"):
            return "primary_llm"
        if config.RELAY_BASE_URL and config.RELAY_API_KEY:
            return "backup_llm"
        return "local_rule_fallback"

    def _cloud_plan(self, text: str, user_id: str = "U1001") -> list[dict[str, Any]] | None:
        """Try configured OpenAI-compatible providers; accept only registered tools."""
        providers = [
            # OpenAI SDK appends /chat/completions to this base URL.
            ("tencent_gateway", config.GATEWAY_BASE_URL.rstrip("/"), "gateway-proxy",
             config.GATEWAY_MODEL, {"X-App-Secret": config.GATEWAY_APP_SECRET}),
            ("primary_llm", config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY,
             config.DEEPSEEK_FAST_MODEL, {}),
            ("backup_llm", config.RELAY_BASE_URL, config.RELAY_API_KEY,
             config.RELAY_FAST_MODEL, {}),
        ]
        providers = [
            provider for provider in providers
            if provider[1] and provider[2] and not str(provider[2]).startswith("YOUR_")
        ]
        if not providers:
            self.last_fallback_reason = "offline_mode"
            return None
        try:
            from openai import OpenAI
            from tools.registry import get_openai_tool_schemas
        except ImportError:
            return None
        allowed, reason = self.cloud_guard.before_call(user_id)
        if not allowed:
            self.last_fallback_reason = reason
            return None
        saw_response = False
        for channel, base_url, api_key, model, default_headers in providers:
            try:
                client = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    default_headers=default_headers or None,
                    timeout=self.timeout,
                    max_retries=0,
                )
                started_at = time.perf_counter()
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "你是 Easier-banking 的金融业务规划器，只能处理账户交易与查询、账单分析、理财适当性、银行卡安全、自动代扣、专项资金管理、反诈与基础金融通识（利率、存款保险、汇率、通胀、LPR、利息计算）及本系统操作咨询。对无关代码、游戏、娱乐八卦、笑话故事、体育和非金融闲聊，不调用工具并礼貌说明业务边界。只输出 JSON 对象 {steps:[{tool_name,title,arguments}]}；只能选择提供的工具；金额、账户和联系人不明确时不要猜，使用空 steps。转账前先 resolve_contact。不要宣称操作已执行。"},
                        {"role": "user", "content": redact_sensitive_text(text)},
                    ],
                    tools=get_openai_tool_schemas(),
                    tool_choice="none",
                    response_format={"type": "json_object"},
                )
                saw_response = True
                elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
                self.last_latency_ms = elapsed_ms
                if channel == "tencent_gateway":
                    self._gateway_latency_samples.append(elapsed_ms)
                    self._gateway_latency_samples = self._gateway_latency_samples[-100:]
                import json
                message = response.choices[0].message
                content = message.content or "{}"
                try:
                    steps = json.loads(content).get("steps", [])
                except (TypeError, json.JSONDecodeError):
                    steps = []
                # Some OpenAI-compatible gateways return structured function calls
                # even when the planner prompt asks for a JSON envelope. Normalize
                # those calls into the same validated plan representation.
                if not steps and message.tool_calls:
                    steps = []
                    for tool_call in message.tool_calls:
                        function = tool_call.function
                        arguments = json.loads(function.arguments or "{}")
                        if not isinstance(arguments, dict):
                            raise ValueError("模型工具调用参数必须是 JSON 对象")
                        steps.append({
                            "tool_name": function.name,
                            "title": function.name,
                            "arguments": arguments,
                        })
                from tools.registry import TOOL_REGISTRY
                if not isinstance(steps, list) or not steps:
                    continue
                validated = []
                for item in steps:
                    name = item.get("tool_name")
                    args = item.get("arguments", {})
                    if name not in TOOL_REGISTRY or not isinstance(args, dict):
                        raise ValueError("模型规划包含无效工具或参数")
                    validated.append({"tool_name": name, "title": str(item.get("title") or name), "arguments": args})
                self.last_channel = channel
                self.last_fallback_reason = None
                self.cloud_guard.record_success(user_id)
                return validated
            except Exception:
                continue
        if saw_response:
            self.cloud_guard.record_success(user_id)
            self.last_fallback_reason = "empty_plan"
        else:
            self.last_fallback_reason = self.cloud_guard.record_failure(user_id)
        return None

    @staticmethod
    def _amount(text: str, default: float | None = None) -> float | None:
        match = re.search(r"(?<!\d)(\d[\d,]*(?:\.\d{1,2})?)\s*(?:元|块)?", text)
        return float(match.group(1).replace(",", "")) if match else default

    @staticmethod
    def _contact(text: str) -> str:
        for key in ("大儿子", "大孙子", "孙子", "小宝", "老伴", "爱人", "老婆", "房东", "张伟", "王芳", "许小宝", "李强"):
            if key in text:
                return key
        match = re.search(r"(?:给|向)\s*([\u4e00-\u9fff]{2,8})\s*(?:转|汇)", text)
        return match.group(1) if match else ""

    def plan(self, text: str, force_local_fallback: bool = False, user_id: str = "U1001") -> list[dict[str, Any]]:
        self.last_channel = "local_rule_fallback"
        self.last_fallback_reason = "forced_local" if force_local_fallback else None
        if not force_local_fallback:
            cloud_plan = self._cloud_plan(text, user_id=user_id)
            if cloud_plan:
                return cloud_plan
        msg = text.strip()
        nodes: list[dict[str, Any]] = []
        def add(name: str, title: str, args: dict | None = None):
            nodes.append({"tool_name": name, "title": title, "arguments": args or {}})

        amount = self._amount(msg)
        # High-value composite scenarios have explicit ordering and arguments.
        if "锁定" in msg and "订单" in msg and amount is not None:
            values = [float(x.replace(",", "")) for x in re.findall(r"(?<!\d)(\d[\d,]*(?:\.\d{1,2})?)\s*元?", msg)]
            lock_amount = values[0] if values else amount
            order_amount = values[1] if len(values) > 1 else lock_amount
            add("query_cards_status", "查询银行卡状态")
            add("lock_demand_funds", "锁定专项资金", {"amount": lock_amount, "purpose": "三亚旅游专项资金"})
            title = "三亚机票酒店生活服务订单" if "三亚" in msg else "生活服务订单"
            add("create_lifestyle_order", "创建生活服务订单", {"service_type": "旅游康养", "title": title, "amount": order_amount, "use_locked_funds": True})
            return nodes
        if "异常交易" in msg or "盗刷" in msg or "异常消费" in msg:
            add("detect_abnormal_transactions", "检查异常交易")
            if any(x in msg for x in ("锁卡", "锁住", "临时锁", "卡片")):
                add("manage_card_limits_and_lock", "临时锁定银行卡", {"card_id": "CARD_001", "action": "lock"})
            if "退订" in msg or "取消" in msg:
                add("scan_subscriptions", "扫描自动扣费")
                add("cancel_subscription", "取消视频会员代扣", {"subscription_id_or_name": "星光视频"})
            if len(nodes) > 1:
                return nodes
        if "账单" in msg and any(x in msg for x in ("分析", "花", "报告", "本月")):
            add("analyze_bills_in_sandbox", "分析本月账单", {"metric_type": "summary", "month_or_range": "本月"})
            if "异常" in msg or "盗刷" in msg:
                add("detect_abnormal_transactions", "检查异常交易")
            return nodes
        if "异常" in msg or "盗刷" in msg:
            add("detect_abnormal_transactions", "检查异常交易")
            return nodes
        if "订阅" in msg or "自动扣费" in msg or "代扣" in msg:
            if "退订" in msg or "取消" in msg:
                add("cancel_subscription", "取消自动代扣", {"subscription_id_or_name": next((x for x in ("星光视频", "极速云盘", "医疗险") if x in msg), "")})
            else:
                add("scan_subscriptions", "查询自动扣费订阅")
            return nodes
        if "余额" in msg or "银行卡状态" in msg or "卡状态" in msg:
            add("query_cards_status", "查询银行卡状态")
            if not any(x in msg for x in ("再", "然后", "接着")):
                return nodes
        redeem_then_transfer = "赎回" in msg and ("转账" in msg or "给" in msg)
        if "赎回" in msg:
            add("redeem_wealth_product", "赎回理财产品", {"holding_id_or_product_id": "PROD_01", "amount": amount})
        elif "理财" in msg and any(x in msg for x in ("比较", "对比", "产品")):
            add("query_and_compare_wealth", "比较理财产品")
        elif "理财" in msg and "评估" in msg:
            add("evaluate_user_risk_profile", "评估风险承受能力")
        if "转账" in msg or re.search(r"给.{0,8}转\s*\d", msg):
            payee = self._contact(msg)
            if payee:
                add("resolve_contact", "确认收款人", {"query": payee})
                numbers = [float(x.replace(",", "")) for x in re.findall(r"(?<!\d)(\d[\d,]*(?:\.\d{1,2})?)\s*元?", msg)]
                transfer_amount = numbers[-1] if redeem_then_transfer and len(numbers) > 1 else (amount or 0.0)
                add("execute_transfer", "办理转账", {"payee_id_or_account": payee, "amount": transfer_amount,
                    "remark": "生活费" if "生活费" in msg else "用户发起转账"})
            return nodes
        if "查" in msg and ("卡" in msg or "银行卡" in msg):
            if not nodes:
                add("query_cards_status", "查询银行卡状态")
            return nodes
        if not nodes:
            add("query_cards_status", "查询账户与银行卡状态")
        return nodes
