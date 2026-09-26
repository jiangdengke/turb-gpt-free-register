"""Durable, redacted outbox for importing Web accounts into chatgpt2api."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

from config import chatgpt2api as _cfg
from core import db

logger = logging.getLogger(__name__)


class ChatGPT2APIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, retryable: bool = True):
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.retryable = bool(retryable)


def _base_url() -> str:
    value = str(getattr(_cfg, "CHATGPT2API_BASE_URL", "") or "").strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ChatGPT2APIError("chatgpt2api base URL is not configured", retryable=False)
    return value


def _read_json_response(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except Exception as exc:
        raise ChatGPT2APIError("chatgpt2api returned invalid JSON", retryable=True) from exc
    return payload if isinstance(payload, dict) else {}


def _safe_error(exc: Exception, credentials: dict | None = None) -> str:
    text = f"{type(exc).__name__}: {str(exc)[:600]}"
    secrets_to_redact = [str(getattr(_cfg, "CHATGPT2API_MANAGEMENT_KEY", "") or "")]
    if isinstance(credentials, dict):
        secrets_to_redact.extend(str(value or "") for value in credentials.values())
    for secret in secrets_to_redact:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"https?://[^\s'\"]+", "[redacted-url]", text)
    text = re.sub(
        r"(?i)\b(access_token|refresh_token|id_token|authorization|code|state|token|secret)=([^&\s,]+)",
        r"\1=[redacted]",
        text,
    )
    return text[:1000]


def _parse_import_result(result: dict) -> dict:
    updated_ids = [
        str(item).strip()
        for item in (result.get("updated_ids") or [])
        if str(item).strip()
    ]
    return {
        "added": int(result.get("added") or 0),
        "skipped": int(result.get("skipped") or 0),
        "synced": int(result.get("synced") or 0),
        "updated_ids": updated_ids,
        "management_id": updated_ids[0] if updated_ids else "",
        "errors": [
            {"code": str(item.get("code") or "error"), "error": str(item.get("error") or "")[:300]}
            for item in (result.get("errors") or [])
            if isinstance(item, dict)
        ],
    }


def _request_json(path: str, body: dict) -> dict:
    key = str(getattr(_cfg, "CHATGPT2API_MANAGEMENT_KEY", "") or "").strip()
    if not key:
        raise ChatGPT2APIError("chatgpt2api management key is not configured", retryable=False)
    request = Request(
        f"{_base_url()}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "turb-gpt-free-register/chatgpt2api-import",
        },
        method="POST",
    )
    timeout = max(1, int(getattr(_cfg, "CHATGPT2API_REQUEST_TIMEOUT", 60) or 60))
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read()
    except HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        retryable = status == 429 or status >= 500 or status == 0
        raise ChatGPT2APIError(
            f"chatgpt2api request rejected: HTTP {status}",
            status_code=status,
            retryable=retryable,
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ChatGPT2APIError(
            f"chatgpt2api request network failure: {type(exc).__name__}",
            retryable=True,
        ) from exc

    if status < 200 or status >= 300:
        raise ChatGPT2APIError(
            f"chatgpt2api request rejected: HTTP {status}",
            status_code=status,
            retryable=status == 429 or status >= 500,
        )
    return _parse_import_result(_read_json_response(raw))



def test_connection() -> dict:
    """Check the loopback Account Service without creating or importing an account."""
    try:
        base_url = _base_url()
    except ChatGPT2APIError:
        return {
            "ok": False,
            "status": "missing_base_url",
            "message": "未配置有效的 chatgpt2api 地址",
        }

    key = str(getattr(_cfg, "CHATGPT2API_MANAGEMENT_KEY", "") or "").strip()
    if not key:
        return {
            "ok": False,
            "status": "missing_key",
            "message": "未配置 chatgpt2api 管理密钥",
        }

    request = Request(
        f"{base_url}/api/accounts?page=1&page_size=1",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "turb-gpt-free-register/chatgpt2api-connection-check",
        },
        method="GET",
    )
    timeout = max(1, int(getattr(_cfg, "CHATGPT2API_REQUEST_TIMEOUT", 60) or 60))
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
    except HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        try:
            exc.close()
        except Exception:
            pass
        if status in (401, 403):
            return {
                "ok": False,
                "status": "invalid_key",
                "http_status": status,
                "message": f"chatgpt2api 管理密钥无效（HTTP {status}）",
            }
        if status == 404:
            return {
                "ok": False,
                "status": "endpoint_not_found",
                "http_status": status,
                "message": "chatgpt2api 接口地址不存在（HTTP 404），请确认填写的是 Account Service 地址",
            }
        if status == 429 or status >= 500:
            return {
                "ok": False,
                "status": "service_unavailable",
                "http_status": status,
                "message": f"chatgpt2api 服务暂时不可用（HTTP {status}）",
            }
        return {
            "ok": False,
            "status": "http_error",
            "http_status": status,
            "message": f"chatgpt2api 接口返回 HTTP {status}",
        }
    except (URLError, TimeoutError, OSError):
        return {
            "ok": False,
            "status": "unreachable",
            "message": "chatgpt2api 地址不可达，请检查服务、端口和本机地址",
        }
    except ValueError:
        return {
            "ok": False,
            "status": "invalid_base_url",
            "message": "chatgpt2api 地址格式无效",
        }

    if 200 <= status < 300:
        return {
            "ok": True,
            "status": "connected",
            "http_status": status,
            "message": "连接成功，chatgpt2api 管理密钥有效",
        }
    return {
        "ok": False,
        "status": "http_error",
        "http_status": status,
        "message": f"chatgpt2api 接口返回 HTTP {status}",
    }


def _request_import(payload: dict) -> dict:
    return _request_json("/api/accounts", {
        "accounts": [dict(payload)],
        "sync_after_import": bool(getattr(_cfg, "CHATGPT2API_SYNC_AFTER_IMPORT", True)),
        "return_items": False,
    })


def _request_update(management_id: str, payload: dict) -> dict:
    body = {
        "id": str(management_id or "").strip(),
        "access_token": str(payload.get("access_token") or "").strip(),
        "refresh_token": str(payload.get("refresh_token") or "").strip(),
        "type": str(payload.get("type") or "").strip() or None,
        "source_type": "web",
    }
    body = {key: value for key, value in body.items() if value not in (None, "")}
    return _request_json("/api/accounts/update", body)


def _normalize_credential_mode(value: object) -> str:
    mode = str(value or "session_json").strip().lower()
    return "session_json" if mode in {"session", "session_json"} else mode


def _credential_payload(
    *,
    access_token: str,
    email: str,
    session_info: dict | None = None,
    refresh_token: str = "",
    id_token: str = "",
    user_id: str = "",
    plan_type: str = "",
) -> dict:
    """Build the normalized payload produced by chatgpt2api's Session JSON importer."""
    session = session_info if isinstance(session_info, dict) else {}
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    token = str(access_token or "").strip()
    values = {
        "access_token": token,
        "user": dict(user),
        "account": dict(account),
        "expires": str(session.get("expires") or "").strip(),
        "email": str(email or user.get("email") or "").strip(),
        "user_id": str(user_id or user.get("id") or "").strip(),
        "type": str(plan_type or account.get("planType") or "").strip(),
        "source_type": "web",
    }
    if refresh_token:
        values["refresh_token"] = str(refresh_token).strip()
    if id_token:
        values["id_token"] = str(id_token).strip()
    if not token:
        raise ValueError("Web account import requires access_token")
    return {
        key: value
        for key, value in values.items()
        if value not in (None, "", {}, [])
    }


