"""Local Codex credential conversion for CPA and Sub2API exports.

The WebUI stores Codex OAuth credentials in SQLite.  This module converts a
complete local credential into the two file shapes documented by the
chatgpt-specimen-toolbox reference implementation:

* CPA: a flat ``type=codex`` auth file;
* Sub2API: an ``exported_at / proxies / accounts`` package containing an
  OAuth account.

The converter is deliberately local-only.  It never contacts CPA or Sub2API,
never logs token values, and refuses callback-only records that do not contain
an access token.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping


class CodexExportError(ValueError):
    """Raised when a local record cannot be converted safely."""


def validate_cpa_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the flat CPA auth-file contract and return the same object."""
    if not isinstance(document, Mapping):
        raise CodexExportError("CPA 导出结果不是 JSON 对象")
    required = (
        "type", "account_id", "chatgpt_account_id", "id_token", "access_token",
        "refresh_token", "email", "name", "plan_type", "chatgpt_plan_type",
        "last_refresh", "expired",
    )
    missing = [key for key in required if key not in document]
    if missing:
        raise CodexExportError("CPA 导出结果缺少字段: " + ", ".join(missing))
    if document.get("type") != "codex":
        raise CodexExportError("CPA 导出结果 type 必须是 codex")
    for key in ("account_id", "chatgpt_account_id", "id_token", "access_token"):
        if not _text(document.get(key)):
            raise CodexExportError(f"CPA 导出结果 {key} 为空")
    if "id_token_synthetic" in document and not isinstance(document["id_token_synthetic"], bool):
        raise CodexExportError("CPA 导出结果 id_token_synthetic 必须是布尔值")
    return dict(document)


