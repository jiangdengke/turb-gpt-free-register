# -*- coding: utf-8 -*-
"""
ChatGPT Auth 模块
处理 chatgpt.com 域名下的认证请求（步骤1-3）
"""
import json
import logging
from urllib.parse import urlencode, urlparse, parse_qs

from core.session import BrowserSession
from config import (
    OPENAI_CLIENT_ID, OPENAI_SCOPE, OPENAI_AUDIENCE, OPENAI_REDIRECT_URI
)

logger = logging.getLogger(__name__)


def _safe_url_for_log(value: object) -> str:
    """Keep OAuth state, codes, and query tokens out of logs."""
    try:
        parsed = urlparse(str(value or ""))
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
        return parsed.path or "<empty>"
    except Exception:
        return "<invalid-url>"


# 2026-09-14 Roxy 成功样本：signin 不再主动携带 passkey capabilities；
# authorize 使用两个 ccaps，并明确返回 ChatGPT 首页。
_CC_CAPS = "login_methods chatgpt_login_finalizer_v1"


def _reset_optional_circuit_breaker(session: BrowserSession) -> None:
    """清理可选发现接口触发的本地熔断，保留 Cookie 与会话上下文。"""
    reset = getattr(session, "reset_circuit_breaker", None)
    if callable(reset):
        reset()


def _ensure_authorize_context(authorize_url: str, session: BrowserSession, email: str) -> str:
    """
    对 NextAuth 返回的 authorize URL 做最后兜底：确保当前前端默认
    login_or_signup 链路的上下文参数没有在重定向生成阶段丢失。
    """
    try:
        parsed = urlparse(authorize_url)
        if not parsed.netloc.endswith("auth.openai.com"):
            return authorize_url
        params = parse_qs(parsed.query, keep_blank_values=True)
        # 旧实现主动注入该字段；当前成功浏览器 authorize 已不携带。
        changed = bool(params.pop("ext-passkey-client-capabilities", None))
        ui_locale = session.navigator_language()
        required = {
            "ext-oai-did": session.device_id,
            "auth_session_logging_id": session.auth_session_logging_id,
            "screen_hint": "login_or_signup",
            "login_hint": email,
            "ui_locales": ui_locale,
            "ccaps": _CC_CAPS,
            "auth_return_target_category": "chatgpt_home",
        }
        for key, value in required.items():
            if key in {"ccaps", "auth_return_target_category", "ui_locales"}:
                if params.get(key) != [value]:
                    params[key] = [value]
                    changed = True
            elif not params.get(key):
                params[key] = [value]
                changed = True
        if not changed:
            return authorize_url
        logger.info("[步骤3] authorize 上下文已对齐：ui_locales=%s", ui_locale)
        return parsed._replace(query=urlencode(params, doseq=True)).geturl()
    except Exception:
        return authorize_url


def get_providers(session: BrowserSession) -> dict:
    """
    步骤1: 获取 OAuth Providers 列表。
    GET https://chatgpt.com/api/auth/providers

    尝试获取可用的 OAuth 提供商。该发现接口不是 signin/CSRF 链路的前置依赖，
    Cloudflare 对它返回 403/429 时允许后续认证继续执行。

    Returns:
        providers 字典，例如；若发现接口被临时拦截则返回空字典:
        {
            "openai": {
                "id": "openai",
                "name": "openai",
                "type": "oauth",
                "signinUrl": "https://chatgpt.com/api/auth/signin/openai",
                "callbackUrl": "https://chatgpt.com/api/auth/callback/openai"
            },
            ...
        }
    """
    url = "https://chatgpt.com/api/auth/providers"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")

    logger.info("[步骤1] 获取 OAuth Providers...")
    resp = None
    try:
        resp = session.get(url, headers=headers)
        resp.raise_for_status()
    except Exception as exc:
        # `/api/auth/providers` 仅用于页面发现；实际 signin 请求不会读取它。
        # BrowserSession 会先记录 403/429 再抛出 HTTPError，必须清理本地熔断，
        # 否则紧接着的 CSRF 请求会在客户端直接被拦截。
        try:
            status = int(getattr(resp, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        if not status:
            try:
                status = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
            except (TypeError, ValueError):
                status = 0
        if status in (403, 429):
            _reset_optional_circuit_breaker(session)
            logger.warning(
                "[步骤1] OAuth Providers 返回 HTTP %s；该发现接口为可选项，"
                "已清理本地熔断并继续 CSRF/signin 链路",
                status,
            )
            return {}
        raise

    data = resp.json()
    logger.info(f"[步骤1] 成功获取 {len(data)} 个 providers: {list(data.keys())}")
    return data


def get_csrf_token(session: BrowserSession) -> str:
    """
    步骤2: 获取 CSRF Token。
    GET https://chatgpt.com/api/auth/csrf

    CSRF token 将在后续 signin 请求中使用。

    Returns:
        csrfToken 字符串
    """
    url = "https://chatgpt.com/api/auth/csrf"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")

    logger.info("[步骤2] 获取 CSRF Token...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()

    data = resp.json()
    csrf_token = data.get("csrfToken", "")
    logger.info("[步骤2] 获取 CSRF Token 成功")
    return csrf_token


def probe_auth_session(session: BrowserSession) -> dict:
    """按 Web 登录页顺序在 providers 之后读取一次匿名 NextAuth session。"""
    url = "https://chatgpt.com/api/auth/session"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")
    logger.info("[步骤1.5] 读取匿名 Auth Session...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def signin_openai(session: BrowserSession, csrf_token: str, email: str) -> str:
    """
    步骤3: 发起 OAuth Signin 请求。
    POST https://chatgpt.com/api/auth/signin/openai

    构造 OAuth 授权参数，获取 authorize URL。

    Args:
        session: 浏览器会话
        csrf_token: 从步骤2获取的 CSRF token
        email: 注册邮箱

    Returns:
        authorize_url: auth.openai.com 的授权 URL
    """
    # 构造 URL 查询参数
    query_params = {
        "prompt": "login",
        "ext-oai-did": session.device_id,
        "auth_session_logging_id": session.auth_session_logging_id,
        "screen_hint": "login_or_signup",
        "login_hint": email,
    }
    url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query_params)

    # 构造请求头
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"

    # 构造请求体
    body = urlencode({
        "callbackUrl": "/",
        "csrfToken": csrf_token,
        "json": "true",
    })

    logger.info(f"[步骤3] 发起 OAuth Signin 请求, 邮箱: {email}")
    resp = session.post(url, headers=headers, data=body)
    resp.raise_for_status()

    data = resp.json()
    authorize_url = data.get("url", "")

    if not authorize_url:
        raise ValueError(f"[步骤3] 未获取到 authorize URL, 响应: {data}")

    authorize_url = _ensure_authorize_context(authorize_url, session, email)
    logger.info("[步骤3] 获取 authorize URL 成功，已确认 login_or_signup/oai-did 上下文")
    logger.debug("[步骤3] authorize URL 路径: %s", _safe_url_for_log(authorize_url))
    return authorize_url