def _public_status(account_id: int, *, default: dict | None = None) -> dict:
    row = db.get_account_pool_import_status(account_id) or (default or {})
    return {
        "status": str(row.get("status") or "pending"),
        "ok": str(row.get("status") or "") == "success",
        "account_id": int(account_id),
        "management_id": str(row.get("management_id") or "") or None,
        "attempts": int(row.get("attempts") or 0),
        "message": str(row.get("last_error") or "") or None,
    }


def _retry_at(attempts: int) -> float:
    base = max(1, int(getattr(_cfg, "CHATGPT2API_RETRY_DELAY", 15) or 15))
    return time.time() + min(base * (2 ** max(0, min(int(attempts) - 1, 8))), 3600)


def _account_session_metadata(account_id: int) -> tuple[dict, str]:
    account = db.get_account(account_id) or {}
    extra = account.get("extra_json")
    if isinstance(extra, str) and extra.strip():
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    extra = extra if isinstance(extra, dict) else {}
    return {
        "user": extra.get("user") if isinstance(extra.get("user"), dict) else {},
        "account": extra.get("account") if isinstance(extra.get("account"), dict) else {},
    }, str(account.get("proxy_used") or "").strip()


def _prepare_credentials(row: dict, credentials: dict) -> dict:
    """Obtain standard Web OAuth credentials only once, then retry from SQLite."""
    mode = _normalize_credential_mode(row.get("credential_mode"))
    if mode != "oauth_pkce" or str(credentials.get("refresh_token") or "").strip():
        return credentials

    from core.web_oauth import obtain_web_oauth_credentials

    account_id = int(row.get("account_id") or 0)
    metadata, proxy = _account_session_metadata(account_id)
    requested_email = str(row.get("email") or "").strip()
    oauth = obtain_web_oauth_credentials(
        requested_email,
        proxy=proxy or None,
    )
    oauth_email = str(oauth.email or "").strip()
    if oauth_email and requested_email and oauth_email.casefold() != requested_email.casefold():
        raise RuntimeError("Web OAuth returned a different account email")
    payload = oauth.as_import_payload()
    fallback = _credential_payload(
        access_token=str(credentials.get("access_token") or ""),
        email=requested_email,
        session_info=metadata,
    )
    for key in ("email", "user_id", "type"):
        if fallback.get(key) and not payload.get(key):
            payload[key] = fallback[key]
    db.save_account_pool_import_credentials(account_id, requested_email, payload)
    return payload