def validate_sub2api_account(account: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one Sub2API OAuth account entry."""
    if not isinstance(account, Mapping):
        raise CodexExportError("Sub2API account 不是 JSON 对象")
    required = ("name", "platform", "type", "concurrency", "priority", "credentials", "extra")
    missing = [key for key in required if key not in account]
    if missing:
        raise CodexExportError("Sub2API account 缺少字段: " + ", ".join(missing))
    if account.get("platform") != "openai" or account.get("type") != "oauth":
        raise CodexExportError("Sub2API account 必须是 openai/oauth")
    credentials = account.get("credentials")
    if not isinstance(credentials, Mapping):
        raise CodexExportError("Sub2API credentials 不是 JSON 对象")
    for key in ("access_token", "chatgpt_account_id"):
        if not _text(credentials.get(key)):
            raise CodexExportError(f"Sub2API credentials {key} 为空")
    if not isinstance(account.get("extra"), Mapping):
        raise CodexExportError("Sub2API extra 不是 JSON 对象")
    return dict(account)


def validate_sub2api_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the Sub2API export envelope."""
    if not isinstance(document, Mapping):
        raise CodexExportError("Sub2API 导出结果不是 JSON 对象")
    for key in ("exported_at", "proxies", "accounts"):
        if key not in document:
            raise CodexExportError("Sub2API 导出结果缺少字段: " + key)
    if not isinstance(document.get("proxies"), list) or not isinstance(document.get("accounts"), list):
        raise CodexExportError("Sub2API proxies/accounts 必须是数组")
    for account in document["accounts"]:
        validate_sub2api_account(account)
    return dict(document)


_PLAN_NAMES = {"free", "plus", "team", "pro", "enterprise", "go"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _b64url_json(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying its signature.

    Token verification is not the responsibility of an export formatter.  A
    malformed/opaque token simply produces an empty claim set; the original
    token is never changed.
    """
    parts = _text(token).split(".")
    if len(parts) < 2:
        return {}
    try:
        encoded = parts[1]
        encoded += "=" * (-len(encoded) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _auth_claims(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(payload.get("https://api.openai.com/auth"))


def _profile_claims(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(payload.get("https://api.openai.com/profile"))


def _epoch(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return int(number)


def _iso_from_epoch(value: Any) -> str:
    timestamp = _epoch(value)
    if timestamp is None:
        return ""
    # Some imported records store epoch milliseconds.  Normalize them before
    # constructing a datetime, and treat unrepresentable metadata as absent.
    if timestamp > 100_000_000_000:
        timestamp //= 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def _epoch_from_time(value: Any) -> int | None:
    timestamp = _epoch(value)
    if timestamp is not None and timestamp > 100000000:
        return timestamp
    text = _text(value)
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None
    return timestamp


def _normalize_expiry(*values: Any) -> tuple[str, int | None]:
    for value in values:
        if value is None or value == "":
            continue
        epoch = _epoch_from_time(value)
        if epoch is not None:
            return _iso_from_epoch(epoch), epoch
        text = _text(value)
        if text:
            return text, None
    return "", None


def _email_key(email: str) -> str:
    return re.sub(r"^_+|_+$", "", re.sub(r"[^a-z0-9]+", "_", _text(email).lower()))


def _synthetic_id_token(
    *,
    account_id: str,
    user_id: str,
    plan_type: str,
    email: str,
    expires_epoch: int | None,
    now_epoch: int,
) -> str:
    """Build the same minimal synthetic id_token shape as the reference tool."""
    auth: dict[str, Any] = {"chatgpt_account_id": account_id}
    if plan_type:
        auth["chatgpt_plan_type"] = plan_type
    if user_id:
        auth["chatgpt_user_id"] = user_id
        auth["user_id"] = user_id
    payload: dict[str, Any] = {
        "iat": now_epoch,
        "exp": expires_epoch or now_epoch + 90 * 24 * 60 * 60,
        "https://api.openai.com/auth": auth,
    }
    if email:
        payload["email"] = email
    return f"{_b64url_json({'alg': 'none', 'typ': 'JWT', 'cpa_synthetic': True})}.{_b64url_json(payload)}.synthetic"


def _candidate_maps(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Return common flat/nested credential maps without traversing arbitrary data."""
    tokens = _mapping(payload.get("tokens"))
    credentials = _mapping(payload.get("credentials"))
    extra = _mapping(payload.get("extra"))
    candidates: list[Mapping[str, Any]] = [payload, tokens, credentials, extra]
    for key in (
        "auth_json", "authJson", "auth", "auth_file", "authFile", "file", "data",
        "cpa_submit_response", "sub2_submit_response",
    ):
        nested = _mapping(payload.get(key))
        if nested:
            candidates.append(nested)
            candidates.extend((_mapping(nested.get("tokens")), _mapping(nested.get("credentials"))))
    return tuple(candidates)


def _from_maps(maps: tuple[Mapping[str, Any], ...], *keys: str) -> str:
    for item in maps:
        for key in keys:
            value = _text(item.get(key))
            if value:
                return value
    return ""


def _plan_from_filename(filename: str) -> str:
    stem = _text(filename).removesuffix(".json")
    suffix = stem.rsplit("-", 1)[-1].lower() if "-" in stem else ""
    return suffix if suffix in _PLAN_NAMES else ""


def _build_context(
    payload: Mapping[str, Any],
    *,
    filename: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise CodexExportError("凭证记录不是 JSON 对象")

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    now_epoch = int(current.timestamp())
    exported_at = current.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    maps = _candidate_maps(payload)

    access_token = _from_maps(maps, "access_token", "accessToken")
    if not access_token:
        raise CodexExportError("记录不包含完整 access_token（可能只是 OAuth 回执）")
    access_claims = _jwt_payload(access_token)
    access_auth = _auth_claims(access_claims)
    access_profile = _profile_claims(access_claims)

    id_token = _from_maps(maps, "id_token", "idToken")
    id_claims = _jwt_payload(id_token)
    id_auth = _auth_claims(id_claims)
    id_profile = _profile_claims(id_claims)

    account_id = _from_maps(
        maps,
        "account_id",
        "chatgpt_account_id",
        "accountId",
        "chatgptAccountId",
    ) or _first(
        access_auth.get("chatgpt_account_id"),
        id_auth.get("chatgpt_account_id"),
        access_claims.get("account_id"),
        id_claims.get("account_id"),
    )
    if not account_id:
        raise CodexExportError("记录缺少 account_id，无法生成目标格式")

    user_id = _from_maps(maps, "chatgpt_user_id", "user_id", "userId") or _first(
        access_auth.get("chatgpt_user_id"),
        access_auth.get("user_id"),
        id_auth.get("chatgpt_user_id"),
        id_auth.get("user_id"),
        access_claims.get("user_id"),
        id_claims.get("user_id"),
    )
    email = _from_maps(maps, "email") or _first(
        access_profile.get("email"),
        id_profile.get("email"),
        access_claims.get("email"),
        id_claims.get("email"),
    )
    plan_type = _from_maps(maps, "plan_type", "chatgpt_plan_type", "planType") or _first(
        access_auth.get("chatgpt_plan_type"),
        id_auth.get("chatgpt_plan_type"),
        _plan_from_filename(filename),
    )
    refresh_token = _from_maps(maps, "refresh_token", "refreshToken")
    # A session JWE is distinct from an OAuth refresh token.  Never infer one
    # from the other; only an explicit session_token is exported to CPA.
    session_token = _from_maps(maps, "session_token", "sessionToken")
    auth_provider = _from_maps(maps, "auth_provider", "authProvider")
    display_name = _from_maps(maps, "name", "display_name", "displayName") or _first(email, account_id)

    expiry_text, expiry_epoch = _normalize_expiry(
        # Match the reference converter: a valid access-JWT exp is the
        # authoritative expiry, with the locally stored timestamp as fallback.
        access_claims.get("exp"),
        _from_maps(maps, "expired", "expires_at", "expiresAt", "expiry"),
    )
    access_expiry_epoch = _epoch(access_claims.get("exp"))

    synthetic = False
    if not id_token:
        id_token = _synthetic_id_token(
            account_id=account_id,
            user_id=user_id,
            plan_type=plan_type,
            email=email,
            expires_epoch=access_expiry_epoch,
            now_epoch=now_epoch,
        )
        synthetic = True

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session_token": session_token,
        "id_token": id_token,
        "id_token_synthetic": synthetic,
        "account_id": account_id,
        "user_id": user_id,
        "email": email,
        "plan_type": plan_type,
        "auth_provider": auth_provider,
        "display_name": display_name,
        "expires_at": expiry_text,
        "expiry_epoch": expiry_epoch,
        "access_expiry_epoch": access_expiry_epoch,
        "exported_at": exported_at,
        "now_epoch": now_epoch,
    }


def build_cpa_document(
    payload: Mapping[str, Any],
    *,
    filename: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Convert one complete local credential to the reference CPA shape."""
    ctx = _build_context(payload, filename=filename, now=now)
    document: dict[str, Any] = {
        "type": "codex",
        "account_id": ctx["account_id"],
        "chatgpt_account_id": ctx["account_id"],
        "email": ctx["email"],
        "name": ctx["display_name"],
        "plan_type": ctx["plan_type"],
        "chatgpt_plan_type": ctx["plan_type"],
        "id_token": ctx["id_token"],
        "access_token": ctx["access_token"],
        "refresh_token": ctx["refresh_token"],
        "last_refresh": ctx["exported_at"],
        "expired": ctx["expires_at"],
    }
    if ctx["id_token_synthetic"]:
        # CPA uses this as a boolean marker; the synthetic JWT itself remains
        # in id_token and must not be duplicated into a misleading field.
        document["id_token_synthetic"] = True
    if ctx["session_token"]:
        document["session_token"] = ctx["session_token"]
    return validate_cpa_document(document)


def build_sub2api_account(
    payload: Mapping[str, Any],
    *,
    filename: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Convert one complete local credential to a Sub2API OAuth account."""
    ctx = _build_context(payload, filename=filename, now=now)
    has_refresh = bool(ctx["refresh_token"])
    account: dict[str, Any] = {
        "name": ctx["display_name"],
        "platform": "openai",
        "type": "oauth",
        "concurrency": 10,
        "priority": 1,
        "credentials": {
            "access_token": ctx["access_token"],
            "chatgpt_account_id": ctx["account_id"],
            "chatgpt_user_id": ctx["user_id"],
            "email": ctx["email"],
            "plan_type": ctx["plan_type"],
        },
        "extra": {
            "email": ctx["email"],
            "email_key": _email_key(ctx["email"]),
            "name": ctx["display_name"],
            "auth_provider": ctx["auth_provider"],
            "source": "chatgpt_web_session",
            "last_refresh": ctx["exported_at"],
        },
    }
    if not has_refresh:
        if ctx["access_expiry_epoch"]:
            account["expires_at"] = ctx["access_expiry_epoch"]
            account["auto_pause_on_expired"] = True
        if ctx["expires_at"]:
            account["credentials"]["expires_at"] = ctx["expires_at"]
            if ctx["expiry_epoch"]:
                account["credentials"]["expires_in"] = max(0, ctx["expiry_epoch"] - ctx["now_epoch"])
    # Match the reference converter's stripUnavailable behavior: omit empty
    # optional fields rather than serializing misleading empty metadata.
    account["credentials"] = {
        key: value
        for key, value in account["credentials"].items()
        if value not in (None, "")
    }
    account["extra"] = {
        key: value
        for key, value in account["extra"].items()
        if value not in (None, "")
    }
    return validate_sub2api_account(account)


def build_sub2api_document(
    records: list[tuple[str, Mapping[str, Any]]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a Sub2API package from ``(filename, payload)`` records."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    exported_at = current.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    document = {
        "exported_at": exported_at,
        "proxies": [],
        "accounts": [
            build_sub2api_account(payload, filename=filename, now=current)
            for filename, payload in records
        ],
    }
    return validate_sub2api_document(document)


def build_local_export(
    target: str,
    records: list[tuple[str, Mapping[str, Any]]],
    *,
    now: datetime | None = None,
) -> list[tuple[str, dict[str, Any]] | dict[str, Any]] | dict[str, Any]:
    """Convert records for a requested local export target.

    The explicit return form keeps conversion pure; the WebUI decides whether
    to return one JSON file or a ZIP/manifest for multiple records.
    """
    normalized = _text(target).lower()
    if normalized == "cpa":
        return [
            (filename, build_cpa_document(payload, filename=filename, now=now))
            for filename, payload in records
        ]
    if normalized in {"sub2", "sub2api", "sub2-api"}:
        return build_sub2api_document(records, now=now)
    raise CodexExportError(f"不支持的导出目标: {target}")


__all__ = [
    "CodexExportError",
    "validate_cpa_document",
    "validate_sub2api_account",
    "validate_sub2api_document",
    "build_cpa_document",
    "build_sub2api_account",
    "build_sub2api_document",
    "build_local_export",
]
