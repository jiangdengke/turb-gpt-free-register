# -*- coding: utf-8 -*-
"""
通用 API 取码邮箱客户端。

邮箱池导入格式：
    email----code_url

注册时领取 email；取码时直接 GET code_url，并从响应中提取 6 位验证码。
响应可以是纯文本、HTML 或 JSON，只要其中包含 6 位验证码即可。
"""
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp

logger = logging.getLogger(__name__)

_CODE_REGEX = re.compile(r"\b(\d{6})\b")
_CONTEXT_WORDS = ("code", "verify", "verification", "验证码", "代码", "确认码", "認証", "コード")
_CONTEXT_CACHE: dict[str, "GenericApiEmailAccount"] = {}
_LAST_OTP_META: dict[str, dict] = {}
_MESSAGE_TIME_SKEW_SECONDS = 10.0
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_FILE = _PROJECT_ROOT / "用于注册的API邮箱.txt"


class GenericApiMailError(RuntimeError):
    """通用 API 取码邮箱错误。"""


@dataclass
class GenericApiEmailAccount:
    email: str
    code_url: str


def _flatten_json(obj) -> str:
    parts: list[str] = []
    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            parts.append(str(x))
    walk(obj)
    return "\n".join(parts)


def _visible_text(text: str) -> str:
    """Remove non-visible HTML so digits in styles and link URLs are not treated as OTPs."""
    text = re.sub(
        r"<(?:script|style|noscript)\b[^>]*>.*?</(?:script|style|noscript)>",
        " ",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _extract_code(text: str) -> str | None:
    """从纯文本/HTML/JSON 文本中提取 6 位 OTP。"""
    if not text:
        return None

    # 兼容 JSON：优先把所有 value 拉平再抽取。
    candidates_text = [text]
    try:
        parsed = json.loads(text)
        candidates_text.insert(0, _flatten_json(parsed))
    except Exception:
        pass

    for body in candidates_text:
        # 复用邮件 OTP 抽取逻辑。
        code = extract_otp({"text": body, "content": body, "subject": body[:200]})
        if code:
            return code

        # 只在可见文本中兜底。原始 HTML 的 style 色值、href 查询参数等
        # 也可能包含 6 位数字，直接扫源码会把这些无关数字误当成 OTP。
        visible = _visible_text(body)
        codes = _CODE_REGEX.findall(visible)
        if not codes:
            continue
        lower = visible.lower()
        for code in codes:
            idx = lower.find(code)
            window = lower[max(0, idx - 80): idx + 86]
            if any(w.lower() in window for w in _CONTEXT_WORDS):
                return code
        return codes[-1]
    return None


def _parse_message_timestamp(value) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _extract_message_code(message: dict) -> str | None:
    verify_code = message.get("verifyCode") or message.get("verify_code")
    if isinstance(verify_code, dict):
        for key in ("code", "value", "otp"):
            code = str(verify_code.get(key) or "").strip()
            if _CODE_REGEX.fullmatch(code):
                return code
    elif verify_code is not None:
        code = str(verify_code).strip()
        if _CODE_REGEX.fullmatch(code):
            return code

    searchable = {
        key: message.get(key)
        for key in ("subject", "text", "content", "html", "body")
        if message.get(key) is not None
    }
    return _extract_code(json.dumps(searchable, ensure_ascii=False))


def _extract_code_with_meta(text: str) -> tuple[str | None, float | None]:
    """Extract the newest message OTP and its server-provided timestamp."""
    try:
        payload = json.loads(text)
    except Exception:
        return _extract_code(text), None

    messages = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(messages, list):
        candidates: list[tuple[str, float | None, int]] = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            code = _extract_message_code(message)
            if not code:
                continue
            message_ts = _parse_message_timestamp(
                message.get("date")
                or message.get("receivedAt")
                or message.get("received_at")
                or message.get("createdAt")
                or message.get("created_at")
            )
            candidates.append((code, message_ts, index))
        if candidates:
            code, message_ts, _ = max(
                candidates,
                key=lambda item: (
                    item[1] is not None,
                    item[1] if item[1] is not None else -item[2],
                ),
            )
            return code, message_ts

    return _extract_code(text), None


def _structured_request_url(url: str) -> tuple[str, bool]:
    """Ask supported mailbox APIs for JSON while preserving generic URL behavior."""
    try:
        parsed = urlsplit(url)
    except Exception:
        return url, False
    if parsed.hostname != "api.xiaoheifk.cn" or parsed.path.rstrip("/") != "/api/mail-new":
        return url, False

    query = parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, "json" if key == "response_type" else value) for key, value in query]
    if not any(key == "response_type" for key, _ in query):
        query.append(("response_type", "json"))
    query = [(key, value) for key, value in query if key != "_ts"]
    query.append(("_ts", str(int(time.time() * 1000))))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)), True


def is_fresh_otp(email: str, code: str, after_ts: float | None) -> bool:
    """Return whether the last fetched code came from a message newer than this attempt."""
    if after_ts is None:
        return False
    meta = _LAST_OTP_META.get(str(email or "").strip().lower()) or {}
    message_ts = meta.get("message_ts")
    return (
        str(meta.get("code") or "") == str(code or "")
        and isinstance(message_ts, (int, float))
        and message_ts >= float(after_ts) - _MESSAGE_TIME_SKEW_SECONDS
    )