def _process_claimed(row: dict) -> dict:
    account_id = int(row.get("account_id") or 0)
    credentials = db.get_account_pool_import_credentials(account_id)
    attempts = int(row.get("attempts") or 1)
    try:
        credentials = _prepare_credentials(row, credentials)
        if not credentials.get("access_token"):
            raise ChatGPT2APIError("import credentials are missing", retryable=True)
        management_id = str(row.get("management_id") or "").strip()
        result = (
            _request_update(management_id, credentials)
            if management_id
            else _request_import(credentials)
        )
    except ChatGPT2APIError as exc:
        remote_account_missing = bool(exc.status_code == 404 and str(row.get("management_id") or "").strip())
        if remote_account_missing:
            db.clear_account_pool_import_management_id(account_id)
        next_attempt = _retry_at(attempts) if (exc.retryable or remote_account_missing) else time.time() + 86400
        db.finish_account_pool_import(
            account_id,
            status="failed",
            error=("remote management id was not found; scheduling a fresh import" if remote_account_missing else _safe_error(exc, credentials)),
            next_attempt_at=next_attempt,
        )
        logger.warning(
            "[chatgpt2api] Web account import failed: account_id=%s status=%s retryable=%s",
            account_id,
            exc.status_code or "local",
            exc.retryable,
        )
        return _public_status(account_id)
    except Exception as exc:
        db.finish_account_pool_import(
            account_id,
            status="failed",
            error=_safe_error(exc, credentials),
            next_attempt_at=_retry_at(attempts),
        )
        logger.warning("[chatgpt2api] Web account import failed: account_id=%s error=%s", account_id, type(exc).__name__)
        return _public_status(account_id)

    management_id = str(result.get("management_id") or "").strip()
    if not management_id:
        db.finish_account_pool_import(
            account_id,
            status="failed",
            error="chatgpt2api did not return an account management id",
            next_attempt_at=_retry_at(attempts),
        )
        return _public_status(account_id)
    updated = db.finish_account_pool_import(
        account_id,
        status="success",
        management_id=management_id,
        error=None,
        next_attempt_at=0.0,
    )
    if updated:
        logger.info(
            "[chatgpt2api] Web account imported: account_id=%s management_id=%s added=%s skipped=%s synced=%s",
            account_id,
            management_id,
            result.get("added", 0),
            result.get("skipped", 0),
            result.get("synced", 0),
        )
    else:
        logger.info(
            "[chatgpt2api] Web account import result superseded by a newer queued update: account_id=%s",
            account_id,
        )
    return _public_status(account_id)


