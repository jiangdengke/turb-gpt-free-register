# -*- coding: utf-8 -*-
"""后台设置已保存 ChatGPT 账号密码的纯协议流程。

该模块与 ``core.mail_password_change`` 完全独立：后者修改邮箱供应商密码，
本模块只处理已经保存的 ChatGPT 账号。最终密码请求被硬性放在邮箱 OTP
验证成功之后，且密码不会写入日志、队列状态或 HTTP 响应。
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from config import email as _email_cfg
from core import db
from core.account_export import (
    _follow_reauth_with_retry,
    fetch_session,
    follow_oauth_callback,
)
from core.chatgpt_auth import get_csrf_token
from core.humanize import delay as human_delay
from core.openai_auth import (
    EmailOtpInvalidError,
    build_sentinel_header,
    request_sentinel_token,
    send_email_otp,
    validate_email_otp,
)
from core.proxy_utils import mask_proxy_url
from core.session import BrowserSession, close_browser_session

logger = logging.getLogger(__name__)

_CHATGPT_BASE = "https://chatgpt.com"
_AUTH_BASE = "https://auth.openai.com"
_ADD_PASSWORD_PATH = "/add-password/new-password"
_PASSWORD_ENDPOINT = f"{_AUTH_BASE}/api/accounts/password/add"
_PASSWORD_MIN_LENGTH = 12
_PASSWORD_MAX_LENGTH = 128
_PASSWORD_GENERATED_LENGTH = 24
_PASSWORD_SYMBOLS = "!@#$%^&*()-_=+[]{}:,.?"


def generate_strong_password(length: int = _PASSWORD_GENERATED_LENGTH) -> str:
    """Generate an independent password without logging or persisting it early."""
    size = max(_PASSWORD_MIN_LENGTH, min(_PASSWORD_MAX_LENGTH, int(length or _PASSWORD_GENERATED_LENGTH)))
    required = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice(_PASSWORD_SYMBOLS),
    ]
    alphabet = string.ascii_letters + string.digits + _PASSWORD_SYMBOLS
    required.extend(secrets.choice(alphabet) for _ in range(size - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)

_WORKERS = 1
_QUEUE_LIMIT = 32
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="chatgpt-password")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"chatgpt-password-{safe}.log"


def is_running(acc_id: int) -> bool:
    with _LOCK:
        return int(acc_id) in _RUNNING


def queue_settings() -> dict:
    with _LOCK:
        running = len(_RUNNING)
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT, "running": running}


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now().strftime("%H:%M:%S")
    with path.open("w" if clear else "a", encoding="utf-8") as handle:
        handle.write(f"{stamp} [INFO] {line}\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _validate_new_password(value: object) -> str:
    """Validate without echoing the supplied password in an error message."""
    password = str(value or "")
    if not (_PASSWORD_MIN_LENGTH <= len(password) <= _PASSWORD_MAX_LENGTH):
        raise ValueError("ChatGPT 密码长度必须为 12 到 128 个字符")
    if not any(char.islower() for char in password):
        raise ValueError("ChatGPT 密码必须包含小写字母")
    if not any(char.isupper() for char in password):
        raise ValueError("ChatGPT 密码必须包含大写字母")
    if not any(char.isdigit() for char in password):
        raise ValueError("ChatGPT 密码必须包含数字")
    if not any(not char.isalnum() for char in password):
        raise ValueError("ChatGPT 密码必须包含符号")
    return password


def _safe_error(exc: BaseException, secret: str | None = None) -> str:
    """Keep task errors useful while preventing the submitted password from leaking."""
    text = str(exc or "").replace("\n", " ").strip()
    if secret:
        for candidate in {secret, quote(secret, safe="")}:
            if candidate:
                text = text.replace(candidate, "<redacted>")
    text = re.sub(r"(?<!\d)\d{4,8}(?!\d)", "<redacted>", text)
    return f"{type(exc).__name__}: {text[:500]}" if text else type(exc).__name__


def _response_status(response) -> int:
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _response_json(response) -> dict:
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"响应不是有效 JSON: {_response_status(response)}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("响应 JSON 类型异常")
    return data


def _raise_for_response(response, stage: str) -> None:
    status = _response_status(response)
    if status >= 400:
        # 不拼接 response.text，避免第三方错误页回显敏感请求上下文。
        raise RuntimeError(f"{stage} HTTP {status}")


def _path_of(value: object) -> str:
    try:
        return urlparse(str(value or "")).path.rstrip("/") or "/"
    except Exception:
        return ""


def _is_email_verification_url(value: object) -> bool:
    return _path_of(value) == "/email-verification"


def _is_add_password_page(value: object) -> bool:
    # 只接受当前 authenticated add-password 路由；reset-password 是另一条
    # 忘记密码流程，不能因为页面名称相似就把它当作本操作的成功状态。
    return _path_of(value) == _ADD_PASSWORD_PATH


def _extract_continue_url(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    page = result.get("page") or {}
    page = page if isinstance(page, dict) else {}
    return str(
        result.get("continue_url")
        or result.get("external_url")
        or result.get("url")
        or page.get("continue_url")
        or page.get("external_url")
        or page.get("url")
        or ""
    ).strip()


def _extract_factor_id(result: dict | None, continue_url: str) -> str:
    if isinstance(result, dict):
        page = result.get("page") or {}
        page = page if isinstance(page, dict) else {}
        payload = page.get("payload") or {}
        if isinstance(payload, dict):
            factor_id = str(payload.get("factor_id") or "").strip()
            if factor_id:
                return factor_id
    if "/mfa-challenge/" in str(continue_url or ""):
        return str(continue_url).rstrip("/").rsplit("/", 1)[-1]
    return ""


def _account_totp_code(email: str) -> str:
    """Generate a current TOTP from the saved account secret without logging it."""
    try:
        account = db.get_account_by_email(email) or {}
        secret = str(account.get("totp_secret") or "").strip()
        if not secret:
            return ""
        import pyotp

        return pyotp.TOTP(secret).now()
    except Exception as exc:
        logger.warning("[ChatGPT密码] 读取账号 TOTP 失败：%s", type(exc).__name__)
        return ""


def _mfa_referer(factor_id: str) -> str:
    return f"{_AUTH_BASE}/mfa-challenge/{quote(str(factor_id or '').strip(), safe='')}"


def _mfa_issue_challenge(session: BrowserSession, factor_id: str) -> dict:
    headers = session.get_auth_headers(referer=_mfa_referer(factor_id))
    headers.pop("openai-sentinel-token", None)
    headers.pop("openai-sentinel-so-token", None)
    response = session.post(
        f"{_AUTH_BASE}/api/accounts/mfa/issue_challenge",
        headers=headers,
        data=json.dumps({"id": factor_id, "type": "totp", "force_fresh_challenge": False}),
        allow_redirects=False,
    )
    _raise_for_response(response, "MFA challenge")
    return _response_json(response)


def _mfa_verify(session: BrowserSession, factor_id: str, code: str) -> dict:
    headers = session.get_auth_headers(referer=_mfa_referer(factor_id))
    headers.pop("openai-sentinel-token", None)
    headers.pop("openai-sentinel-so-token", None)
    response = session.post(
        f"{_AUTH_BASE}/api/accounts/mfa/verify",
        headers=headers,
        data=json.dumps({"id": factor_id, "type": "totp", "code": code}),
        allow_redirects=False,
    )
    _raise_for_response(response, "MFA verify")
    return _response_json(response)


def _complete_add_password_mfa(session: BrowserSession, email: str, result: dict) -> dict:
    """Complete the account TOTP step returned after email OTP validation."""
    continue_url = _extract_continue_url(result)
    page = result.get("page") if isinstance(result, dict) else {}
    page = page if isinstance(page, dict) else {}
    page_type = str(page.get("type") or "")
    if page_type != "mfa_challenge" and "/mfa-challenge/" not in continue_url:
        return result

    factor_id = _extract_factor_id(result, continue_url)
    if not factor_id:
        raise RuntimeError("邮箱 OTP 后 MFA challenge 缺少 factor_id")
    code = _account_totp_code(email)
    if not code:
        raise RuntimeError("邮箱 OTP 后 MFA challenge 需要可用 TOTP")
    _mfa_issue_challenge(session, factor_id)
    mfa_result = _mfa_verify(session, factor_id, code)
    next_url = _extract_continue_url(mfa_result)
    if not next_url:
        raise RuntimeError("MFA 验证成功但没有 add-password continue_url")
    output = dict(mfa_result)
    output["continue_url"] = next_url
    output["_add_password_referer"] = _mfa_referer(factor_id)
    return output


def _absolute_auth_url(value: object) -> str:
    url = str(value or "").strip()
    if url.startswith("/"):
        return _AUTH_BASE + url
    return url


def _trigger_add_password(session: BrowserSession, email: str) -> str:
    """Start the authenticated add-password state machine."""
    csrf = get_csrf_token(session)
    if not csrf:
        raise RuntimeError("add-password CSRF 缺失")

    query = {
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "post_login_add_password": "true",
        "max_age": "0",
        "ext-oai-did": session.device_id,
    }
    url = f"{_CHATGPT_BASE}/api/auth/signin/openai?{urlencode(query)}"
    headers = session.get_nextauth_headers(referer=f"{_CHATGPT_BASE}/")
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = _CHATGPT_BASE
    body = urlencode({
        "callbackUrl": f"{_CHATGPT_BASE}/",
        "csrfToken": csrf,
        "json": "true",
    })
    response = session.post(url, headers=headers, data=body)
    _raise_for_response(response, "add-password signin")
    auth_url = str(_response_json(response).get("url") or "").strip()
    if not auth_url:
        raise RuntimeError("add-password signin 未返回 authorize URL")
    return auth_url


def _follow_add_password_page(session: BrowserSession, continue_url: str, referer: str) -> str:
    target = _absolute_auth_url(continue_url)
    if not target:
        raise RuntimeError("add-password continue_url 为空")
    parsed = urlparse(target)
    if parsed.scheme != "https" or parsed.netloc != urlparse(_AUTH_BASE).netloc:
        raise RuntimeError("add-password continue_url 目标异常")
    headers = session.get_auth_navigate_headers(referer=referer)
    response = session.get(target, headers=headers, allow_redirects=True)
    _raise_for_response(response, "add-password 页面导航")
    final_url = str(getattr(response, "url", "") or target)
    if not _is_add_password_page(final_url):
        raise RuntimeError(
            f"add-password 页面导航落点异常: path={_path_of(final_url)}"
        )
    return final_url


def _follow_add_password_authorize(session: BrowserSession, email: str, auth_url: str) -> str:
    """Follow the add-password authorize state, rebuilding a transient /error state once."""
    current_url = str(auth_url or "").strip()
    for attempt in range(1, 3):
        final_url = _follow_reauth_with_retry(session, current_url)
        if _is_email_verification_url(final_url):
            return final_url
        if _path_of(final_url) != "/error" or attempt >= 2:
            raise RuntimeError(
                f"add-password authorize 未进入邮箱 OTP 页面: path={_path_of(final_url)}"
            )
        logger.warning("[ChatGPT密码] add-password authorize 落到 error，重建认证状态 attempt=%s/2", attempt)
        reset = getattr(session, "reset_circuit_breaker", None)
        if callable(reset):
            reset()
        human_delay("api")
        current_url = _trigger_add_password(session, email)
    raise RuntimeError("add-password authorize 重建失败")


def _auth_session_dump(session: BrowserSession, referer: str) -> None:
    """Establish the auth-page state used by the current frontend before OTP."""
    url = f"{_AUTH_BASE}/api/accounts/client_auth_session_dump"
    response = session.get(
        url,
        headers=session.get_auth_headers(referer=referer),
        allow_redirects=True,
    )
    _raise_for_response(response, "auth session dump")


def _sentinel_headers(session: BrowserSession, flow: str) -> dict:
    challenge = request_sentinel_token(session, flow)
    token, so_token = build_sentinel_header(session, challenge, flow)
    if not token:
        raise RuntimeError(f"Sentinel flow {flow} 未返回 token")
    headers = {
        "openai-sentinel-token": token,
    }
    if so_token:
        headers["openai-sentinel-so-token"] = so_token
    return headers


def _is_invalid_otp_error(exc: BaseException) -> bool:
    if isinstance(exc, EmailOtpInvalidError):
        return True
    response = getattr(exc, "response", None)
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    text = str(exc or "").lower()
    return status in {400, 401, 422} and any(
        marker in text
        for marker in ("otp", "code", "invalid", "incorrect", "expired", "验证码")
    )


def _validate_add_password_otp(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    *,
    email_source: str | None = None,
    otp_code: str | None = None,
    max_attempts: int = 3,
) -> dict:
    """Read and validate the account email OTP before any password mutation."""
    from core.email_provider import wait_for_otp

    current_code = otp_code
    last_exc: BaseException | None = None
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        try:
            if current_code is None:
                current_code = wait_for_otp(
                    email,
                    after_ts=otp_after_ts,
                    email_source=email_source,
                )
            # The current Auth frontend sends both headers when the Session Observer
            # token is available. A fresh pair is used for each validation attempt.
            headers = _sentinel_headers(session, "email_otp_validate")
            result = validate_email_otp(
                session,
                current_code,
                sentinel_header=headers.get("openai-sentinel-token"),
                so_header=headers.get("openai-sentinel-so-token"),
            )
            if not isinstance(result, dict) or not result.get("continue_url"):
                raise RuntimeError("邮箱 OTP 验证成功但缺少 continue_url")
            return result
        except Exception as exc:
            last_exc = exc
            if not _is_invalid_otp_error(exc) or attempt >= max(1, int(max_attempts)):
                raise
            logger.warning("[ChatGPT密码] 邮箱 OTP 无效或过期，重新发送后重试 attempt=%s/%s", attempt, max_attempts)
            send_email_otp(session, referer="https://auth.openai.com/email-verification")
            otp_after_ts = time.time()
            current_code = None
            human_delay("api")
    raise last_exc if last_exc else RuntimeError("邮箱 OTP 验证失败")


def set_chatgpt_password(
    session: BrowserSession,
    email: str,
    password: str,
    *,
    email_source: str | None = None,
    otp_code: str | None = None,
    access_token: str | None = None,
) -> dict:
    """Run CSRF -> add-password auth -> email OTP -> password/add.

    ``otp_code`` exists only for deterministic tests/manual adapters. Production
    queue calls leave it unset and obtain the code from the configured email source.
    The final POST is reachable only after ``_validate_add_password_otp`` returns.
    """
    email = str(email or "").strip()
    password = _validate_new_password(password)
    if not email:
        raise ValueError("email 不能为空")

    if access_token:
        try:
            from core.chatgpt_bootstrap import authenticated_bootstrap
            authenticated_bootstrap(session, access_token, strict=False)
        finally:
            reset = getattr(session, "reset_circuit_breaker", None)
            if callable(reset):
                reset()

    auth_url = _trigger_add_password(session, email)
    otp_after_ts = time.time()
    final_url = _follow_add_password_authorize(session, email, auth_url)
    _auth_session_dump(session, final_url)
    logger.info("[ChatGPT密码] 已进入邮箱 OTP 页面，等待账号邮箱验证码")

    otp_result = _validate_add_password_otp(
        session,
        email,
        otp_after_ts,
        email_source=email_source,
        otp_code=otp_code,
    )
    otp_result = _complete_add_password_mfa(session, email, otp_result)
    continue_url = _extract_continue_url(otp_result)
    password_page_referer = str(
        otp_result.get("_add_password_referer")
        or f"{_AUTH_BASE}/email-verification"
    ).strip()
    if not password_page_referer.startswith(f"{_AUTH_BASE}/"):
        password_page_referer = f"{_AUTH_BASE}/email-verification"
    password_page_url = _follow_add_password_page(
        session,
        continue_url,
        referer=password_page_referer,
    )
    logger.info("[ChatGPT密码] 邮箱 OTP 已验证，进入 add-password 页面")

    # This is the sole password mutation request. It is deliberately below the
    # successful OTP validation and add-password page checks above.
    headers = session.get_auth_headers(referer=password_page_url)
    headers.update(_sentinel_headers(session, "password_reset"))
    response = session.post(
        _PASSWORD_ENDPOINT,
        headers=headers,
        data=json.dumps({"password": password}, separators=(",", ":")),
    )
    _raise_for_response(response, "ChatGPT 密码设置")
    result = _response_json(response)
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    if not result.get("continue_url") and page.get("type") != "external_url":
        raise RuntimeError("ChatGPT 密码设置响应未完成 add-password 流程")

    refreshed_session: dict | None = None
    callback_url = result.get("continue_url")
    if callback_url:
        try:
            follow_oauth_callback(
                session,
                _absolute_auth_url(callback_url),
                referer=password_page_url,
            )
            try:
                refreshed_session = fetch_session(session)
            except Exception as exc:
                logger.warning("[ChatGPT密码] 密码已提交，但刷新本地 session 失败：%s", type(exc).__name__)
        except Exception as exc:
            # The mutation already returned success. Callback refresh is best-effort
            # and must not turn a successful password change into a false failure.
            logger.warning("[ChatGPT密码] 密码已提交，但 OAuth callback 刷新失败：%s", type(exc).__name__)

    output = {"ok": True, "status": "success", "message": "ChatGPT 密码设置完成"}
    if refreshed_session:
        output["access_token"] = refreshed_session.get("accessToken")
        output["expires_at"] = refreshed_session.get("expires")
    return output


def _normalize_proxy(proxy: str | None) -> str | None:
    text = str(proxy or "").strip()
    if not text:
        return None
    if text.lower().startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")):
        return text
    return None


def _resolve_password_proxy(proxy: str | None):
    """Use an explicit saved proxy as the transport target; otherwise use the pool."""
    from core.proxy_chain import open_proxy_pool_proxy

    target = _normalize_proxy(proxy)
    transport, relay = open_proxy_pool_proxy(target)
    return transport or None, relay, "saved" if target else "pool"


def _run_password_task(
    *, account_id: int, email: str, password: str, access_token: str,
    email_source: str | None, proxy: str | None, trigger: str,
) -> dict:
    session: BrowserSession | None = None
    relay = None
    file_handler: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    result: dict = {"ok": False, "status": "failed"}
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_chatgpt_password_running(account_id):
            result["error"] = "账号已删除或 ChatGPT 密码任务已被重置"
            result["message"] = "ChatGPT 密码任务未执行"
            try:
                db.update_account_chatgpt_password(account_id, result)
            except Exception:
                logger.error("[ChatGPT密码] 写回未执行状态失败：account_id=%s", account_id)
            return result

        path = log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text("", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        file_handler = logging.FileHandler(str(path), encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        file_handler.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(file_handler)
        logger.info("[ChatGPT密码] 开始后台设置：account_id=%s trigger=%s", account_id, trigger)

        real_proxy, relay, proxy_source = _resolve_password_proxy(proxy)
        session = BrowserSession(proxy=real_proxy, fingerprint_seed=f"account:{email.strip().lower()}")
        logger.info(
            "[ChatGPT密码] 会话创建完成：target=%s transport=%s source=%s",
            mask_proxy_url(getattr(session, "proxy_target", None) or session.proxy or "direct") or "direct",
            mask_proxy_url(session.proxy or "direct") or "direct",
            proxy_source,
        )
        logger.info("[ChatGPT密码] 会话指纹已固定")
        result = set_chatgpt_password(
            session,
            email,
            password,
            email_source=email_source,
            access_token=access_token,
        )
        result["_password"] = password
        db.update_account_chatgpt_password(account_id, result)
        if result.get("access_token"):
            db.update_account_access_token(
                account_id,
                str(result.get("access_token") or ""),
                expires_at=result.get("expires_at"),
            )
        _append_log(email, "[ChatGPT密码] 完成：密码已写回账号 registration_password")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "error": _safe_error(exc, password),
            "message": "ChatGPT 密码设置失败",
        }
        try:
            db.update_account_chatgpt_password(account_id, result)
        except Exception:
            logger.error("[ChatGPT密码] 写回失败状态失败：account_id=%s", account_id)
        try:
            _append_log(email, f"[ChatGPT密码] 失败：{result['error']}")
        except Exception:
            pass
        logger.error("[ChatGPT密码] 后台异常：account_id=%s error=%s", account_id, result["error"])
        return result
    finally:
        if session is not None:
            try:
                close_browser_session(session)
            except Exception:
                pass
        if relay is not None:
            try:
                relay.close()
            except Exception:
                pass
        if file_handler is not None:
            try:
                root_logger.removeHandler(file_handler)
                file_handler.close()
            except Exception:
                pass
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def enqueue_account_chatgpt_password(
    *, account_id: int, email: str, password: str, access_token: str,
    email_source: str | None = None, trigger: str = "manual", proxy: str | None = None,
) -> dict:
    """Queue a user-triggered password setting operation."""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    try:
        password = _validate_new_password(password)
    except ValueError as exc:
        return {"accepted": False, "busy": False, "error": str(exc)}
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not access_token:
        return {"accepted": False, "busy": False, "error": "缺少 access_token，请先查活刷新 AT"}
    try:
        existing = db.get_account(account_id) or {}
        if db._extract_registration_password(existing):
            return {"accepted": False, "busy": False, "error": "该账号已有 ChatGPT 密码，无需再次设置"}
    except Exception:
        pass
    with _LOCK:
        if account_id in _RUNNING:
            return {"accepted": False, "busy": True, "error": "该账号正在设置 ChatGPT 密码"}
    if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
        return {"accepted": False, "busy": False, "error": "设置 ChatGPT 密码需要先开启 USE_EMAIL_SERVICE 自动收取邮箱验证码"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "ChatGPT 密码队列已满，请稍后重试"}
    if not db.claim_account_chatgpt_password(account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在设置 ChatGPT 密码"}

    try:
        _append_log(email, f"[ChatGPT密码] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    except Exception as exc:
        _QUEUE_SLOTS.release()
        db.update_account_chatgpt_password(
            account_id,
            {"ok": False, "status": "failed", "error": _safe_error(exc, password)},
        )
        return {"accepted": False, "busy": False, "error": "ChatGPT 密码日志初始化失败"}
    try:
        future = _EXECUTOR.submit(
            _run_password_task,
            account_id=account_id,
            email=email,
            password=password,
            access_token=access_token,
            email_source=email_source,
            proxy=proxy,
            trigger=str(trigger or "manual"),
        )
        return {"accepted": True, "busy": False, "future": future, "log_path": str(log_path(email))}
    except Exception as exc:
        _QUEUE_SLOTS.release()
        db.update_account_chatgpt_password(
            account_id,
            {"ok": False, "status": "failed", "error": _safe_error(exc, password)},
        )
        return {"accepted": False, "busy": False, "error": "ChatGPT 密码任务入队失败"}
