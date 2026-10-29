"""Deterministic DAG banking agent with security-gated execution and Saga rollback."""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from core import database
from core.compliance import is_clearly_off_topic, refusal_text
from core.llm_router import LLMRouter
from core.security import security_gateway
from core import workspace
from tools import registry


@dataclass
class DAGNode:
    step_id: str
    title: str
    tool_name: str
    arguments: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)
    risk_level: str = "GREEN"
    status: str = "PENDING"
    pending_action_id: str | None = None
    result: dict[str, Any] | None = None
    rollback_token: dict[str, Any] | None = None


@dataclass
class DAGTask:
    dag_id: str
    user_id: str
    original_message: str
    nodes: list[DAGNode]
    overall_status: str = "RUNNING"
    pending_step_id: str | None = None


_TASKS: dict[str, DAGTask] = {}
_LOCK = threading.RLock()


def _ensure_task_schema() -> None:
    connection = database.get_connection()
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS agent_dag_tasks (dag_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,overall_status TEXT NOT NULL,payload_json TEXT NOT NULL,updated_at TEXT NOT NULL)")
        connection.commit()
    finally:
        connection.close()


def _serialize(task: DAGTask) -> str:
    return json.dumps(asdict(task), ensure_ascii=False, default=str)


def _persist(task: DAGTask) -> None:
    _ensure_task_schema()
    connection = database.get_connection()
    try:
        connection.execute("INSERT INTO agent_dag_tasks VALUES (?,?,?,?,datetime('now')) ON CONFLICT(dag_id) DO UPDATE SET overall_status=excluded.overall_status,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                           (task.dag_id, task.user_id, task.overall_status, _serialize(task)))
        connection.commit()
    finally:
        connection.close()


def _restore(dag_id: str) -> DAGTask | None:
    with _LOCK:
        if dag_id in _TASKS:
            return _TASKS[dag_id]
    _ensure_task_schema()
    rows = database.fetch_all("SELECT payload_json FROM agent_dag_tasks WHERE dag_id=?", (dag_id,))
    if not rows:
        return None
    raw = json.loads(rows[0]["payload_json"])
    task = DAGTask(raw["dag_id"], raw["user_id"], raw["original_message"],
                   [DAGNode(**node) for node in raw["nodes"]], raw["overall_status"], raw.get("pending_step_id"))
    with _LOCK:
        _TASKS[dag_id] = task
    return task


def build_mermaid_graph(dag_task: DAGTask | dict) -> str:
    data = asdict(dag_task) if isinstance(dag_task, DAGTask) else dag_task
    nodes = data.get("nodes", [])
    lines = ["graph TD"]
    styles = {"COMPLETED": "completed", "WAITING_CONFIRM": "waiting", "WAITING_MFA": "mfa",
              "ROLLED_BACK": "rolledback", "PENDING": "pending", "ABORTED": "pending", "FAILED": "failed", "RUNNING": "running"}
    for node in nodes:
        status = node.get("status", "PENDING")
        icon = {"COMPLETED": "✓", "WAITING_CONFIRM": "⏸", "WAITING_MFA": "🔐", "ROLLED_BACK": "↶",
                "PENDING": "○", "ABORTED": "⊘", "FAILED": "✗", "RUNNING": "…"}.get(status, "○")
        label = f"{node.get('step_id','')} {icon} {node.get('title',node.get('tool_name',''))} [{status}]"
        lines.append(f"  {node.get('step_id')}[{label}]")
    for node in nodes:
        for dependency in node.get("depends_on", []):
            lines.append(f"  {dependency} --> {node.get('step_id')}")
    lines.extend([
        "  classDef completed fill:#dcfce7,stroke:#16a34a,color:#14532d",
        "  classDef waiting fill:#ffedd5,stroke:#f97316,color:#7c2d12",
        "  classDef mfa fill:#fee2e2,stroke:#dc2626,color:#7f1d1d",
        "  classDef rolledback fill:#f3e8ff,stroke:#9333ea,stroke-dasharray:5 5,color:#581c87",
        "  classDef pending fill:#f1f5f9,stroke:#94a3b8,color:#334155",
        "  classDef failed fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d",
        "  classDef running fill:#dbeafe,stroke:#2563eb,color:#1e3a8a",
    ])
    for node in nodes:
        lines.append(f"  class {node.get('step_id')} {styles.get(node.get('status','PENDING'),'pending')}")
    return "\n".join(lines)


class BankingAgent:
    def __init__(self, router: LLMRouter | None = None, gateway=None):
        self.router = router or LLMRouter()
        self.gateway = gateway or security_gateway

    def _panel(self, user_id: str, grounding: dict | None = None) -> dict:
        return {"grounding_check": grounding or {"passed": True, "unverified_numbers": [], "grounded_numbers": []},
                "circuit_breaker": self.gateway.get_security_status(user_id),
                "latest_audit_logs": self.gateway.get_recent_audit_logs(8, user_id),
                "chain_integrity": self.gateway.verify_audit_chain()}

    def _response(self, task: DAGTask, status: str, reply: str, elder: bool,
                  pending: dict | None = None, grounding: dict | None = None) -> dict:
        if grounding is None:
            known_numbers = []
            for item in task.nodes:
                if item.result:
                    known_numbers.extend(item.result.get("ground_truth_numbers", []))
            grounding = {"grounded_numbers": known_numbers}
        clean = self.gateway.inspect_and_sanitize_output(reply, grounding.get("grounded_numbers", []) if grounding else [], "banking")
        profile = workspace.get_user_profile(task.user_id)
        profile_name = str(profile.get("name") or "您好")
        voice = (profile_name + "，" + clean["sanitized_text"]) if elder else clean["sanitized_text"]
        return {"status": status, "reply_text": clean["sanitized_text"], "elder_voice_text": voice,
                "route_channel": self.router.last_channel, "dag": {"dag_id": task.dag_id,
                "overall_status": task.overall_status, "nodes": [asdict(node) for node in task.nodes],
                "mermaid_graph": build_mermaid_graph(task),
                "can_rollback": any(n.status == "COMPLETED" and n.rollback_token for n in task.nodes)},
                "pending_interaction": pending, "security_panel": self._panel(task.user_id, clean["grounding_check"])}

    def _continue(self, task: DAGTask, elder_mode: bool) -> dict:
        all_numbers: list[Any] = []
        summaries: list[str] = []
        for node in task.nodes:
            if node.status != "PENDING":
                if node.result:
                    all_numbers.extend(node.result.get("ground_truth_numbers", []))
                    if node.status in {"COMPLETED", "ROLLED_BACK"} and node.tool_name != "resolve_contact":
                        summaries.append(node.result.get("summary", ""))
                continue
            node.status = "RUNNING"
            risk = registry.evaluate_tool_risk(node.tool_name, node.arguments, task.user_id)
            node.risk_level = risk["risk_level"]
            gate = self.gateway.intercept_tool_call(node.tool_name, node.arguments, task.user_id, task.dag_id)
            if gate.get("gate_status") == "EXECUTED":
                node.result = gate.get("tool_result")
                if node.result.get("status") != "success":
                    node.status = "FAILED"
                    task.overall_status = "FAILED"
                    _persist(task)
                    return self._response(task, "ERROR", node.result.get("summary", "操作未能完成。"), elder_mode)
                node.status = "COMPLETED"
                node.rollback_token = node.result.get("rollback_token")
                all_numbers.extend(node.result.get("ground_truth_numbers", []))
                if node.tool_name != "resolve_contact":
                    summaries.append(node.result.get("summary", ""))
                _persist(task)
                continue
            if gate.get("status") == "pending":
                node.risk_level = gate.get("risk_level", node.risk_level)
                node.status = "WAITING_MFA" if node.risk_level == "RED" else "WAITING_CONFIRM"
                node.pending_action_id = gate["pending_action_id"]
                task.pending_step_id = node.step_id
                task.overall_status = "PAUSED_FOR_MFA" if node.risk_level == "RED" else "PAUSED_FOR_CONFIRM"
                _persist(task)
                pending = {"pending_action_id": node.pending_action_id, "dag_id": task.dag_id,
                           "step_id": node.step_id, "tool_name": node.tool_name, "risk_level": node.risk_level,
                           "risk_reason": gate.get("risk_reason", "需要授权"), "require_mfa": node.risk_level == "RED",
                           "arguments": node.arguments, "demo_hint": gate.get("demo_hint")}
                text = "；".join(filter(None, summaries + [gate.get("confirmation_prompt", "请确认本次操作。")]))
                return self._response(task, task.overall_status, text, elder_mode, pending)
            node.status = "FAILED"
            task.overall_status = "BLOCKED_BY_SECURITY"
            _persist(task)
            return self._response(task, "BLOCKED_BY_SECURITY", gate.get("summary", "安全策略已拦截该操作。"), elder_mode)
        task.overall_status = "COMPLETED"
        _persist(task)
        reply = "；".join(filter(None, summaries)) or "已完成处理。"
        grounding = {"passed": True, "unverified_numbers": [], "grounded_numbers": all_numbers}
        return self._response(task, "COMPLETED", reply, elder_mode, grounding=grounding)

    def chat(self, user_message: str, user_id: str = "U001", elder_mode: bool = True,
             force_local_fallback: bool = False) -> dict:
        user_id = "U1001" if user_id == "U001" else user_id
        inspection = self.gateway.inspect_user_input(user_message, user_id)
        if not inspection.get("passed"):
            task = DAGTask("DAG-" + uuid.uuid4().hex[:10].upper(), user_id, user_message, [], "BLOCKED_BY_SECURITY")
            _persist(task)
            return self._response(task, "BLOCKED_BY_SECURITY", inspection.get("reason", "安全护盾已拦截请求。") + " " + inspection.get("advice", ""), elder_mode)
        if is_clearly_off_topic(user_message):
            task = DAGTask("DAG-" + uuid.uuid4().hex[:10].upper(), user_id, user_message, [], "COMPLIANCE_REJECTED")
            profile = workspace.get_user_profile(user_id)
            response = self._response(task, "COMPLIANCE_REJECTED", refusal_text(elder_mode, profile.get("name", "")), elder_mode)
            response["compliance_rejected"] = True
            response["compliance_badge"] = "🛡️ 业务合规边界提示"
            response["route_channel"] = "domain_guardrail"
            return response
        plans = self.router.plan(user_message, force_local_fallback=force_local_fallback, user_id=user_id)
        if not plans:
            task = DAGTask("DAG-" + uuid.uuid4().hex[:10].upper(), user_id, user_message, [], "CLARIFICATION_NEEDED")
            return self._response(task, "CLARIFICATION_NEEDED", "请告诉我您想查询或办理哪项业务。", elder_mode)
        dag_id = "DAG-" + uuid.uuid4().hex[:10].upper()
        if plans and plans[0]["tool_name"] == "resolve_contact" and len(plans) > 1:
            resolved = registry.execute_tool("resolve_contact", plans[0]["arguments"], user_id)
            contacts = resolved.get("data", {}).get("matches", [])
            if not contacts and resolved.get("data", {}).get("contact"):
                contacts = [resolved["data"]["contact"]]
            if resolved.get("status") == "clarification_needed" or len(contacts) != 1:
                task = DAGTask(dag_id, user_id, user_message, [DAGNode("S1", "确认具体联系人", "resolve_contact", plans[0]["arguments"])], "CLARIFICATION_NEEDED")
                task.nodes[0].result = resolved
                _persist(task)
                return self._response(task, "CLARIFICATION_NEEDED", resolved.get("summary", "请确认收款人。"), elder_mode)
            plans[1]["arguments"]["payee_id_or_account"] = contacts[0].get("contact_id") or contacts[0].get("name")
        nodes = [DAGNode(f"S{i+1}", p["title"], p["tool_name"], p["arguments"], [f"S{i}"] if i else []) for i,p in enumerate(plans)]
        for node in nodes:
            node.risk_level = registry.evaluate_tool_risk(
                node.tool_name, node.arguments, user_id
            ).get("risk_level", "GREEN")
        task = DAGTask(dag_id, user_id, user_message, nodes)
        with _LOCK:
            _TASKS[dag_id] = task
        _persist(task)
        return self._continue(task, elder_mode)

    def resume_dag_after_auth(self, dag_id: str, pending_action_id: str, action: str = "approve",
                              sms_code: str | None = None, user_id: str = "U001",
                              elder_mode: bool = True) -> dict:
        task = _restore(dag_id)
        if not task or task.user_id not in {user_id, "U1001" if user_id == "U001" else user_id}:
            raise ValueError("DAG 不存在或不属于当前用户")
        node = next((n for n in task.nodes if n.pending_action_id == pending_action_id), None)
        if not node:
            return self._response(task, "ERROR", "待确认操作已失效。", elder_mode)
        if action.lower() == "reject":
            rejected = self.gateway.reject_pending_action(pending_action_id, user_id=user_id)
            node.status = "ABORTED"
            task.overall_status = "ABORTED"
            _persist(task)
            return self._response(task, "COMPLETED", rejected.get("summary", "已取消本次操作。"), elder_mode)
        outcome = self.gateway.approve_pending_action(pending_action_id, "CONFIRM", sms_code, user_id=user_id)
        if outcome.get("status") != "success":
            if outcome.get("gate_status") == "MFA_VERIFICATION_FAILED" and not outcome.get("remaining_attempts", 1):
                node.status = "FAILED"
                task.pending_step_id = None
                task.overall_status = "BLOCKED_BY_SECURITY"
                _persist(task)
                return self._response(task, "BLOCKED_BY_SECURITY", outcome.get("summary", "验证已作废。"), elder_mode)
            return self._response(task, "PAUSED_FOR_MFA" if node.risk_level == "RED" else "PAUSED_FOR_CONFIRM",
                                  outcome.get("summary", "验证尚未通过，请重试。"), elder_mode,
                                  {"pending_action_id": pending_action_id, "dag_id": dag_id, "step_id": node.step_id,
                                   "tool_name": node.tool_name, "risk_level": node.risk_level,
                                   "require_mfa": node.risk_level == "RED", "arguments": node.arguments})
        node.result = outcome["tool_result"]
        node.rollback_token = outcome.get("rollback_token")
        node.status = "COMPLETED"
        node.pending_action_id = None
        task.pending_step_id = None
        task.overall_status = "RUNNING"
        _persist(task)
        return self._continue(task, elder_mode)

    def abort_and_rollback_dag(self, dag_id: str, reason: str = "用户中途打断并回滚", user_id: str = "U001") -> dict:
        task = _restore(dag_id)
        if not task:
            raise ValueError("DAG 不存在")
        if task.pending_step_id:
            pending_node = next((n for n in task.nodes if n.step_id == task.pending_step_id), None)
            if pending_node and pending_node.pending_action_id:
                self.gateway.reject_pending_action(pending_node.pending_action_id, reason, user_id)
                pending_node.status = "ABORTED"
        report = []
        for node in reversed(task.nodes):
            if node.status == "COMPLETED" and node.rollback_token:
                result = registry.compensate_tool_action(node.rollback_token)
                report.append({"step_id": node.step_id, "tool_name": node.tool_name, "result": result})
                node.status = "ROLLED_BACK" if result.get("status") == "success" else "FAILED"
                self.gateway.log_event(user_id, "SAGA_COMPENSATION_ROLLBACK", node.tool_name, node.risk_level,
                                       {"dag_id": dag_id, "step_id": node.step_id, "reason": reason, "result": result},
                                       "SUCCESS" if result.get("status") == "success" else "ERROR")
            elif node.status == "PENDING":
                node.status = "ABORTED"
        task.overall_status = "ROLLED_BACK"
        _persist(task)
        rolled = [r["result"].get("summary", "") for r in report]
        reply = "任务已停止并按相反顺序完成补偿回滚。" + (" " + "；".join(rolled) if rolled else "")
        return self._response(task, "ROLLED_BACK", reply, True)


banking_agent = BankingAgent()

__all__ = ["DAGNode", "DAGTask", "LLMRouter", "BankingAgent", "banking_agent", "build_mermaid_graph"]