def process_due_imports(limit: int = 20) -> list[dict]:
    results = []
    for row in db.list_due_account_pool_imports(limit=limit):
        account_id = int(row.get("account_id") or 0)
        if not account_id or not db.claim_account_pool_import(account_id):
            continue
        results.append(_process_claimed({**row, "attempts": int(row.get("attempts") or 0) + 1}))
    return results


_WORKER_LOCK = threading.Lock()
_WORKER_WAKE = threading.Event()
_WORKER: threading.Thread | None = None


def _worker_loop() -> None:
    while True:
        try:
            process_due_imports()
        except Exception:
            logger.exception("[chatgpt2api] import worker iteration failed")
        _WORKER_WAKE.wait(timeout=15.0)
        _WORKER_WAKE.clear()


def start_import_worker() -> None:
    """Start the daemon and resume due outbox rows after a WebUI restart."""
    global _WORKER
    if not bool(getattr(_cfg, "ENABLE_CHATGPT2API_IMPORT", False)):
        return
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            recovered = db.recover_account_pool_imports()
            if recovered:
                logger.info("[chatgpt2api] Requeued %s interrupted Web account imports", recovered)
            _WORKER = threading.Thread(target=_worker_loop, name="chatgpt2api-import", daemon=True)
            _WORKER.start()
    _WORKER_WAKE.set()


def enqueue_registered_account(
    *,
    account_id: int,
    email: str,
    access_token: str,
    session_info: dict | None = None,
    proxy: str | None = None,
) -> dict:
    """Persist a redacted outbox reference; the worker performs OAuth/import."""
    del proxy  # The worker reads the persisted account proxy only for OpenAI OAuth.
    if not bool(getattr(_cfg, "ENABLE_CHATGPT2API_IMPORT", False)):
        return {"status": "skipped", "ok": False, "account_id": int(account_id), "message": "disabled"}
    key = str(getattr(_cfg, "CHATGPT2API_MANAGEMENT_KEY", "") or "").strip()
    if not key:
        return {"status": "failed", "ok": False, "account_id": int(account_id), "message": "management key is not configured"}

    mode = _normalize_credential_mode(
        getattr(_cfg, "CHATGPT2API_CREDENTIAL_MODE", "session_json")
    )
    if mode not in {"oauth_pkce", "session_json"}:
        return {
            "status": "failed",
            "ok": False,
            "account_id": int(account_id),
            "message": "credential mode must be session_json or oauth_pkce",
        }
    try:
        existing = db.get_account_pool_import_credentials(account_id)
        payload = existing if mode == "oauth_pkce" and existing.get("refresh_token") else _credential_payload(
            access_token=access_token,
            email=email,
            session_info=session_info,
        )
        db.save_account_pool_import_credentials(account_id, email, payload)
        row = db.enqueue_account_pool_import(account_id, email, mode)
    except Exception as exc:
        logger.warning(
            "[chatgpt2api] could not queue Web account import: account_id=%s error=%s",
            account_id,
            type(exc).__name__,
        )
        return {
            "status": "failed",
            "ok": False,
            "account_id": int(account_id),
            "message": _safe_error(exc),
        }

    start_import_worker()
    _WORKER_WAKE.set()
    return _public_status(account_id, default=row)


def get_import_status(account_id: int) -> dict:
    return _public_status(account_id)
