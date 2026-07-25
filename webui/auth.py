# -*- coding: utf-8 -*-
"""WebUI 授权码登录与接口鉴权。"""
from __future__ import annotations

import hmac
import hashlib
import logging
import os
import secrets
from datetime import timedelta
from typing import Any

from flask import Response, jsonify, redirect, render_template, request, session, url_for

from core import db

logger = logging.getLogger(__name__)

AUTH_ENV_KEYS = ("WEBUI_AUTH_CODE", "AUTH_CODE", "WEB_AUTH_CODE")
_SESSION_KEY = "webui_auth_ok"
_SESSION_ROLE_KEY = "webui_auth_role"
_SESSION_SCANNER_ID_KEY = "webui_scanner_id"
_SESSION_SCANNER_NAME_KEY = "webui_scanner_name"
_SESSION_SCANNER_VERSION_KEY = "webui_scanner_key_version"
_AUTH_CODE: str | None = None
_GENERATED = False

def init_auth(app: Any, *, auth_code: str | None = None) -> str:
    """初始化授权码和 Flask session。未显式配置时生成临时授权码。"""
    global _AUTH_CODE, _GENERATED

    code = (auth_code or "").strip()
    if not code:
        try:
            from config.env_loader import load_env, env_str
            load_env(override=False)
            for key in AUTH_ENV_KEYS:
                code = env_str(key, "")
                if code:
                    break
        except Exception:
            for key in AUTH_ENV_KEYS:
                code = (os.getenv(key) or "").strip()
                if code:
                    break

    if not code:
        code = secrets.token_urlsafe(18)
        _GENERATED = True
    else:
        _GENERATED = False

    _AUTH_CODE = code
    session_secret = os.getenv("WEBUI_SESSION_SECRET") or os.getenv("FLASK_SECRET_KEY")
    if not session_secret:
        # 授权码来自 .env 时，用带命名空间的摘要生成稳定签名密钥；修改授权码会自然注销旧会话。
        session_secret = hashlib.sha256(f"turb-gpt-webui-session:{code}".encode("utf-8")).hexdigest()
    app.secret_key = session_secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    return code


def is_generated_code() -> bool:
    return _GENERATED


def expected_auth_code() -> str:
    return _AUTH_CODE or ""


def _extract_auth_code() -> str:
    # 非登录接口只接受 Header 授权码，避免 query/body 中的授权码进入日志、Referer 或业务数据。
    header_code = (request.headers.get("X-Auth-Code") or request.headers.get("X-Authorization-Code") or "").strip()
    if header_code:
        return header_code
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def code_is_valid(code: str) -> bool:
    expected = expected_auth_code()
    return bool(expected) and bool(code) and hmac.compare_digest(str(code), expected)


def _clear_auth_session() -> None:
    for key in (
        _SESSION_KEY,
        _SESSION_ROLE_KEY,
        _SESSION_SCANNER_ID_KEY,
        _SESSION_SCANNER_NAME_KEY,
        _SESSION_SCANNER_VERSION_KEY,
    ):
        session.pop(key, None)


def current_auth_role() -> str | None:
    """返回 admin/scanner；旧版布尔会话按管理员兼容。"""
    if code_is_valid(_extract_auth_code()):
        return "admin"
    if session.get(_SESSION_KEY) is not True:
        return None
    role = str(session.get(_SESSION_ROLE_KEY) or "admin")
    if role != "scanner":
        return "admin"
    scanner_id = session.get(_SESSION_SCANNER_ID_KEY)
    try:
        scanner = db.get_scanner(int(scanner_id))
    except (TypeError, ValueError):
        scanner = None
    if (
        scanner is None
        or not scanner.get("enabled")
        or int(scanner.get("key_version") or 1)
        != int(session.get(_SESSION_SCANNER_VERSION_KEY) or 0)
    ):
        _clear_auth_session()
        return None
    session[_SESSION_SCANNER_NAME_KEY] = scanner.get("name") or ""
    return "scanner"


def current_scanner_identity() -> dict | None:
    if current_auth_role() != "scanner":
        return None
    return {
        "id": int(session.get(_SESSION_SCANNER_ID_KEY) or 0),
        "name": session.get(_SESSION_SCANNER_NAME_KEY) or "",
    }


def request_is_authorized() -> bool:
    return current_auth_role() is not None


def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept") or ""
    return "application/json" in accept and "text/html" not in accept


def _unauthorized_response():
    if _wants_json():
        return jsonify({"ok": False, "error": "未授权：请先登录或提供授权码"}), 401
    return redirect(url_for("auth_login", next=request.path))


def _forbidden_response():
    if _wants_json():
        return jsonify({"ok": False, "error": "当前扫码员没有管理权限"}), 403
    return redirect("/scan")


def register_auth_routes(app: Any) -> None:
    @app.before_request
    def _require_auth_code():
        endpoint = request.endpoint or ""
        if endpoint in {"auth_login", "auth_logout", "static"}:
            return None
        if request.path in ("/favicon.ico",):
            return Response(status=204)
        role = current_auth_role()
        if role is None:
            return _unauthorized_response()
        if role == "scanner":
            scanner_path = request.path == "/scan" or request.path.startswith("/api/scan/")
            if not scanner_path:
                return _forbidden_response()
        return None

    @app.route("/login", methods=["GET", "POST"], endpoint="auth_login")
    def _auth_login():
        error = ""
        next_url = request.values.get("next") or "/"
        if not str(next_url).startswith("/") or str(next_url).startswith("//"):
            next_url = "/"
        if request.method == "POST":
            code = (request.form.get("auth_code") or "").strip()
            remember = (request.form.get("remember") or "").strip().lower() in ("1", "true", "on", "yes")
            if code_is_valid(code):
                _clear_auth_session()
                session.permanent = remember
                session[_SESSION_KEY] = True
                session[_SESSION_ROLE_KEY] = "admin"
                return redirect(next_url)
            scanner = db.authenticate_scanner_key(code)
            if scanner:
                _clear_auth_session()
                session.permanent = remember
                session[_SESSION_KEY] = True
                session[_SESSION_ROLE_KEY] = "scanner"
                session[_SESSION_SCANNER_ID_KEY] = int(scanner.get("id") or 0)
                session[_SESSION_SCANNER_NAME_KEY] = scanner.get("name") or ""
                session[_SESSION_SCANNER_VERSION_KEY] = int(scanner.get("key_version") or 1)
                return redirect("/scan")
            error = "授权码错误"
        return render_template("login.html", error=error, next_url=next_url, login_url=url_for("auth_login"))

    @app.post("/logout", endpoint="auth_logout")
    def _auth_logout():
        _clear_auth_session()
        if _wants_json():
            return jsonify({"ok": True})
        return redirect(url_for("auth_login"))
