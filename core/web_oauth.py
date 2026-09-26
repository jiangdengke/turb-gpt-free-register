"""Standard OpenAI PKCE login for a Web account-pool credential.

This is deliberately separate from the Codex/CPA credential path.  The
platform OAuth client and redirect URI are the same ones used by the target
Account Service OAuth bridge, so the resulting token set can be imported as a
``source_type=web`` account.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

logger = logging.getLogger(__name__)

_AUTH_BASE = "https://auth.openai.com"
_PLATFORM_BASE = "https://platform.openai.com"
_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
_REDIRECT_URI = f"{_PLATFORM_BASE}/auth/callback"
_AUDIENCE = "https://api.openai.com/v1"
_SCOPE = "openid profile email offline_access"
_AUTH0_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
_MAX_REDIRECTS = 15


@dataclass(frozen=True)
class WebOAuthCredential:
    """The token set and non-sensitive account metadata for one import."""

    access_token: str
    refresh_token: str
    id_token: str
    email: str
    user_id: str
    plan_type: str

    def as_import_payload(self) -> dict[str, str]:
        payload = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "email": self.email,
            "user_id": self.user_id,
            "type": self.plan_type,
            "source_type": "web",
        }
        return {key: value for key, value in payload.items() if str(value or "").strip()}


def _generate_web_pkce() -> tuple[str, str]:
    """Match chatgpt2api's standard platform OAuth PKCE verifier generation."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _is_web_callback_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except Exception:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname == "platform.openai.com"
        and parsed.port is None
        and parsed.path == "/auth/callback"
    )


def _build_authorize_url(session, state: str, code_challenge: str, email_hint: str = "") -> str:
    params = {
        "issuer": _AUTH_BASE,
        "client_id": _CLIENT_ID,
        "audience": _AUDIENCE,
        "redirect_uri": _REDIRECT_URI,
        "device_id": session.device_id,
        "screen_hint": "login_or_signup",
        "max_age": "0",
        "scope": _SCOPE,
        "response_type": "code",
        "response_mode": "query",
        "state": state,
        "nonce": secrets.token_urlsafe(32),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "auth0Client": _AUTH0_CLIENT,
    }
    if str(email_hint or "").strip():
        params["login_hint"] = str(email_hint).strip()
    return f"{_AUTH_BASE}/api/accounts/authorize?{urlencode(params)}"


def _bootstrap_authorize(session, auth_url: str) -> None:
    headers = session.get_auth_navigate_headers(
        referer="",
        user_initiated=True,
        target_origin=_AUTH_BASE,
    )
    response = session.get(auth_url, headers=headers, allow_redirects=True)
    response.raise_for_status()
    logger.info("[Web OAuth] 已建立标准 PKCE 授权会话")


def _follow_until_callback(session, url: str) -> str:
    current = str(url or "").strip()
    if current.startswith("/"):
        current = f"{_AUTH_BASE}{current}"
    for _hop in range(_MAX_REDIRECTS):
        if _is_web_callback_url(current):
            return current
        headers = session.get_auth_navigate_headers(
            referer=f"{_AUTH_BASE}/",
            user_initiated=True,
            target_origin=_AUTH_BASE,
        )
        response = session.get(current, headers=headers, allow_redirects=False)
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            raise RuntimeError(f"Web OAuth callback navigation failed: HTTP {status}")
        location = response.headers.get("location") or response.headers.get("Location")
        if not location:
            raise RuntimeError("Web OAuth flow stopped before the platform callback")
        current = urljoin(current, location)
    raise RuntimeError(f"Web OAuth callback navigation exceeded {_MAX_REDIRECTS} redirects")


