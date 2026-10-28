"""Minimal PII redaction shared by every outbound model client."""

from __future__ import annotations

import re


def redact_sensitive_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(
        r"(?<!\d)\d{4,8}[\s*-]*\*{4,}[\s-]*(\d{4})(?!\d)",
        lambda match: "****" + match.group(1),
        value,
    )
    value = re.sub(
        r"(?<!\d)(?:\d[\s-]?){12,19}(?!\d)",
        lambda match: "****" + re.sub(r"\D", "", match.group(0))[-4:],
        value,
    )
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已脱敏]", value)
    value = re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)", "[证件号已脱敏]", value)
    return value


__all__ = ["redact_sensitive_text"]