def pick_account() -> GenericApiEmailAccount:
    """领取一个可用通用 API 邮箱。"""
    from core.db import claim_next_generic_api_email, generic_api_email_pool_summary

    inserted, skipped = import_from_file()
    if inserted:
        logger.info(f"[GenericAPI] 已自动从 {_ACCOUNTS_FILE.name} 导入 {inserted} 个邮箱（跳过 {skipped} 个）")

    row = claim_next_generic_api_email()
    if row is None:
        summary = generic_api_email_pool_summary()
        raise GenericApiMailError(
            f"通用 API 邮箱池没有可用账号: {summary}. 请在 WebUI 邮箱池导入：邮箱----取码地址"
        )
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[account.email] = account
    logger.info(f"[GenericAPI] 选中邮箱: {account.email}（DB id={row.get('id')}）")
    return account


def import_from_file(path: str | Path | None = None) -> tuple[int, int]:
    """从文本文件导入通用 API 邮箱，每行：email----code_url 或 email====code_url。"""
    from core.db import import_generic_api_emails
    p = Path(path) if path else _ACCOUNTS_FILE
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    if not p.exists():
        return 0, 0
    records = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----") if "----" in line else line.split("====")
        parts = [x.strip() for x in parts]
        if len(parts) < 2:
            continue
        records.append({"email": parts[0], "code_url": parts[1]})
    return import_generic_api_emails(records)


def get_account_context(email: str) -> GenericApiEmailAccount | None:
    if email in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[email]
    from core.db import get_generic_api_email_by_email
    row = get_generic_api_email_by_email(email)
    if row is None:
        return None
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[email] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core.db import release_generic_api_email
    release_generic_api_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(email, None)
    _LAST_OTP_META.pop(str(email or "").strip().lower(), None)


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    轮询该邮箱配置的 code_url，直到提取到 6 位验证码或超时。

    settle 机制：首次拿到验证码后不立刻返回，而是继续等 OTP_SETTLE_SECONDS 秒。
    如果期间取码地址返回了不同验证码，则替换候选并重置 settle 倒计时；
    连续 settle 秒没有变化后才返回，避免取到接口缓存中的旧码。
    """
    account = get_account_context(email)
    if account is None:
        raise GenericApiMailError(f"通用 API 邮箱不存在或未导入: {email}")

    deadline = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
    interval = poll_interval or _email_cfg.OTP_POLL_INTERVAL
    settle = settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; gpt-register/1.0)",
    }
    last_error = ""
    best_otp: str | None = None
    best_seen_at: float = 0.0
    settle_until: float | None = None
    logger.info(
        f"[GenericAPI] 开始轮询取码地址: {email}，"
        f"最长 {max_wait or _email_cfg.OTP_MAX_WAIT}s, settle={settle}s"
    )

    while time.time() < deadline:
        try:
            request_url, requires_message_time = _structured_request_url(account.code_url)
            resp = requests.get(request_url, headers=headers, timeout=20, verify=False)
            text = resp.text or ""
            if resp.status_code == 200:
                code, message_ts = _extract_code_with_meta(text)
                if (
                    code
                    and requires_message_time
                    and after_ts is not None
                    and (
                        message_ts is None
                        or message_ts < float(after_ts) - _MESSAGE_TIME_SKEW_SECONDS
                    )
                ):
                    if message_ts is None:
                        last_error = "取码接口响应缺少邮件时间，暂不接受为本轮验证码"
                    else:
                        last_error = (
                            "最新验证码邮件早于本轮请求时间: "
                            f"message={datetime.fromtimestamp(message_ts, timezone.utc).isoformat()} "
                            f"after={datetime.fromtimestamp(float(after_ts), timezone.utc).isoformat()}"
                        )
                    code = None
                if code:
                    now_seen = time.time()
                    if not best_otp or message_ts != _LAST_OTP_META.get(email.lower(), {}).get("message_ts"):
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                        _LAST_OTP_META[email.lower()] = {
                            "code": code,
                            "message_ts": message_ts,
                            "after_ts": after_ts,
                        }
                        logger.info(
                            f"[GenericAPI] 锁定 OTP={code}"
                            f"{'（邮件时间已校验）' if message_ts is not None else ''}, "
                            f"等 {settle}s 看取码接口是否出现更新验证码..."
                        )
                    elif code != best_otp:
                        logger.info(
                            f"[GenericAPI] 发现更新 OTP={code}，"
                            f"替换之前的 {best_otp}, 重置 settle 计时"
                        )
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                        _LAST_OTP_META[email.lower()] = {
                            "code": code,
                            "message_ts": message_ts,
                            "after_ts": after_ts,
                        }
                    else:
                        logger.debug(f"[GenericAPI] 取码接口仍返回候选 OTP={best_otp}")
                else:
                    last_error = f"HTTP 200 但未提取到 6 位验证码，响应预览: {text[:160]}"
            else:
                last_error = f"HTTP {resp.status_code}: {text[:160]}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        now = time.time()
        if best_otp and settle_until is not None and now >= settle_until:
            logger.info(
                f"[GenericAPI] settle 完成，返回 OTP={best_otp}, "
                f"候选锁定时间={time.strftime('%H:%M:%S', time.localtime(best_seen_at))}"
            )
            return best_otp

        remaining = int(deadline - now)
        if best_otp and settle_until is not None:
            logger.info(
                f"[GenericAPI] 已锁定候选 OTP={best_otp}，等 settle 中"
                f"（剩余 settle ~{max(0, int(settle_until - now))}s, 总剩余 {remaining}s）..."
            )
        else:
            logger.info(
                f"[GenericAPI] 暂未从取码接口拿到验证码，"
                f"{interval}s 后重试（剩余 {remaining}s）..."
            )
        time.sleep(interval)

    if best_otp:
        logger.warning(f"[GenericAPI] 总超时但已有候选，返回 OTP={best_otp}")
        return best_otp

    raise GenericApiMailError(f"等待通用 API 验证码超时: {email}; {last_error}")
