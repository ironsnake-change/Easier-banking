"""FastAPI application for the Easier-banking authenticated workbench."""

from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
import json
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import config, database, workspace
from core.ai_banking import (
    ACTION_LABELS,
    ai_banking_assistant,
    authorization_level,
    execute_action as execute_bank_action,
)
from core.agent import banking_agent
from core.compliance import is_clearly_off_topic, refusal_text
from core.receipts import build_receipt_data
from core.security import PENDING_ACTIONS, _SESSION_STATE, security_gateway

DEFAULT_USER = workspace.DEFAULT_USER_ID
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _legacy_user(value: str | None) -> str:
    return DEFAULT_USER if not value or value == "U001" else value


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return database.fetch_all(sql, params)


def _session_user(token: str | None, legacy_user: str | None = None, *, required: bool = True) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="请先登录后再访问账户数据")
    user_id = workspace.resolve_session(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="登录会话已失效，请重新登录")
    return user_id


def _now_display() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _receipt_for_result(user_id: str, result: dict[str, Any], original_message: str = "") -> dict[str, Any]:
    receipt = build_receipt_data(user_id, result, original_message=original_message)
    return receipt or {}


def build_live_state(user_id: str = DEFAULT_USER) -> dict[str, Any]:
    user_id = _legacy_user(user_id)
    workspace.ensure_workspace_schema()
    users = _rows("SELECT * FROM users WHERE user_id=?", (user_id,))
    if not users:
        raise HTTPException(status_code=404, detail="用户不存在")
    user = users[0]
    profile = workspace.get_user_profile(user_id)
    preferences = workspace.get_preferences(user_id)
    accounts = _rows("SELECT * FROM accounts WHERE user_id=? ORDER BY account_id", (user_id,))
    transactions = _rows(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY occurred_at ASC", (user_id,)
    )
    cards = _rows("SELECT * FROM cards WHERE user_id=? ORDER BY card_id", (user_id,))
    products = _rows("SELECT * FROM wealth_products ORDER BY product_id")
    holdings = _rows(
        "SELECT h.*,p.name AS product_name,p.risk_grade,p.expected_yield "
        "FROM wealth_holdings h JOIN wealth_products p ON p.product_id=h.product_id "
        "WHERE h.user_id=? ORDER BY h.holding_id", (user_id,)
    )
    subscriptions = _rows("SELECT * FROM subscriptions WHERE user_id=? ORDER BY sub_id", (user_id,))
    contacts = _rows(
        "SELECT contact_id,name,alias,phone,bank_card,birthday,usual_amount,is_risky,risk_tags "
        "FROM contacts WHERE user_id=? ORDER BY contact_id", (user_id,)
    )
    tasks = _rows("SELECT * FROM scheduled_tasks WHERE user_id=? ORDER BY target_date,task_id", (user_id,))
    monthly: dict[str, dict[str, float]] = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    categories: dict[str, float] = defaultdict(float)
    abnormal: list[dict[str, Any]] = []
    for tx in transactions:
        month = str(tx["occurred_at"])[:7]
        amount = float(tx["amount"])
        monthly[month]["income" if tx["direction"] == "IN" else "expense"] += amount
        if tx["direction"] == "OUT":
            categories[tx["category"]] += amount
        if tx["is_abnormal"]:
            abnormal.append({
                "transaction_id": tx["transaction_id"],
                "occurred_at": tx["occurred_at"],
                "merchant": tx["merchant"],
                "amount": amount,
                "abnormal_reason": tx["abnormal_reason"],
            })
    trend_series = [
        {"period": period, "income": round(values["income"], 2), "expense": round(values["expense"], 2)}
        for period, values in sorted(monthly.items())[-12:]
    ]
    expense_total = sum(categories.values())
    category_breakdown = [
        {"category": key, "amount": round(amount, 2),
         "share": round(amount * 100 / expense_total, 1) if expense_total else 0.0}
        for key, amount in sorted(categories.items(), key=lambda item: item[1], reverse=True)
    ]
    status = security_gateway.get_security_status(user_id)
    audit = security_gateway.get_recent_audit_logs(15, user_id)
    chain = security_gateway.verify_audit_chain()
    primary = accounts[0] if accounts else {"balance": 0.0, "locked_balance": 0.0, "account_id": ""}
    elder = bool(preferences.get("elder_mode", user.get("elderly_mode", 0)))
    return {
        "user_profile": {
            "user_id": user_id,
            "username": profile.get("username", ""),
            "name": user["name"],
            "age": user["age"],
            "risk_level": user["risk_level"],
            "account_type": profile.get("account_type", "elder" if elder else "standard"),
            "display_card_tail": profile.get("display_card_tail", ""),
            "kyc_verified": bool(profile.get("kyc_verified", 1)),
            "available_balance": float(primary["balance"]),
            "locked_balance": float(primary["locked_balance"]),
            "daily_transfer_used": float(user["daily_transfer_used"]),
            "daily_transfer_limit": float(preferences.get("transfer_threshold", 1000.0)),
            "elderly_mode": elder,
        },
        "greeting": (f"张叔叔，您好。今天也陪您把每一笔钱看清楚。" if user_id == DEFAULT_USER and elder
                     else f"{user['name']}，您好。今天也陪您把每一笔钱看清楚。" if elder
                     else f"林砚，您好。您的资金与提醒已准备好。" if user_id == workspace.STANDARD_USER_ID
                     else f"{user['name']}，您好。您的金融工作台已准备好。"),
        "preferences": preferences,
        "pockets": workspace.get_pockets(user_id),
        "chat_messages": workspace.get_chat_messages(user_id),
        "accounts": accounts,
        "transactions": transactions,
        "bill_analytics": {
            "trend_series": trend_series,
            "category_breakdown": category_breakdown,
            "abnormal_transactions": abnormal,
            "transaction_count": len(transactions),
            "income_total": round(sum(float(t["amount"]) for t in transactions if t["direction"] == "IN"), 2),
            "expense_total": round(sum(float(t["amount"]) for t in transactions if t["direction"] == "OUT"), 2),
        },
        "cards": cards,
        "wealth_holdings": holdings,
        "wealth_products": products,
        "subscriptions": subscriptions,
        "scheduled_tasks": tasks,
        "contacts": contacts,
        "security_status": {
            **status,
            "latest_audit_logs": audit,
            "audit_chain": chain,
            "is_integrity_valid": chain["is_integrity_valid"],
        },
        "engine_status": {
            "primary": (f"腾讯云 CloudBase 上海节点直连 ({config.GATEWAY_MODEL}) [已挂载 X-App-Secret 鉴权 | "
                        + (f"实测均值 {banking_agent.router.average_gateway_latency_ms:.0f}ms]" if banking_agent.router.average_gateway_latency_ms is not None else "等待首个网关样本]")),
            "fallback": "三级容灾就绪 (Tencent Gateway -> DeepSeek/Relay -> Local Rule Fallback)",
            "gateway_base_url": config.GATEWAY_BASE_URL,
            "model": config.GATEWAY_MODEL,
            "route_channel": banking_agent.router.last_channel,
            "last_latency_ms": banking_agent.router.last_latency_ms,
        },
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    workspace.ensure_workspace_schema()
    yield


app = FastAPI(title="Easier-banking", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class LoginRequest(BaseModel):
    username_or_card: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    username: str = Field(min_length=4, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    account_type: str = "standard"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str = "U001"
    elder_mode: bool | None = None
    force_local_fallback: bool = False


class ResumeRequest(BaseModel):
    dag_id: str
    pending_action_id: str
    action: str = "approve"
    sms_code: str | None = None
    face_verified: bool = False
    user_id: str = "U001"
    elder_mode: bool | None = None


class RollbackRequest(BaseModel):
    dag_id: str
    reason: str = "用户请求撤销"
    user_id: str = "U001"


class PreferencesRequest(BaseModel):
    chart_type: str | None = None
    elder_mode: bool | None = None
    tts_rate: float | None = None
    sound_enabled: bool | None = None
    auto_read: bool | None = None
    transfer_threshold: float | None = None
    engine_mode: str | None = None


class KinshipRequest(BaseModel):
    contact_id: str
    alias: str = Field(min_length=1, max_length=40)
    user_id: str = "U001"


class CancelSubscriptionRequest(BaseModel):
    subscription_id_or_name: str = Field(min_length=1, max_length=120)
    reason: str = "用户主动退订"
    user_id: str = "U001"


class ConfirmSubscriptionCancellationRequest(BaseModel):
    pending_action_id: str = Field(min_length=1, max_length=64)
    confirm: bool


class AssistantChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    elder_mode: bool | None = None


class AssistantExecuteRequest(BaseModel):
    message_id: int
    temporary_limit_raise: bool = False
    cancel: bool = False


class BankActionRequest(BaseModel):
    action: dict[str, Any]
    confirmed: bool = False
    temporary_limit_raise: bool = False


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    workspace.ensure_workspace_schema()
    return {"status": "ok", "database": str(database.DB_PATH), "time": _now_display()}


@app.get("/api/auth/demo-accounts")
def demo_accounts():
    workspace.ensure_workspace_schema()
    return {"accounts": [
        {"username": "linyan", "name": "林砚", "user_id": "U2001", "card_tail": "8848", "account_type": "standard", "demo_password": workspace.DEMO_PASSWORD},
        {"username": "zhangjg", "name": "张建国", "user_id": "U1001", "card_tail": "0921", "account_type": "elder", "demo_password": workspace.DEMO_PASSWORD},
    ]}


@app.post("/api/auth/login")
def login(request: LoginRequest):
    account = workspace.authenticate(request.username_or_card, request.password)
    if not account:
        raise HTTPException(status_code=401, detail="账号、卡号尾号或密码不正确")
    token = workspace.create_session(account["user_id"])
    return {"status": "success", "session_token": token, "user_profile": workspace.get_user_profile(account["user_id"]),
            "preferences": workspace.get_preferences(account["user_id"]), "live_state": build_live_state(account["user_id"])}


@app.post("/api/auth/register")
def register(request: RegisterRequest):
    try:
        account = workspace.register_account(request.name, request.username, request.password, request.account_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = workspace.create_session(account["user_id"])
    return {"status": "success", "session_token": token, "user_profile": workspace.get_user_profile(account["user_id"]),
            "preferences": workspace.get_preferences(account["user_id"]), "live_state": build_live_state(account["user_id"])}


@app.get("/api/auth/me")
def me(x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token)
    return {"status": "success", "user_profile": workspace.get_user_profile(user_id),
            "preferences": workspace.get_preferences(user_id)}


@app.post("/api/auth/logout")
def logout(x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    workspace.delete_session(x_session_token)
    return {"status": "success", "message": "已安全退出当前账户"}


@app.get("/api/state")
def get_state(user_id: str = "U001", x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    return build_live_state(_session_user(x_session_token, user_id))


@app.get("/api/chat/history")
def chat_history(user_id: str = "U001", x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    return {"messages": workspace.get_chat_messages(_session_user(x_session_token, user_id))}


@app.post("/api/chat")
def chat(request: ChatRequest, x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token, request.user_id)
    preferences = workspace.get_preferences(user_id)
    elder_mode = bool(preferences.get("elder_mode", 0)) if request.elder_mode is None else request.elder_mode
    force_local = bool(request.force_local_fallback or preferences.get("engine_mode") == "local")
    workspace.append_chat_message(user_id, "user", request.message)
    result = banking_agent.chat(request.message, user_id=user_id, elder_mode=elder_mode, force_local_fallback=force_local)
    receipt_data = _receipt_for_result(user_id, result, request.message)
    receipt = {"status": result.get("status"), "dag": result.get("dag", {}), "created_at": _now_display(),
               "receiptData": receipt_data} if receipt_data else {}
    if result.get("compliance_rejected"):
        receipt = {"compliance_rejected": True, "compliance_badge": result.get("compliance_badge")}
    reply = result.get("reply_text") or result.get("summary") or result.get("elder_voice_text") or "已收到您的请求。"
    workspace.append_chat_message(user_id, "assistant", reply, receipt)
    result["live_state"] = build_live_state(user_id)
    result["receiptData"] = receipt_data or None
    return result


def _authorization_card(user_id: str, action: dict[str, Any], level: str,
                        remaining: float, daily_limit: float) -> dict[str, Any]:
    params = action.get("params", {})
    amount = float(params.get("amount") or (params.get("value") if action.get("action") == "card_control" and params.get("operation") == "adjust_limit" else 0) or 0)
    card_tail = str(params.get("card_tail") or "")
    if not card_tail:
        account_rows = database.fetch_all("SELECT card_no FROM cards WHERE user_id=? ORDER BY card_id LIMIT 1", (user_id,))
        card_tail = str(account_rows[0]["card_no"])[-4:] if account_rows else ""
    return {
        "authorization_id": "AUTH-" + secrets.token_hex(5).upper(),
        "level": level,
        "status": "PENDING",
        "action": action,
        "business_type": ACTION_LABELS.get(action.get("action"), "账户操作"),
        "target": params.get("recipient") or params.get("service_name") or params.get("name") or params.get("card_tail") or "当前账户",
        "amount": round(amount, 2),
        "card_tail": card_tail,
        "remaining_limit": round(remaining, 2),
        "daily_limit": round(daily_limit, 2),
        "raise_to": max(5000.0, round(amount + (daily_limit - remaining), 2)) if level == "L3" else None,
    }


def _assistant_action_reply(action: dict[str, Any], level: str, remaining: float, daily_limit: float) -> str:
    name = action.get("action")
    params = action.get("params", {})
    if name == "subscribe_service":
        return f"已识别到「{params.get('service_name')}」付费订阅，月费 ¥{float(params.get('amount', 0)):.2f}。完成下方 L2 授权后，系统才会扣款并创建自动续费协议。"
    if name == "transfer":
        amount = float(params.get("amount", 0))
        if level == "L3":
            return f"本次拟向 {params.get('recipient')} 转账 ¥{amount:.2f}，超过今日剩余额度 ¥{remaining:.2f}。请选择临时提额并完成强验证，或取消交易。"
        return f"已识别到向 {params.get('recipient')} 转账 ¥{amount:.2f} 的指令。请核对下方交易安全授权卡，确认前不会扣款。"
    if name == "manage_pocket":
        operation = params.get("operation")
        verb = "取出" if operation == "withdraw" else "划入"
        return f"已识别到专项口袋「{params.get('name')}」{verb} ¥{float(params.get('amount', 0)):.2f} 的指令，请完成 L2 授权后执行。"
    if name == "card_control":
        return "已生成银行卡风控变更指令。该操作需要 L2 授权，确认后将立即同步卡面、开关状态与审计链。"
    if name == "redeem_wealth":
        return f"已生成理财赎回指令，涉及金额 ¥{float(params.get('amount', 0)):.2f}。确认后将实时返回活期账户。"
    if name == "subscribe_wealth":
        return f"已生成理财申购追加指令，涉及金额 ¥{float(params.get('amount', 0)):.2f}。确认后将从活期账户划入持仓。"
    return "已生成真实账本业务指令，请核对下方授权卡。"


def _tts_summary(user_id: str, reply: str, action: dict[str, Any] | None = None,
                 level: str | None = None, executed: dict[str, Any] | None = None) -> str:
    profile = workspace.get_user_profile(user_id)
    state = build_live_state(user_id)
    elder = profile.get("account_type") == "elder"
    salutation = "张叔叔" if elder else (profile.get("name") or "您好") + "您好"
    params = (action or {}).get("params", {})
    if executed and executed.get("summary"):
        return salutation + "，" + str(executed["summary"]).rstrip("。") + "。"
    if action:
        if action.get("action") == "transfer":
            if level == "L3":
                return salutation + "，这笔转账金额较大，已经超过今天的剩余额度。请确认是否临时提额，确认前不会扣款。"
            return salutation + "，我识别到一笔向" + str(params.get("recipient", "收款人")) + "转账" + f"{float(params.get('amount', 0)):.2f}" + "元的指令，请您确认后我再执行。"
        if action.get("action") == "subscribe_service":
            return salutation + "，我识别到您要开通" + str(params.get("service_name", "付费服务")) + "，每月" + f"{float(params.get('amount', 0)):.2f}" + "元。确认授权后才会扣款。"
        if action.get("action") == "manage_pocket":
            return salutation + "，这是专项资金口袋操作，涉及金额" + f"{float(params.get('amount', 0)):.2f}" + "元，请确认后执行。"
        return salutation + "，这项银行卡或账户设置需要您确认后再执行。"
    lower = str(reply)
    subscriptions = state.get("subscriptions", [])
    if "星光视频" in lower:
        rows = [row for row in state.get("transactions", []) if "星光" in str(row.get("merchant", ""))]
        total = sum(float(row.get("amount", 0)) for row in rows)
        active = next((row for row in subscriptions if str(row.get("status")) == "ACTIVE" and any(name in str(row.get("merchant")) for name in ("星光", "流媒体", "视频"))), None)
        return salutation + f"，近半年视频会员扣款{len(rows)}笔，合计{total:.2f}元。" + (
            f"目前{active['merchant']}每月扣费{float(active['amount']):.2f}元，需要的话我可以帮您关闭。" if active else "目前没有生效中的视频会员代扣。"
        )
    if any(word in lower for word in ("流媒体", "自动续费", "订阅", "代扣")):
        active = [row for row in subscriptions if str(row.get("status")) == "ACTIVE"]
        monthly = sum(float(row.get("amount", 0)) for row in active)
        return salutation + f"，目前共有{len(active)}项自动扣款，每月合计{monthly:.2f}元。如果哪一项不需要，我可以帮您解除授权。"
    analytics = state.get("bill_analytics", {})
    if any(word in lower for word in ("收支", "账单", "支出", "消费", "花了")):
        categories = analytics.get("category_breakdown", [])[:2]
        focus = "和".join(item.get("category", "") for item in categories if item.get("category"))
        remaining = max(0.0, float(state["user_profile"].get("daily_transfer_limit", 1000)) - float(state["user_profile"].get("daily_transfer_used", 0)))
        return salutation + f"，为您汇总近半年收支情况：累计收入{float(analytics.get('income_total', 0)):.2f}元，总支出{float(analytics.get('expense_total', 0)):.2f}元" + (
            f"，主要集中在{focus}" if focus else ""
        ) + f"。目前尾号{profile.get('display_card_tail', '')}账户资金状况正常，今日剩余转账额度{remaining:.2f}元。"
    if any(word in lower for word in ("异常", "盗刷", "境外")):
        abnormal = analytics.get("abnormal_transactions", [])
        total = sum(float(item.get("amount", 0)) for item in abnormal)
        return salutation + f"，我为您查到{len(abnormal)}笔异常交易，合计{total:.2f}元。建议优先核对境外交易，并确认银行卡安全锁状态。"
    sentences = re.split(r"(?<=[。；])", str(reply))
    concise = "".join(sentences[:2])[:260]
    return salutation + "，" + concise if concise else salutation + "，业务已经核对完成。"


@app.post("/api/assistant/chat")
def assistant_chat(request: AssistantChatRequest, x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token)
    preferences = workspace.get_preferences(user_id)
    elder_mode = bool(preferences.get("elder_mode", 0)) if request.elder_mode is None else request.elder_mode
    workspace.append_chat_message(user_id, "user", request.message)
    result = ai_banking_assistant.chat(request.message, user_id, elder_mode)
    action = result.get("bank_action")
    authorization = None
    receipt_data = None
    executed = None
    reply = result.get("reply_text") or "已完成账户核验。"
    if action:
        level = result.get("authorization_level") or authorization_level(user_id, action)[0]
        remaining = float(result.get("remaining_limit", 0))
        daily_limit = float(result.get("daily_limit", 1000))
        authorization = _authorization_card(user_id, action, level, remaining, daily_limit)
        if level == "L1":
            try:
                executed = execute_bank_action(user_id, action, level=level)
                receipt_data = executed.get("receipt")
                authorization.update({"status": "AUTHORIZED", "executed_at": _now_display()})
                reply = str(executed.get("summary") or reply).rstrip() + " ✓ 业务已即时办结 · 审计哈希已生成"
            except Exception as exc:
                reply = reply.rstrip() + " 本次保护性操作未能执行：" + str(exc)
                authorization.update({"status": "FAILED", "error": str(exc)})
        else:
            reply = _assistant_action_reply(action, level, remaining, daily_limit)
    receipt_payload = {}
    tts_text = _tts_summary(user_id, reply, action, level if action else None, executed)
    result["tts_text"] = tts_text
    if tts_text:
        receipt_payload["ttsText"] = tts_text
    if authorization:
        receipt_payload["authorization"] = authorization
    if receipt_data:
        receipt_payload.update({"status": "COMPLETED", "receiptData": receipt_data, "created_at": _now_display()})
    message_id = workspace.append_chat_message(user_id, "assistant", reply, receipt_payload)
    result["reply_text"] = reply
    response = {
        **result,
        "message_id": message_id,
        "authorization": authorization,
        "receiptData": receipt_data,
        "live_state": build_live_state(user_id),
    }
    return response


@app.post("/api/assistant/execute")
def assistant_execute(request: AssistantExecuteRequest, x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token)
    rows = database.fetch_all("SELECT * FROM chat_messages WHERE message_id=? AND user_id=?", (request.message_id, user_id))
    if not rows:
        raise HTTPException(status_code=404, detail="待授权消息不存在")
    row = rows[0]
    try:
        receipt = json.loads(row["receipt_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=409, detail="授权卡数据损坏") from exc
    authorization = receipt.get("authorization")
    if not isinstance(authorization, dict) or authorization.get("status") != "PENDING":
        raise HTTPException(status_code=409, detail="该授权已处理或已失效")
    if request.cancel:
        authorization["status"] = "CANCELLED"
        authorization["cancelled_at"] = _now_display()
        receipt["authorization"] = authorization
        connection = database.get_connection()
        try:
            connection.execute("UPDATE chat_messages SET receipt_json=? WHERE message_id=? AND user_id=?",
                               (json.dumps(receipt, ensure_ascii=False), request.message_id, user_id))
            connection.commit()
        finally:
            connection.close()
        security_gateway.log_event(user_id, "AI_BANK_ACTION_CANCELLED", authorization.get("business_type", ""), "GREEN",
                                   {"message_id": request.message_id}, "CANCELLED")
        return {"status": "CANCELLED", "live_state": build_live_state(user_id)}
    level = str(authorization.get("level") or "L2")
    if level == "L3" and not request.temporary_limit_raise:
        return {"status": "LIMIT_EXCEEDED", "authorization": authorization, "live_state": build_live_state(user_id)}
    started = datetime.now()
    try:
        executed = execute_bank_action(user_id, authorization["action"], level=level,
                                       temporary_limit_raise=request.temporary_limit_raise)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    elapsed_ms = round((datetime.now() - started).total_seconds() * 1000)
    receipt.update({
        "status": "COMPLETED",
        "receiptData": executed.get("receipt"),
        "summary": executed.get("summary"),
        "elapsed_ms": elapsed_ms,
    })
    receipt["ttsText"] = _tts_summary(user_id, executed.get("summary", ""), authorization.get("action"), level, executed)
    authorization.update({"status": "AUTHORIZED", "executed_at": _now_display(), "elapsed_ms": elapsed_ms})
    receipt["authorization"] = authorization
    connection = database.get_connection()
    try:
        updated_content = row["content"].rstrip() + " ✓ 已授权执行"
        connection.execute("UPDATE chat_messages SET content=?,receipt_json=? WHERE message_id=? AND user_id=?",
                           (updated_content, json.dumps(receipt, ensure_ascii=False), request.message_id, user_id))
        connection.commit()
    finally:
        connection.close()
    return {"status": "COMPLETED", "summary": executed.get("summary"), "receiptData": executed.get("receipt"),
            "authorization": authorization, "elapsed_ms": elapsed_ms, "live_state": build_live_state(user_id)}


@app.post("/api/assistant/action")
def assistant_action(request: BankActionRequest, x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token)
    level, remaining, daily_limit = authorization_level(user_id, request.action)
    authorization = _authorization_card(user_id, request.action, level, remaining, daily_limit)
    if level != "L1" and not request.confirmed:
        return {"status": "CONFIRMATION_REQUIRED", "authorization": authorization, "live_state": build_live_state(user_id)}
    try:
        executed = execute_bank_action(user_id, request.action, level=level,
                                       temporary_limit_raise=request.temporary_limit_raise)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    authorization.update({"status": "AUTHORIZED", "executed_at": _now_display()})
    return {"status": "COMPLETED", "summary": executed.get("summary"), "receiptData": executed.get("receipt"),
            "authorization": authorization, "live_state": build_live_state(user_id)}


@app.post("/api/assistant/probe")
def assistant_probe():
    return ai_banking_assistant.probe()


@app.post("/api/compliance/precheck")
def compliance_precheck(request: ChatRequest, x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token, request.user_id)
    if not is_clearly_off_topic(request.message):
        return {"status": "passed", "compliance_rejected": False}
    preferences = workspace.get_preferences(user_id)
    profile = workspace.get_user_profile(user_id)
    elder_mode = bool(preferences.get("elder_mode", 0))
    reply = refusal_text(elder_mode, profile.get("name", ""))
    workspace.append_chat_message(user_id, "user", request.message)
    workspace.append_chat_message(user_id, "assistant", reply, {
        "compliance_rejected": True, "compliance_badge": "🛡️ 业务合规边界提示"
    })
    security_gateway.log_event(user_id, "DOMAIN_GUARDRAIL_REJECTED", "", "GREEN", {"message_length": len(request.message)}, "BLOCKED")
    return {"status": "COMPLIANCE_REJECTED", "compliance_rejected": True,
            "compliance_badge": "🛡️ 业务合规边界提示", "reply_text": reply,
            "live_state": build_live_state(user_id)}


@app.post("/api/dag/resume")
def resume_dag(request: ResumeRequest, x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token, request.user_id)
    elder_mode = bool(workspace.get_preferences(user_id).get("elder_mode", 0)) if request.elder_mode is None else request.elder_mode
    try:
        result = banking_agent.resume_dag_after_auth(request.dag_id, request.pending_action_id, request.action,
                                                     request.sms_code, request.face_verified, user_id, elder_mode)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result.get("reply_text"):
        receipt_data = _receipt_for_result(user_id, result)
        workspace.append_chat_message(
            user_id,
            "assistant",
            result["reply_text"],
            {"status": result.get("status"), "dag": result.get("dag", {}),
             "receiptData": receipt_data} if receipt_data else {},
        )
        result["receiptData"] = receipt_data or None
    result["live_state"] = build_live_state(user_id)
    return result


@app.post("/api/dag/rollback")
def rollback_dag(request: RollbackRequest, x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token, request.user_id)
    try:
        result = banking_agent.abort_and_rollback_dag(request.dag_id, request.reason, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result["live_state"] = build_live_state(user_id)
    return result


@app.post("/api/preferences")
def update_preferences(request: PreferencesRequest, x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token)
    values = request.model_dump(exclude_none=True) if hasattr(request, "model_dump") else request.dict(exclude_none=True)
    result = workspace.update_preferences(user_id, values)
    security_gateway.log_event(user_id, "PREFERENCES_UPDATED", "update_preferences", "GREEN", values)
    return {"status": "success", "preferences": result, "live_state": build_live_state(user_id)}


@app.post("/api/preferences/kinship")
def update_kinship(request: KinshipRequest, x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token, request.user_id)
    rows = _rows("SELECT alias FROM contacts WHERE contact_id=? AND user_id=?", (request.contact_id, user_id))
    if not rows:
        raise HTTPException(status_code=404, detail="联系人不存在")
    connection = database.get_connection()
    try:
        connection.execute("UPDATE contacts SET alias=? WHERE contact_id=? AND user_id=?", (request.alias.strip(), request.contact_id, user_id))
        connection.commit()
    finally:
        connection.close()
    security_gateway.log_event(user_id, "KINSHIP_ALIAS_UPDATED", "update_kinship", "GREEN", {"contact_id": request.contact_id, "alias": request.alias.strip()})
    return {"status": "success", "contacts": build_live_state(user_id)["contacts"]}


@app.post("/api/subscriptions/cancel")
def cancel_subscription(request: CancelSubscriptionRequest, x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token, request.user_id)
    gate = security_gateway.intercept_tool_call(
        "cancel_subscription",
        {"subscription_id_or_name": request.subscription_id_or_name, "reason": request.reason},
        user_id,
    )
    if gate.get("status") == "pending":
        return {
            "status": "pending",
            "gate_status": gate.get("gate_status"),
            "pending_action_id": gate.get("pending_action_id"),
            "risk_level": gate.get("risk_level"),
            "risk_reason": gate.get("risk_reason"),
            "confirmation_prompt": gate.get("confirmation_prompt"),
        }
    if gate.get("status") != "success":
        return {**gate, "live_state": build_live_state(user_id)}
    return {"status": "success", "tool_result": gate.get("tool_result"), "live_state": build_live_state(user_id)}


@app.post("/api/subscriptions/cancel/confirm")
def confirm_subscription_cancellation(
    request: ConfirmSubscriptionCancellationRequest,
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
):
    user_id = _session_user(x_session_token)
    receipt_data = None
    if request.confirm:
        gate = security_gateway.approve_pending_action(
            request.pending_action_id, "CONFIRM", user_id=user_id, expected_tool_name="cancel_subscription"
        )
    else:
        gate = security_gateway.reject_pending_action(
            request.pending_action_id, "用户取消退订确认", user_id, expected_tool_name="cancel_subscription"
        )
    if gate.get("gate_status") == "PENDING_ACTION_NOT_FOUND":
        raise HTTPException(status_code=404, detail=gate.get("summary", "待确认操作不存在或已处理"))
    if gate.get("status") == "success" and gate.get("tool_result"):
        pseudo = {"status": "COMPLETED", "route_channel": "security_gateway",
                  "dag": {"dag_id": "SUB-" + request.pending_action_id, "nodes": [{
                      "tool_name": "cancel_subscription", "arguments": {}, "status": "COMPLETED", "risk_level": gate.get("risk_level", "YELLOW"),
                      "result": gate.get("tool_result"),
                  }]}, "security_panel": {"circuit_breaker": security_gateway.get_security_status(user_id)}}
        receipt_data = _receipt_for_result(user_id, pseudo)
    response = {**gate, "live_state": build_live_state(user_id)}
    response["receiptData"] = receipt_data
    if receipt_data and gate.get("tool_result"):
        workspace.append_chat_message(user_id, "assistant", gate["tool_result"].get("summary", "周期代扣已更新。"), {"status": "success", "receiptData": receipt_data})
    return response


@app.post("/api/security/reset-breaker")
def reset_breaker(x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    user_id = _session_user(x_session_token)
    return {"status": "success", "security_status": security_gateway.reset_circuit_breaker(user_id), "live_state": build_live_state(user_id)}


@app.post("/api/security/verify-chain")
def verify_chain(x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    _session_user(x_session_token)
    return security_gateway.verify_audit_chain()


@app.post("/api/system/reset-demo")
def reset_demo(x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    _session_user(x_session_token)
    workspace.reset_workspace_data()
    for table in ("audit_logs", "security_sessions", "pending_actions", "agent_dag_tasks"):
        connection = database.get_connection()
        try:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.commit()
        finally:
            connection.close()
    PENDING_ACTIONS.clear()
    _SESSION_STATE.clear()
    from core.agent import _TASKS
    _TASKS.clear()
    workspace.ensure_workspace_schema()
    return {"status": "success", "database": str(database.DB_PATH), "live_state": build_live_state(DEFAULT_USER)}


__all__ = ["app", "build_live_state"]
