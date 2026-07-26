# -*- coding: utf-8 -*-
"""扫码任务自动检测服务。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from config import proxy as proxy_cfg
from core import db, plan_check_service

logger = logging.getLogger(__name__)

_THREAD_LOCK = threading.Lock()
_STOP_EVENT = threading.Event()
_THREAD: threading.Thread | None = None


def _interval_seconds() -> float:
    try:
        value = float(getattr(proxy_cfg, "SCAN_AUTO_CHECK_INTERVAL", 15.0) or 15.0)
    except (TypeError, ValueError):
        value = 15.0
    return max(5.0, min(300.0, value))


def _timestamp(value) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        parsed = datetime.fromisoformat(normalized)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def run_once() -> dict:
    """为已领取的扫码任务发起套餐查询，并同步已完成任务。"""
    tasks = db.list_scan_tasks_admin(limit=5000)
    interval = _interval_seconds()
    now = time.time()
    active_ids: set[int] = set()
    stats = {"active": 0, "queued": 0, "busy": 0, "skipped": 0, "failed": 0}

    for task in tasks:
        if task.get("status") not in {"claimed", "scanned"}:
            continue
        account_id = int(task.get("account_id") or 0)
        if account_id <= 0 or account_id in active_ids:
            continue
        active_ids.add(account_id)
        stats["active"] += 1

        account = db.get_account(account_id)
        if not account:
            stats["skipped"] += 1
            continue
        if db._account_is_plus(account):
            # list_scan_tasks_admin() 已经同步了当前套餐；这里仅防止并发更新窗口。
            stats["skipped"] += 1
            continue

        access_token = str(account.get("access_token") or "").strip()
        if not access_token:
            logger.warning("[ScanAuto] 账号缺少 access_token，跳过任务 #%s", task.get("id"))
            stats["skipped"] += 1
            continue

        plan_status = str(account.get("plan_check_status") or "").strip().lower()
        if plan_status in {"queued", "running"}:
            stats["busy"] += 1
            continue
        last_checked = _timestamp(account.get("plan_checked_at"))
        if last_checked is not None and now - last_checked < interval:
            stats["skipped"] += 1
            continue

        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=account.get("email") or "",
            access_token=access_token,
            trigger="scan_auto",
            proxy=None,
            timezone_offset_min="-",
        )
        if queued.get("accepted"):
            stats["queued"] += 1
            logger.info(
                "[ScanAuto] 已为扫码任务 #%s 发起自动套餐检测: %s",
                task.get("id"),
                account.get("email") or account_id,
            )
        elif queued.get("busy"):
            stats["busy"] += 1
        else:
            stats["failed"] += 1
            logger.warning(
                "[ScanAuto] 任务 #%s 自动套餐检测入队失败: %s",
                task.get("id"),
                queued.get("error") or "未知错误",
            )
    return stats


def _worker() -> None:
    while not _STOP_EVENT.is_set():
        try:
            run_once()
        except Exception:
            logger.exception("[ScanAuto] 自动检测循环异常")
        _STOP_EVENT.wait(min(5.0, _interval_seconds()))


def start() -> None:
    """启动单例后台线程；适配 gunicorn 单 worker 和本地 WebUI。"""
    global _THREAD
    with _THREAD_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP_EVENT.clear()
        _THREAD = threading.Thread(
            target=_worker,
            name="scan-auto-check",
            daemon=True,
        )
        _THREAD.start()
        logger.info("[ScanAuto] 自动套餐检测已启动，轮询间隔=%ss", _interval_seconds())


def stop() -> None:
    global _THREAD
    with _THREAD_LOCK:
        _STOP_EVENT.set()
        thread = _THREAD
        _THREAD = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
