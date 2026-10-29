from __future__ import annotations

import json
import re
import struct
from pathlib import Path

from main import public_config


ROOT = Path(__file__).resolve().parents[1]
STYLES = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def test_theme_tokens_and_system_default_are_defined():
    required_dark = {
        "--bg": "#0B0B0C",
        "--panel": "#131315",
        "--ink": "#EDEDED",
        "--accent": "#EDEDED",
        "--green": "#6FBF9A",
        "--button-bg": "#EDEDED",
        "--button-ink": "#0B0B0C",
        "--radius": "14px",
    }
    required_light = {
        "--bg": "#F6F6F4",
        "--panel": "#FFFFFF",
        "--ink": "#111111",
        "--accent": "#111111",
        "--green": "#1F8A5F",
        "--button-bg": "#111111",
        "--button-ink": "#FFFFFF",
    }
    for token, value in required_dark.items():
        assert f"{token}: {value};" in STYLES
    for token, value in required_light.items():
        assert f"{token}: {value};" in STYLES
    assert ':root[data-theme="light"]' in STYLES
    assert "prefers-color-scheme: light" in INDEX
    assert 'localStorage.getItem("easier_banking_theme_v3")' in INDEX


def _contrast(foreground: str, background: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4 for channel in channels]
        return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]
    first, second = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (first + .05) / (second + .05)


def test_core_text_tokens_meet_aa_contrast():
    for foreground, background in (
        ("#EDEDED", "#0B0B0C"),
        ("#C9C9CC", "#131315"),
        ("#8E8E93", "#131315"),
        ("#111111", "#F6F6F4"),
        ("#3A3A3D", "#FFFFFF"),
        ("#6B6B70", "#FFFFFF"),
    ):
        assert _contrast(foreground, background) >= 4.5


