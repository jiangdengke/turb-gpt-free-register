"""Read-only proxy reachability and exit-geo diagnostics."""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse

from curl_cffi.requests import Session

from config import ACCEPT_LANGUAGE, IMPERSONATE, USER_AGENT
from config.proxy import normalize_proxy_url
from core.session import BrowserSession

logger = logging.getLogger(__name__)

DEFAULT_TARGET_URL = "https://chatgpt.com/"
_GEO_ENDPOINTS = (
    "https://ipinfo.io/json",
    "https://ipapi.co/json/",
    "https://ipwho.is/",
)


def _valid_http_url(value: str, *, field_name: str) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} 必须是完整的 HTTP/HTTPS 地址")
    if len(text) > 2048:
        raise ValueError(f"{field_name} 过长")
    return text


def _mask_proxy(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "直连"
    try:
        parsed = urlparse(text if "://" in text else f"//{text}")
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        host = parsed.hostname or "?"
        port = f":{parsed.port}" if parsed.port else ""
        auth = "***@" if parsed.username or parsed.password else ""
        return f"{scheme}{auth}{host}{port}"
    except Exception:
        return "***"


def _safe_error(exc: BaseException, proxy_url: str = "") -> str:
    text = str(exc or "").strip() or type(exc).__name__
    if proxy_url:
        text = text.replace(proxy_url, "<proxy>")
        text = text.replace(proxy_url.replace("http://", ""), "<proxy>")
    return text[:500]


def _json_payload(response) -> dict:
    try:
        data = response.json()
    except Exception:
        try:
            data = json.loads(response.text or "")
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


def _geo_from_response(response) -> dict:
    return BrowserSession._normalize_geo_response(_json_payload(response))


def _geo_from_target(response, target_url: str) -> dict:
    host = (urlparse(target_url).hostname or "").lower()
    if host in {"ipinfo.io", "ipapi.co", "ipwho.is"}:
        return _geo_from_response(response)
    return {}


def test_proxy(
    proxy: str = "",
    target_url: str = DEFAULT_TARGET_URL,
    *,
    timeout: float = 15.0,
) -> dict:
    """Test one proxy without touching registration or account APIs.

    Any HTTP response proves that the proxy transport reached the target. The
    target status is returned separately so a target-side 403 still reports a
    usable proxy. Exit geo is queried through the same session and proxy.
    """
    target = _valid_http_url(target_url or DEFAULT_TARGET_URL, field_name="测试地址")
    raw_proxy = str(proxy or "").strip()
    normalized_proxy = normalize_proxy_url(raw_proxy) if raw_proxy else ""
    if raw_proxy and "://" not in normalized_proxy and "://" not in raw_proxy:
        raise ValueError("代理地址格式无效")
    try:
        timeout_seconds = max(3.0, min(30.0, float(timeout or 15.0)))
    except (TypeError, ValueError) as exc:
        raise ValueError("超时时间无效") from exc

    client = Session(impersonate=IMPERSONATE)
    if normalized_proxy:
        client.proxies = {"http": normalized_proxy, "https": normalized_proxy}
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
    started = time.perf_counter()
    try:
        response = client.get(
            target,
            headers=headers,
            allow_redirects=True,
            timeout=timeout_seconds,
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        geo = _geo_from_target(response, target)
        geo_error = ""
        if not geo.get("ip"):
            geo_error = "出口 GeoIP 未返回有效信息"
            for endpoint in _GEO_ENDPOINTS:
                try:
                    geo_response = client.get(
                        endpoint,
                        headers={
                            "User-Agent": USER_AGENT,
                            "Accept": "application/json",
                        },
                        allow_redirects=True,
                        timeout=timeout_seconds,
                    )
                    if 200 <= int(geo_response.status_code) < 300:
                        candidate = _geo_from_response(geo_response)
                        if candidate.get("ip") or candidate.get("country"):
                            geo = candidate
                            geo_error = ""
                            break
                except Exception as exc:
                    geo_error = _safe_error(exc, normalized_proxy)

        status_code = int(response.status_code)
        result = {
            "ok": True,
            "status": "reachable",
            "message": "代理可达" if normalized_proxy else "直连可达",
            "proxy": _mask_proxy(normalized_proxy),
            "target_url": target,
            "http_status": status_code,
            "target_ok": 200 <= status_code < 400,
            "latency_ms": latency_ms,
            "exit_ip": geo.get("ip") or "",
            "country": geo.get("country") or "",
            "region": geo.get("region") or "",
            "city": geo.get("city") or "",
            "timezone": geo.get("timezone") or "",
            "org": geo.get("org") or "",
        }
        if geo_error:
            result["geo_error"] = geo_error[:300]
        return result
    except Exception as exc:
        return {
            "ok": False,
            "status": "unreachable",
            "message": "代理不可达" if normalized_proxy else "直连失败",
            "proxy": _mask_proxy(normalized_proxy),
            "target_url": target,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error": _safe_error(exc, normalized_proxy),
        }
    finally:
        try:
            client.close()
        except Exception:
            logger.debug("关闭代理测试会话失败", exc_info=True)
