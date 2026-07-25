# -*- coding: utf-8 -*-
"""把已注册账号转换为 sub2api OAuth 账号导出格式。"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Iterable


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        segment = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _sources(account: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key in ("oauth_credentials", "credentials", "oauth", "tokens", "auth"):
        value = account.get(key)
        if isinstance(value, dict):
            out.append(value)
    out.append(account)
    return out


def _first(sources: Iterable[dict[str, Any]], *keys: str, default: Any = "") -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None and str(value).strip() != "":
                return value
    return default


def _unix_seconds(value: Any) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        timestamp = float(raw)
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000
        return max(0, int(timestamp))
    except (TypeError, ValueError):
        pass
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        return max(0, int(datetime.fromisoformat(normalized).timestamp()))
    except (TypeError, ValueError):
        return 0


def _claim_dict(claims: dict[str, Any], key: str) -> dict[str, Any]:
    value = claims.get(key)
    return value if isinstance(value, dict) else {}


def build_sub2_oauth_account(
    account: dict[str, Any],
    *,
    source_file: str = "",
) -> dict[str, Any]:
    """生成 sub2api 导出文件的 accounts[] OAuth 条目。"""
    sources = _sources(account)
    access_token = str(_first(sources, "access_token", "accessToken")).strip()
    if not access_token:
        raise ValueError("账号缺少 access_token")
    refresh_token = str(_first(sources, "refresh_token", "refreshToken")).strip()
    id_token = str(_first(sources, "id_token", "idToken")).strip()

    access_claims = _jwt_claims(access_token)
    id_claims = _jwt_claims(id_token)
    access_auth = _claim_dict(access_claims, "https://api.openai.com/auth")
    id_auth = _claim_dict(id_claims, "https://api.openai.com/auth")
    access_profile = _claim_dict(access_claims, "https://api.openai.com/profile")
    id_profile = _claim_dict(id_claims, "https://api.openai.com/profile")

    account_id = str(_first(
        sources,
        "chatgpt_account_id",
        "account_id",
        "accountId",
        default=access_auth.get("chatgpt_account_id") or id_auth.get("chatgpt_account_id") or "",
    )).strip()
    user_id = str(_first(
        sources,
        "chatgpt_user_id",
        "user_id",
        "userId",
        default=access_auth.get("chatgpt_user_id") or id_auth.get("chatgpt_user_id") or "",
    )).strip()
    auth_user_id = str(_first(
        sources,
        "chatgpt_auth_user_id",
        default=(
            id_claims.get("https://api.openai.com/auth.user_id")
            or access_claims.get("https://api.openai.com/auth.user_id")
            or id_auth.get("user_id")
            or access_auth.get("user_id")
            or user_id
        ),
    )).strip()
    account_user_id = str(_first(
        sources,
        "chatgpt_account_user_id",
        default=f"{user_id}__{account_id}" if user_id and account_id else "",
    )).strip()
    email = str(_first(
        sources,
        "email",
        default=(
            id_claims.get("email")
            or access_claims.get("email")
            or id_profile.get("email")
            or access_profile.get("email")
            or ""
        ),
    )).strip()
    plan_type = str(_first(
        sources,
        "chatgpt_plan_type",
        "current_plan_type",
        "plan_type",
        "planType",
        default=access_auth.get("chatgpt_plan_type") or id_auth.get("chatgpt_plan_type") or "free",
    )).strip() or "free"
    expires_at = _unix_seconds(_first(sources, "expires_at", "expired", "expires", default=0))
    if not expires_at:
        expires_at = _unix_seconds(access_claims.get("exp"))

    missing = [
        name
        for name, value in (("refresh_token", refresh_token), ("id_token", id_token))
        if not value
    ]
    ready = not missing
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    field_source = (
        "account_id/user_id/plan/email 优先读取保存凭据，其次读取 JWT claims；"
        "expires_at 优先读取保存值，其次读取 access_token.exp"
    )
    credentials = {
        "access_token": access_token,
        "account_id": account_id,
        "chatgpt_account_id": account_id,
        "chatgpt_account_user_id": account_user_id,
        "chatgpt_auth_user_id": auth_user_id,
        "chatgpt_plan_type": plan_type,
        "chatgpt_user_id": user_id,
        "email": email,
        "expires_at": expires_at,
        "id_token": id_token,
        "plan_type": plan_type,
        "refresh_token": refresh_token,
    }
    extra = {
        "account_id": account_id,
        "chatgpt_account_id": account_id,
        "chatgpt_account_user_id": account_user_id,
        "chatgpt_auth_user_id": auth_user_id,
        "chatgpt_field_checked_at": now,
        "chatgpt_field_source": field_source,
        "chatgpt_plan_type": plan_type,
        "chatgpt_user_id": user_id,
        "codex_5h_reset_after_seconds": 0,
        "codex_5h_reset_at": "",
        "codex_5h_used_percent": 0,
        "codex_5h_window_minutes": 0,
        "codex_7d_reset_after_seconds": 0,
        "codex_7d_reset_at": "",
        "codex_7d_used_percent": 0,
        "codex_7d_window_minutes": 0,
        "codex_primary_over_secondary_percent": 0,
        "codex_primary_reset_after_seconds": 0,
        "codex_primary_used_percent": 0,
        "codex_primary_window_minutes": 0,
        "codex_secondary_reset_after_seconds": 0,
        "codex_secondary_used_percent": 0,
        "codex_secondary_window_minutes": 0,
        "codex_usage_updated_at": "",
        "cpa_missing_reason": "" if ready else f"缺少 {', '.join(missing)}，账号不能自动续期",
        "cpa_ready": ready,
        "email": email,
        "openai_long_context_billing_enabled": bool(account.get("openai_long_context_billing_enabled", False)),
        "privacy_mode": str(account.get("privacy_mode") or ""),
        "source_file": str(source_file or ""),
    }
    return {
        "name": email or account_id or f"account-{account.get('id') or 'unknown'}",
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": extra,
        "concurrency": int(account.get("concurrency") or 10),
        "priority": int(account.get("priority") or 1),
        "rate_multiplier": float(account.get("rate_multiplier") or 1),
        "auto_pause_on_expired": bool(account.get("auto_pause_on_expired", True)),
    }


def build_sub2_export(accounts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """生成与 sub2api 导出页面一致的根对象。"""
    return {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": list(accounts),
    }