def test_gradients_are_limited_to_monochrome_card_and_chat_texture():
    lines = [line.strip() for line in STYLES.splitlines() if "gradient" in line]
    allowed_prefixes = (".chip", ".card-chip", ".bank-card", "mask-image:", "-webkit-mask-image:")
    assert len(lines) == 7
    for line in lines:
        assert line.startswith(allowed_prefixes), line
        for value in re.findall(r"#[0-9A-Fa-f]{6}", line):
            red, green, blue = (int(value[index:index + 2], 16) for index in (1, 3, 5))
            assert red == green == blue, line
        for red, green, blue in re.findall(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", line):
            assert red == green == blue, line
    assert "radial-gradient" not in STYLES
    assert "connectorFlow 1s" not in STYLES


def test_all_declared_text_sizes_are_at_least_12px():
    sizes = [int(value) for value in re.findall(r"font-size:\s*(\d+)px", STYLES)]
    assert sizes and min(sizes) >= 12


def test_chart_uses_theme_variables_and_redraws():
    assert 'getPropertyValue("--green")' in APP
    assert 'getPropertyValue("--gold")' in APP
    assert "startChartAnimation();" in APP
    assert "requestAnimationFrame(drawChart);" in APP


def test_html_ids_are_unique_and_eyebrows_are_allowed_section_labels():
    ids = re.findall(r'\bid="([^"]+)"', INDEX)
    assert len(ids) == len(set(ids))
    eyebrow_values = re.findall(r'class="eyebrow"[^>]*>([^<]+)<', INDEX)
    assert set(eyebrow_values) <= {
        "PRIVATE NET BANKING",
        "DIGITAL ONBOARDING",
        "PRIVATE BANKING ASSISTANT",
        "ACCOUNT OVERVIEW",
        "CASH FLOW · 2026",
        "PORTFOLIO & CARDS",
        "PREFERENCES",
        "AI ENGINE ROUTING CONSOLE",
    }
    assert 'class="eyebrow"' not in APP


def test_topbar_menu_demo_tools_and_short_tabs_are_wired():
    for element_id in ("accountPill", "themeToggle", "runtimeModeBadge", "profileMenu",
                       "elderToggleTop", "switchAccount", "logoutBtn", "demoTools",
                       "probeGateway", "simulateSaga", "resetDemo"):
        assert f'id="{element_id}"' in INDEX
    assert '<span id="cloudStatus" class="cloud-status-accessible checking">' in INDEX
    assert ">总览</button>" in INDEX
    assert ">统计</button>" in INDEX
    assert ">卡片与安全</button>" in INDEX
    assert ">设置</button>" in INDEX
    assert 'id="demoTools" class="demo-tools" hidden' in INDEX
    assert 'requestJSON("/api/public-config"' in APP
    assert "toggleProfileMenu" in APP
    assert 'event.target.closest(".profile-control")' in APP


def test_secondary_english_eyebrows_and_legacy_brand_names_are_absent():
    forbidden_labels = (
        "REVO" + "LUT POCKETS",
        "DAG · SAGA TRANSACTION",
        "SUBSCRIPTION RADAR",
        "SECURITY CORE",
        "ACCESSIBILITY",
        "INTERACTION & LIMITS",
    )
    combined = INDEX + "\n" + APP + "\n" + STYLES
    for label in forbidden_labels:
        assert label not in combined
    forbidden_names = re.compile("REVO" + r"LUT|Revo" + r"lut|微" + "众|朝朝" + "宝")
    offenders = []
    for directory in (ROOT / "static", ROOT / "core", ROOT / "tests"):
        for path in directory.rglob("*"):
            if path.suffix.lower() not in {".py", ".js", ".html", ".css"}:
                continue
            if forbidden_names.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_public_config_is_minimal_and_read_only():
    config = public_config()
    assert set(config) == {"demo_mode", "gateway_mode", "model"}
    serialized = json.dumps(config).lower()
    assert "secret" not in serialized
    assert "api_key" not in serialized


def test_public_config_switches_demo_tools_off(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    assert public_config()["demo_mode"] is False


def test_authorization_otp_and_message_contracts_remain_available():
    for element_id in ("modalBackdrop", "modalBody", "modalActions", "modalOtp"):
        assert element_id in INDEX + APP
    assert "runSensitiveAction" in APP
    assert "confirmProposalAuthorization" in APP
    assert "演示模式：验证码见提示" in APP
    assert '(isUser ? "" : "Easier-banking · ")' in APP
    assert "proposal-reasons" in APP
    user_bubble_rule = ".message-row.user .message-bubble { border: 1px solid var(--line);"
    assert user_bubble_rule in STYLES
    assert "background: var(--panel-raised)" in STYLES[STYLES.index(user_bubble_rule):STYLES.index(user_bubble_rule) + 180]


def _png_dimensions(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", payload[16:24])


def test_required_layout_screenshots_and_overflow_audit_exist():
    screenshots = ROOT / "docs" / "screenshots"
    expected = [
        "1280x720-dark-overview.png",
        "1280x720-dark-security.png",
        "1280x720-light-overview.png",
        "1280x720-light-security.png",
        "1366x768-dark-login.png",
        "1366x768-dark-overview.png",
        "1366x768-dark-analytics.png",
        "1366x768-dark-security.png",
        "1366x768-dark-settings.png",
        "1366x768-dark-authorization-modal.png",
        "1366x768-dark-otp-modal.png",
        "1366x768-light-login.png",
        "1366x768-light-overview.png",
        "1366x768-light-analytics.png",
        "1366x768-light-security.png",
        "1366x768-light-settings.png",
        "1366x768-light-authorization-modal.png",
        "1366x768-light-otp-modal.png",
        "1536x864-dark-overview.png",
        "1536x864-dark-security.png",
        "1536x864-light-overview.png",
        "1536x864-light-security.png",
        "1920x1080-dark-overview.png",
        "1920x1080-dark-security.png",
        "1920x1080-light-overview.png",
        "1920x1080-light-security.png",
    ]
    assert len(expected) == 26
    for name in expected:
        path = screenshots / name
        assert path.is_file(), name
        expected_width, expected_height = map(int, name.split("-", 1)[0].split("x"))
        assert _png_dimensions(path) == (expected_width, expected_height)
    audit = json.loads((screenshots / "layout-audit.json").read_text(encoding="utf-8"))
    assert len(audit) == 26
    assert all(item["scrollWidth"] <= item["innerWidth"] for item in audit)
    assert all(
        not item["workbench"] or item["workbench"]["right"] <= item["innerWidth"]
        for item in audit
    )
