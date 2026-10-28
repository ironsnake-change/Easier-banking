"""Windows desktop entry point for the bundled Easier-banking application."""

from __future__ import annotations

import json
import http.client
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit


APP_NAME = "Easier-banking"
BROWSER_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


def _app_data_dir() -> Path:
    base = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _configure_logging(log_path: Path) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(file_handler)
    root.setLevel(logging.INFO)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(url: str, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            payload = _http_json(url)
            if payload.get("status") == "ok":
                return True
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.15)
    logging.error("Health check timed out | url=%s | last_error=%s", url, last_error)
    return False


def _browser_path() -> Path | None:
    override = os.getenv("EASIER_BANKING_BROWSER", "").strip()
    if override and Path(override).is_file():
        return Path(override)
    return next((path for path in BROWSER_CANDIDATES if path.is_file()), None)


def _show_error(message: str) -> None:
    if os.getenv("EASIER_BANKING_SMOKE_TEST", "").strip() == "1":
        logging.error(message)
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    except Exception:
        print(message, file=sys.stderr)


def _fallback_window(url: str) -> None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("520x220")
    root.configure(bg="#101114")
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Easier-banking 正在运行", font=("Segoe UI", 16, "bold")).pack(anchor="w")
    ttk.Label(frame, text=url, foreground="#5f6368").pack(anchor="w", pady=(8, 18))
    import webbrowser
    ttk.Button(frame, text="打开银行工作台", command=lambda: webbrowser.open(url)).pack(anchor="w")
    ttk.Button(frame, text="退出应用", command=root.destroy).pack(anchor="w", pady=(8, 0))
    root.mainloop()


def _http_json(url: str, payload: dict | None = None, headers: dict[str, str] | None = None) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid local app URL: {url}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parsed.hostname, parsed.port, timeout=15.0)
    request_path = parsed.path or "/"
    if parsed.query:
        request_path += "?" + parsed.query
    connection.request(
        "POST" if data is not None else "GET",
        request_path,
        body=data,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}: {body[:300]}")
        return json.loads(body)
    finally:
        connection.close()


def _smoke_check(url: str) -> dict:
    health = _http_json(url + "/api/health")
    login = _http_json(
        url + "/api/auth/login",
        {"username_or_card": "linyan", "password": "Demo@2026"},
    )
    probe = _http_json(
        url + "/api/assistant/probe",
        {},
        {"X-Session-Token": login["session_token"]},
    )
    return {"health": health, "login_status": login.get("status"), "probe": probe}


def main() -> int:
    data_dir = _app_data_dir()
    log_path = data_dir / "desktop.log"
    _configure_logging(log_path)
    stream = open(log_path, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")
    logging.info("Desktop launcher starting | executable=%s | frozen=%s", sys.executable, getattr(sys, "frozen", False))
    os.environ.setdefault("EASIER_BANKING_DB_PATH", str(data_dir / "bank_core.db"))
    os.environ.setdefault("PYTHONUTF8", "1")

    try:
        logging.info("Importing uvicorn")
        import uvicorn
        import certifi
        logging.info("Importing application")
        from main import app
        ca_bundle = Path(certifi.where())
        logging.info(
            "CA bundle resolved | path=%s | exists=%s | env=%s",
            ca_bundle,
            ca_bundle.is_file(),
            os.getenv("SSL_CERT_FILE"),
        )
        if ca_bundle.is_file():
            os.environ["SSL_CERT_FILE"] = str(ca_bundle)
            os.environ["REQUESTS_CA_BUNDLE"] = str(ca_bundle)
            os.environ["CURL_CA_BUNDLE"] = str(ca_bundle)
        logging.info("Application imports completed")
    except BaseException as exc:
        logging.exception("Application import failed")
        _show_error(f"Easier-banking 启动失败。\n\n{exc}\n\n日志：{log_path}")
        return 1

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    logging.info("Starting local server on %s", url)
    try:
        server_config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            loop="asyncio",
            http="h11",
            ws="none",
            log_config=None,
            log_level="info",
            access_log=False,
        )
        server_config.load()
        logging.info("Uvicorn configuration loaded")
        server = uvicorn.Server(server_config)
    except BaseException:
        logging.exception("Unable to initialize the local ASGI server")
        _show_error(f"本地银行服务初始化失败。\n\n日志：{log_path}")
        return 1
    session_result = {"exit_code": 0}

    def run_desktop_session() -> None:
        logging.info("Waiting for local health check")
        if not _wait_until_ready(url + "/api/health"):
            session_result["exit_code"] = 1
            _show_error(f"本地银行服务未能启动。\n\n日志：{log_path}")
            server.should_exit = True
            return
        logging.info("Local server is ready")
        if os.getenv("EASIER_BANKING_SMOKE_TEST", "").strip() == "1":
            logging.info("Running packaged smoke check")
            try:
                smoke_result = _smoke_check(url)
            except Exception as exc:
                logging.exception("Packaged smoke check failed")
                smoke_result = {"status": "error", "error": str(exc)}
            result_path = Path(os.getenv("EASIER_BANKING_SMOKE_RESULT", str(data_dir / "smoke-result.json")))
            result_path.write_text(json.dumps(smoke_result, ensure_ascii=False, indent=2), encoding="utf-8")
            session_result["exit_code"] = 0 if smoke_result.get("probe", {}).get("status") == "ONLINE" else 2
            server.should_exit = True
            return

        browser = _browser_path()
        try:
            if browser:
                profile = data_dir / "browser-profile"
                profile.mkdir(parents=True, exist_ok=True)
                browser_process = subprocess.Popen(
                    [
                        str(browser),
                        f"--app={url}",
                        f"--user-data-dir={profile}",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-features=Translate",
                        "--window-size=1480,980",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                browser_process.wait()
            else:
                _fallback_window(url)
        except Exception:
            logging.exception("Desktop browser window failed; using the default browser")
            import webbrowser
            if not webbrowser.open(url):
                session_result["exit_code"] = 1
                _show_error(f"无法打开浏览器。请手动访问：\n{url}\n\n日志：{log_path}")
            else:
                _fallback_window(url)
        finally:
            server.should_exit = True

    control = threading.Thread(target=run_desktop_session, name="desktop-session", daemon=True)
    control.start()
    try:
        server.run()
    except BaseException:
        logging.exception("Local ASGI server failed")
        session_result["exit_code"] = 1
        server.should_exit = True
    logging.info("Local ASGI server stopped | started=%s | should_exit=%s", server.started, server.should_exit)
    control.join(timeout=8)
    return int(session_result["exit_code"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if exc.code not in (None, 0):
            logging.error("Desktop launcher exited with code %s", exc.code)
            logging.error(traceback.format_exc())
        raise
    except BaseException:
        try:
            logging.exception("Fatal desktop launcher error")
            failure_log = _app_data_dir() / "desktop.log"
            with failure_log.open("a", encoding="utf-8") as handle:
                handle.write(traceback.format_exc())
        finally:
            raise SystemExit(1)