def _callback_from_result(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    for value in (
        result.get("external_url"),
        result.get("continue_url"),
        result.get("redirect_url"),
        result.get("url"),
        page.get("external_url"),
        page.get("continue_url"),
        page.get("redirect_url"),
        page.get("url"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _exchange_code(session, code: str, state: str, code_verifier: str) -> WebOAuthCredential:
    callback_query = parse_qs(urlparse(code if code.startswith("http") else "").query) if code.startswith("http") else {}
    authorization_code = str((callback_query.get("code") or [code])[0]).strip()
    callback_state = str((callback_query.get("state") or [state])[0]).strip()
    if not authorization_code:
        raise RuntimeError("Web OAuth callback did not contain an authorization code")
    if callback_state and callback_state != state:
        raise RuntimeError("Web OAuth state mismatch")

    headers = session._get_common_headers()
    headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": _PLATFORM_BASE,
        "Referer": f"{_PLATFORM_BASE}/",
        "auth0-client": _AUTH0_CLIENT,
    })
    response = session.post(
        f"{_AUTH_BASE}/api/accounts/oauth/token",
        headers=headers,
        json={
            "client_id": _CLIENT_ID,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": _REDIRECT_URI,
        },
        allow_redirects=False,
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise RuntimeError(f"Web OAuth token exchange failed: HTTP {status}")
    try:
        token_data = response.json()
    except Exception as exc:
        raise RuntimeError("Web OAuth token exchange returned invalid JSON") from exc
    if not isinstance(token_data, dict):
        raise RuntimeError("Web OAuth token exchange returned an invalid payload")

    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    if not access_token:
        raise RuntimeError("Web OAuth token response omitted access_token")
    if not refresh_token:
        raise RuntimeError("Web OAuth token response omitted refresh_token")

    id_token = str(token_data.get("id_token") or "").strip()
    claims = {}
    try:
        parts = id_token.split(".")
        if len(parts) >= 2:
            from core import codex_oauth as proto
            claims = proto._decode_jwt_segment(parts[1])
    except Exception:
        claims = {}
    auth_claim = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    profile_claim = claims.get("https://api.openai.com/profile") if isinstance(claims.get("https://api.openai.com/profile"), dict) else {}
    email = str(claims.get("email") or profile_claim.get("email") or "").strip()
    user_id = str(claims.get("sub") or claims.get("user_id") or auth_claim.get("user_id") or "").strip()
    plan_type = str(auth_claim.get("chatgpt_plan_type") or claims.get("plan_type") or "").strip()
    return WebOAuthCredential(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        email=email,
        user_id=user_id,
        plan_type=plan_type,
    )


def obtain_web_oauth_credentials(
    email: str,
    *,
    otp_provider=None,
    proxy: str | None = None,
) -> WebOAuthCredential:
    """Run the existing protocol login and return a standard Web OAuth token set."""
    if not str(email or "").strip():
        raise ValueError("email is required for Web OAuth")
    if otp_provider is None:
        from core.email_provider import wait_for_otp as otp_provider
    from core import codex_oauth as proto
    from core.session import BrowserSession, close_browser_session

    code_verifier, code_challenge = _generate_web_pkce()
    state = proto._generate_state()
    task_seed = f"web-oauth:{str(email).lower()}:{secrets.token_urlsafe(12)}"
    session = BrowserSession(proxy=proxy, fingerprint_seed=task_seed)
    try:
        proto._codex_auth_preflight(session)
        auth_url = _build_authorize_url(session, state, code_challenge, email)
        _bootstrap_authorize(session, auth_url)

        otp_after_ts = time.time()
        auth_result = proto._submit_email(session, email)
        login_status, early_callback_url, auth_result = proto._try_password_mfa_login(
            session,
            email,
            state,
            auth_result,
            callback_matcher=_is_web_callback_url,
        )
        password_login_done = login_status == "logged_in"
        if not password_login_done and (
            login_status == "email_otp" or proto._is_email_otp_step(auth_result)
        ):
            for attempt in range(1, 4):
                try:
                    otp = otp_provider(email, after_ts=otp_after_ts)
                    auth_result = proto._submit_email_otp(session, otp)
                    break
                except Exception:
                    if attempt >= 3:
                        raise
                    otp_after_ts = time.time()
                    auth_result = proto._submit_email(session, email)
        if not password_login_done and proto._is_mfa_step(
            auth_result, proto._extract_continue_url(auth_result)
        ):
            auth_result = proto._complete_mfa_if_required(session, email, auth_result)
            continue_url = proto._extract_continue_url(auth_result)
            early_callback_url = (
                _follow_until_callback(session, continue_url)
                if continue_url and not _is_web_callback_url(continue_url)
                else continue_url
            ) or early_callback_url
        if proto._is_phone_step(auth_result):
            auth_result = proto._do_phone_verification(session)
            continue_url = proto._extract_continue_url(auth_result)
            early_callback_url = (
                _follow_until_callback(session, continue_url)
                if continue_url and not _is_web_callback_url(continue_url)
                else continue_url
            ) or early_callback_url

        callback = early_callback_url
        if callback and not _is_web_callback_url(callback):
            callback = _follow_until_callback(session, callback)
        if not callback:
            continue_url = _callback_from_result(auth_result)
            if not continue_url:
                raise RuntimeError("Web OAuth response did not provide a callback URL")
            callback = _follow_until_callback(session, continue_url)
        parsed = parse_qs(urlparse(callback).query)
        code = str((parsed.get("code") or [""])[0]).strip()
        returned_state = str((parsed.get("state") or [""])[0]).strip()
        if not code:
            error = str((parsed.get("error_description") or parsed.get("error") or [""])[0]).strip()
            raise RuntimeError(error or "Web OAuth callback omitted code")
        if returned_state and returned_state != state:
            raise RuntimeError("Web OAuth state mismatch")
        return _exchange_code(session, code, state, code_verifier)
    finally:
        try:
            close_browser_session(session)
        except Exception:
            logger.debug("[Web OAuth] session close failed", exc_info=True)
