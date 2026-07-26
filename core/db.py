# -*- coding: utf-8 -*-
"""
本地文件持久化层。

根目录文件分工：
    - 用于注册的邮箱.txt      仅保留可继续注册的邮箱素材
    - 注册成功的邮箱.txt      仅保存注册成功的邮箱素材，不追加 token
    - 注册成功的token.txt     每行只保存一个 access token
    - 用于注册的邮箱.json     Outlook 账号池完整状态
    - 注册成功的邮箱.json     注册成功账号完整状态
"""
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT
_LEGACY_DATA_DIR = _PROJECT_ROOT / "data"
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_PLAN_CHECK_STALE_SECONDS = 120
_PLAN_CHECK_QUEUE_STALE_SECONDS = 1800

_OUTLOOK_JSON = _PROJECT_ROOT / "用于注册的邮箱.json"
_OUTLOOK_TXT = _PROJECT_ROOT / "用于注册的邮箱.txt"
_GENERIC_API_EMAIL_JSON = _PROJECT_ROOT / "用于注册的API邮箱.json"
_GENERIC_API_EMAIL_TXT = _PROJECT_ROOT / "用于注册的API邮箱.txt"
_ACCOUNTS_JSON = _PROJECT_ROOT / "注册成功的邮箱.json"
_ACCOUNTS_TXT = _PROJECT_ROOT / "注册成功的邮箱.txt"
_TOKENS_TXT = _PROJECT_ROOT / "注册成功的token.txt"
_JOBS_JSON = _PROJECT_ROOT / "注册任务.json"
_VIEWER_HTML = _PROJECT_ROOT / "accounts_viewer.html"
_CODEX_DIR = _PROJECT_ROOT / "codex_accounts"
# 导出状态单独存：{ "codex-邮箱-plan.json": {"exported_at": "...", "exported_count": N} }
# 不污染 CPA 兼容的原文件
_CODEX_EXPORT_STATE = _PROJECT_ROOT / "codex_导出状态.json"
_SCANNERS_JSON = _PROJECT_ROOT / "扫码员.json"
_SCAN_TASKS_JSON = _PROJECT_ROOT / "扫码任务.json"
_SCAN_LEASE_SECONDS = 600
_SCAN_MAX_ACTIVE_TASKS = 3

_LEGACY_SQLITE = _LEGACY_DATA_DIR / "registrations.db"
_LEGACY_OUTLOOK_JSON = _LEGACY_DATA_DIR / "outlook_accounts.json"
_LEGACY_ACCOUNTS_JSON = _LEGACY_DATA_DIR / "registered_accounts.json"
_LEGACY_JOBS_JSON = _LEGACY_DATA_DIR / "registration_jobs.json"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_storage() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    _ensure_storage()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_storage()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _next_id(items: list[dict]) -> int:
    ids = [int(item.get("id") or 0) for item in items]
    return (max(ids) if ids else 0) + 1


def _outlook_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("password") or "",
        row.get("client_id") or "",
        row.get("refresh_token") or "",
    ])


def _generic_api_email_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("code_url") or "",
    ])


def _account_line(row: dict) -> str:
    base = row.get("original_email_line") or row.get("email") or ""
    token = row.get("access_token") or ""
    totp = row.get("totp_secret") or ""
    return f"{base}----{token}----{totp}" if totp else f"{base}----{token}"


def _registered_email_line(row: dict) -> str:
    """生成注册成功邮箱 TXT 的行内容；token 由注册成功的token.txt 单独保存。"""
    return row.get("original_email_line") or row.get("email") or ""


def _sync_outlook_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_outlook_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _OUTLOOK_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_generic_api_email_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_generic_api_email_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _GENERIC_API_EMAIL_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_accounts_txt(rows: list[dict]) -> None:
    lines = [_registered_email_line(r) for r in sorted(rows, key=lambda x: int(x.get("id") or 0))]
    _ACCOUNTS_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_tokens_txt(rows: list[dict]) -> None:
    tokens = [
        r.get("access_token") or ""
        for r in sorted(rows, key=lambda x: int(x.get("id") or 0))
        if r.get("access_token")
    ]
    _TOKENS_TXT.write_text(("\n".join(tokens) + ("\n" if tokens else "")), encoding="utf-8")


def _viewer_snapshot(outlook_rows: list[dict], account_rows: list[dict]) -> dict:
    account_by_email = {
        (a.get("email") or "").lower(): a
        for a in account_rows
    }
    return {
        "generated_at": _now(),
        "accounts": [
            _decorate_account(r)
            for r in sorted(account_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "outlook": [
            _decorate_outlook(r, account_by_email)
            for r in sorted(outlook_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "summary": {
            "accounts": len(account_rows),
            "outlook_total": len(outlook_rows),
            "outlook_available": sum(1 for r in outlook_rows if r.get("status") == "available"),
            "outlook_used": sum(1 for r in outlook_rows if r.get("status") == "used"),
            "outlook_failed": sum(1 for r in outlook_rows if r.get("status") == "failed"),
        },
    }


def _render_static_viewer(outlook_rows: list[dict] | None = None, account_rows: list[dict] | None = None) -> Path:
    """生成可直接双击打开的静态账号查看页。"""
    outlook_rows = _load_outlook() if outlook_rows is None else outlook_rows
    account_rows = _load_accounts() if account_rows is None else account_rows
    snapshot = _viewer_snapshot(outlook_rows, account_rows)
    data_json = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
    title = escape(f"账号查看器 - {snapshot['generated_at']}")
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    :root {{
      --bg: #eef3f8;
      --surface: #ffffff;
      --soft: #f7f9fc;
      --text: #172033;
      --muted: #667085;
      --line: #d9e2ec;
      --blue: #2563eb;
      --green: #16803c;
      --red: #c2413a;
      --amber: #b7791f;
    }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 22px 28px;
      background: #101827;
      color: #fff;
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: center;
      flex-wrap: wrap;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    .meta {{ margin-top: 6px; color: #b8c7d9; font-size: 13px; }}
    .stats {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .stat {{
      min-width: 116px;
      padding: 10px 12px;
      border: 1px solid rgba(255,255,255,.16);
      border-radius: 8px;
      background: rgba(255,255,255,.08);
    }}
    .stat span {{ display: block; color: #b8c7d9; font-size: 12px; }}
    .stat strong {{ display: block; margin-top: 4px; font-size: 18px; }}
    main {{ width: min(1500px, calc(100vw - 32px)); margin: 16px auto 30px; display: grid; gap: 16px; }}
    .toolbar, section {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 8px 22px rgba(15,23,42,.06);
    }}
    .toolbar {{ padding: 14px; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    .search {{ min-width: min(520px, 100%); flex: 1; }}
    input {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
    }}
    .buttons {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    button {{
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 0 12px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--soft); }}
    button.primary {{ border-color: var(--blue); background: var(--blue); color: #fff; }}
    button.good {{ border-color: #2f855a; background: #edf8f1; color: #166534; }}
    button:disabled {{ color: #98a2b3; cursor: not-allowed; background: #f2f4f7; }}
    .head {{ padding: 14px 16px; border-bottom: 1px solid var(--line); background: var(--soft); }}
    .head p {{ margin-top: 4px; color: var(--muted); font-size: 12px; }}
    .table-wrap {{ overflow: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf1f5; text-align: left; white-space: nowrap; vertical-align: middle; }}
    th {{ position: sticky; top: 0; background: #fbfcfe; color: #475467; z-index: 1; font-size: 12px; }}
    tr:hover td {{ background: #fbfdff; }}
    .main-cell {{ font-weight: 700; }}
    .sub-cell {{ margin-top: 3px; color: var(--muted); font-size: 12px; }}
    .mono {{ font-family: ui-monospace, "JetBrains Mono", Consolas, monospace; font-size: 12px; }}
    .muted {{ color: var(--muted); }}
    .pill {{ display: inline-flex; min-width: 48px; justify-content: center; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
    .status-available {{ color: var(--blue); background: #eef4ff; }}
    .status-used {{ color: #475467; background: #f2f4f7; }}
    .status-failed {{ color: var(--red); background: #fff0ef; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    #toast {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      padding: 10px 14px;
      border-radius: 8px;
      background: #101827;
      color: #fff;
      box-shadow: 0 14px 30px rgba(15,23,42,.24);
      opacity: 0;
      transform: translateY(8px);
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
    }}
    #toast.show {{ opacity: 1; transform: translateY(0); }}
    @media (max-width: 820px) {{
      header {{ align-items: flex-start; }}
      .stats {{ width: 100%; }}
      .stat {{ flex: 1; }}
    }}
  </style>
</head>
<body>
<header>
  <div>
    <h1>账号查看器</h1>
    <p class="meta">静态快照，无需启动 Web Server。生成时间：<span id="generated"></span></p>
  </div>
  <div class="stats">
    <div class="stat"><span>已完成</span><strong id="statAccounts">0</strong></div>
    <div class="stat"><span>邮箱总数</span><strong id="statOutlook">0</strong></div>
    <div class="stat"><span>可用邮箱</span><strong id="statAvailable">0</strong></div>
  </div>
</header>
<main>
  <div class="toolbar">
    <div class="search"><input id="q" placeholder="搜索邮箱、token、clientId、状态"></div>
    <div class="buttons">
      <button class="primary" id="copyAllTokens">复制全部 Token</button>
      <button class="good" id="copyAllLines">复制全部整行</button>
      <button id="copyAllEmails">复制全部邮箱素材</button>
    </div>
  </div>
  <section>
    <div class="head">
      <h2>已完成账号</h2>
      <p>整行格式：邮箱----密码----clientId----邮箱刷新令牌----accessToken----totpSecret（如有）</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>邮箱</th><th>来源</th><th>Token</th><th>备注</th><th>2FA</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody id="accountsBody"></tbody>
      </table>
    </div>
  </section>
  <section>
    <div class="head">
      <h2>邮箱素材库</h2>
      <p>原始格式：邮箱----密码----clientId----邮箱刷新令牌；注册完成后可直接复制对应 Token 或整行。</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>邮箱</th><th>状态</th><th>Token</th><th>导入时间</th><th>已用时间</th><th>操作</th></tr></thead>
        <tbody id="outlookBody"></tbody>
      </table>
    </div>
  </section>
</main>
<div id="toast"></div>
<script id="snapshot" type="application/json">{data_json}</script>
<script>
const SNAPSHOT = JSON.parse(document.getElementById('snapshot').textContent);
const $ = (s) => document.querySelector(s);
let copySeq = 0;
const copyStore = new Map();

function fmt(v) {{ return v == null || v === '' ? '-' : String(v); }}
function esc(v) {{
  return fmt(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}
function short(v, n = 34) {{
  const s = v || '';
  return s.length > n ? `${{s.slice(0, n)}}...` : s;
}}
function copyId(v) {{
  if (!v) return '';
  const id = `c${{++copySeq}}`;
  copyStore.set(id, v);
  return id;
}}
function btn(label, value, cls = '') {{
  const id = copyId(value);
  return `<button class="${{cls}}" data-copy-id="${{id}}" ${{id ? '' : 'disabled'}}>${{label}}</button>`;
}}
function pill(status) {{
  const map = {{ available: '可用', used: '已用', failed: '失败' }};
  const label = map[status] || status || '-';
  return `<span class="pill status-${{esc(status)}}">${{esc(label)}}</span>`;
}}
function showToast(text) {{
  const toast = $('#toast');
  toast.textContent = text;
  toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove('show'), 1400);
}}
async function copyText(text) {{
  if (!text) return;
  if (navigator.clipboard && window.isSecureContext) {{
    await navigator.clipboard.writeText(text);
  }} else {{
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }}
  showToast('已复制');
}}
function haystack(row) {{
  return Object.values(row).join('\\n').toLowerCase();
}}
function render() {{
  copyStore.clear();
  copySeq = 0;
  const q = $('#q').value.trim().toLowerCase();
  const accounts = SNAPSHOT.accounts.filter((r) => !q || haystack(r).includes(q));
  const outlook = SNAPSHOT.outlook.filter((r) => !q || haystack(r).includes(q));
  $('#generated').textContent = SNAPSHOT.generated_at;
  $('#statAccounts').textContent = SNAPSHOT.summary.accounts;
  $('#statOutlook').textContent = SNAPSHOT.summary.outlook_total;
  $('#statAvailable').textContent = SNAPSHOT.summary.outlook_available;
  $('#accountsBody').innerHTML = accounts.map((r) => `
    <tr>
      <td class="muted">#${{esc(r.id)}}</td>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell">${{esc(r.user_name || '-')}}</div></td>
      <td>${{esc(r.email_source || '-')}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 42))}}</span></td>
      <td title="${{esc(r.note || '')}}">${{r.note ? esc(short(r.note, 60)) : '<span class="muted">-</span>'}}</td>
      <td>${{r.totp_secret ? '已启用' : '<span class="muted">未启用</span>'}}</td>
      <td class="muted">${{esc(r.created_at || '-')}}</td>
      <td class="actions">${{btn('复制Token', r.access_token, 'primary')}} ${{btn('复制整行', r.copy_line, 'good')}}</td>
    </tr>`).join('');
  $('#outlookBody').innerHTML = outlook.map((r) => `
    <tr>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell mono">${{esc(short(r.copy_line, 76))}}</div></td>
      <td>${{pill(r.status)}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 36) || '未生成')}}</span></td>
      <td class="muted">${{esc(r.imported_at || r.created_at || '-')}}</td>
      <td class="muted">${{esc(r.used_at || '-')}}</td>
      <td class="actions">${{btn('复制邮箱', r.copy_line)}} ${{btn('复制Token', r.access_token, 'primary')}} ${{btn('复制整行', r.account_copy_line, 'good')}}</td>
    </tr>`).join('');
}}
document.addEventListener('click', (e) => {{
  const target = e.target.closest('[data-copy-id]');
  if (!target) return;
  copyText(copyStore.get(target.dataset.copyId));
}});
$('#q').addEventListener('input', render);
$('#copyAllTokens').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.access_token).filter(Boolean).join('\\n')));
$('#copyAllLines').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.copy_line).filter(Boolean).join('\\n')));
$('#copyAllEmails').addEventListener('click', () => copyText(SNAPSHOT.outlook.map((r) => r.copy_line).filter(Boolean).join('\\n')));
render();
</script>
</body>
</html>
"""
    tmp = _VIEWER_HTML.with_suffix(".html.tmp")
    tmp.write_text(html_text, encoding="utf-8")
    try:
        tmp.replace(_VIEWER_HTML)
        return _VIEWER_HTML
    except PermissionError:
        # Windows 下如果目标 HTML 正被浏览器或编辑器短暂占用，原子替换可能失败。
        # 先尝试直接覆盖；仍失败时写一个时间戳快照，避免注册流程被查看页刷新阻断。
        try:
            _VIEWER_HTML.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return _VIEWER_HTML
        except PermissionError:
            fallback = _DATA_DIR / f"accounts_viewer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            fallback.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return fallback


def _load_outlook() -> list[dict]:
    rows = _read_json(_OUTLOOK_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_OUTLOOK_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_outlook(rows: list[dict]) -> None:
    _write_json(_OUTLOOK_JSON, rows)
    _sync_outlook_txt(rows)
    _render_static_viewer(outlook_rows=rows)


def _load_generic_api_emails() -> list[dict]:
    rows = _read_json(_GENERIC_API_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_generic_api_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _generic_api_email_line(row)
    _write_json(_GENERIC_API_EMAIL_JSON, rows)
    _sync_generic_api_email_txt(rows)


def _load_accounts() -> list[dict]:
    rows = _read_json(_ACCOUNTS_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_ACCOUNTS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_accounts(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _account_line(row)
    _write_json(_ACCOUNTS_JSON, rows)
    _sync_accounts_txt(rows)
    _sync_tokens_txt(rows)
    _render_static_viewer(account_rows=rows)


def _load_jobs() -> list[dict]:
    rows = _read_json(_JOBS_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_JOBS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_jobs(rows: list[dict]) -> None:
    _write_json(_JOBS_JSON, rows)


def _load_scanners() -> list[dict]:
    rows = _read_json(_SCANNERS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_scanners(rows: list[dict]) -> None:
    _write_json(_SCANNERS_JSON, rows)


def _load_scan_tasks() -> list[dict]:
    rows = _read_json(_SCAN_TASKS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_scan_tasks(rows: list[dict]) -> None:
    _write_json(_SCAN_TASKS_JSON, rows)


def _find_by_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def _extract_link_expiry_timestamp(value) -> float | None:
    """把提链服务返回的秒/毫秒时间戳或 ISO 时间转换为 Unix 秒。"""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        timestamp = float(raw)
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000
        return timestamp if timestamp > 0 else None
    except (TypeError, ValueError):
        pass
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        return datetime.fromisoformat(normalized).timestamp()
    except (TypeError, ValueError):
        return None


def _scanner_key_hash(key: str) -> str:
    return hashlib.sha256(f"scanner-key:{key}".encode("utf-8")).hexdigest()


def _new_scanner_key() -> str:
    return f"scan_{secrets.token_urlsafe(24)}"


def _public_scanner(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "name": row.get("name") or "",
        "enabled": bool(row.get("enabled")),
        "key_version": int(row.get("key_version") or 1),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_login_at": row.get("last_login_at"),
    }


def _task_event(task: dict, action: str, *, scanner_id: int | None = None, detail: str = "") -> None:
    events = task.setdefault("events", [])
    events.append({
        "at": _now(),
        "action": str(action),
        "scanner_id": int(scanner_id) if scanner_id is not None else None,
        "detail": str(detail or ""),
    })
    if len(events) > 100:
        del events[:-100]


def _account_is_plus(row: dict) -> bool:
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    return "plus" in plan and "free" not in plan


def _account_has_scannable_link(row: dict) -> bool:
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    return (
        row.get("extract_link_status") == "success"
        and bool(row.get("extract_link_ok"))
        and plan == "free"
        and bool(row.get("plus_trial_eligible"))
        and bool(
            row.get("extract_link_image_url_png")
            or row.get("extract_link_image_url_svg")
            or row.get("extract_link_long_url")
            or row.get("extract_link_copy_paste")
        )
    )


def _masked_scan_email(value: str) -> str:
    email = str(value or "")
    local, sep, domain = email.partition("@")
    if sep and len(local) > 2:
        return f"{local[:2]}{'*' * min(6, max(2, len(local) - 2))}@{domain}"
    return email


def _scan_link_fingerprint(row: dict) -> str:
    raw = json.dumps({
        "job_id": row.get("extract_link_job_id"),
        "completed_at": row.get("extract_link_completed_at"),
        "long_url": row.get("extract_link_long_url"),
        "copy_paste": row.get("extract_link_copy_paste"),
        "image_url_png": row.get("extract_link_image_url_png"),
        "image_url_svg": row.get("extract_link_image_url_svg"),
        "expires_at": row.get("extract_link_expires_at"),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_scan_url(value, allowed_schemes: set[str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return raw if urlparse(raw).scheme.lower() in allowed_schemes else ""
    except (TypeError, ValueError):
        return ""


def _sync_scan_tasks_locked(accounts: list[dict], tasks: list[dict]) -> bool:
    """根据账号的最新提链/套餐状态更新扫码任务，调用方必须持有 _LOCK。"""
    changed = False
    now_ts = time.time()
    account_by_id = {int(row.get("id") or 0): row for row in accounts}

    for task in tasks:
        account = account_by_id.get(int(task.get("account_id") or 0))
        status = str(task.get("status") or "")
        if account is None and status in {"pending", "claimed", "scanned"}:
            task["status"] = "superseded"
            task["scanner_id"] = None
            task["lease_expires_at"] = None
            task["updated_at"] = _now()
            _task_event(task, "superseded", detail="账号已不存在")
            changed = True
            continue
        if account is None:
            continue
        if _account_is_plus(account) and status not in {"completed", "superseded"}:
            now = _now()
            if not task.get("scanned_at"):
                task["scanned_at"] = now
                _task_event(task, "auto_scanned", detail="套餐查询确认账号已升级为 Plus")
            task["status"] = "completed"
            task["completed_at"] = now
            task["lease_expires_at"] = None
            task["updated_at"] = now
            _task_event(task, "completed", detail="账号套餐已变更为 Plus")
            changed = True
            continue
        if status == "claimed":
            lease_ts = float(task.get("lease_expires_at") or 0)
            if lease_ts and lease_ts <= now_ts:
                scanner_id = task.get("scanner_id")
                task["status"] = "pending"
                task["scanner_id"] = None
                task["claimed_at"] = None
                task["lease_expires_at"] = None
                task["updated_at"] = _now()
                _task_event(task, "lease_expired", scanner_id=scanner_id)
                changed = True
                status = "pending"
        if status in {"pending", "claimed", "scanned"}:
            expires_ts = float(task.get("link_expires_ts") or 0)
            if expires_ts and expires_ts <= now_ts:
                scanner_id = task.get("scanner_id")
                task["status"] = "expired"
                task["scanner_id"] = None
                task["lease_expires_at"] = None
                task["updated_at"] = _now()
                _task_event(task, "expired", scanner_id=scanner_id)
                changed = True

    for account in accounts:
        if not _account_has_scannable_link(account):
            continue
        expires_ts = _extract_link_expiry_timestamp(account.get("extract_link_expires_at"))
        if expires_ts is not None and expires_ts <= now_ts:
            continue
        account_id = int(account.get("id") or 0)
        fingerprint = _scan_link_fingerprint(account)
        same_task = next((
            task for task in tasks
            if int(task.get("account_id") or 0) == account_id
            and task.get("link_fingerprint") == fingerprint
        ), None)
        if same_task is not None:
            continue
        for task in tasks:
            if (
                int(task.get("account_id") or 0) == account_id
                and task.get("status") in {"pending", "claimed", "scanned"}
            ):
                scanner_id = task.get("scanner_id")
                task["status"] = "superseded"
                task["scanner_id"] = None
                task["lease_expires_at"] = None
                task["updated_at"] = _now()
                _task_event(task, "superseded", scanner_id=scanner_id, detail="账号生成了新的支付链接")
                changed = True
        now = _now()
        scanner_id = int(account.get("extract_link_scanner_id") or 0) or None
        lease_expires_at = None
        if scanner_id is not None:
            lease_expires_at = now_ts + _SCAN_LEASE_SECONDS
            if expires_ts is not None:
                lease_expires_at = min(lease_expires_at, expires_ts)
        task = {
            "id": _next_id(tasks),
            "account_id": account_id,
            "link_fingerprint": fingerprint,
            "status": "claimed" if scanner_id is not None else "pending",
            "scanner_id": scanner_id,
            "claimed_at": now if scanner_id is not None else None,
            "lease_expires_at": lease_expires_at,
            "scanned_at": None,
            "completed_at": None,
            "link_expires_at": account.get("extract_link_expires_at"),
            "link_expires_ts": expires_ts,
            "created_at": now,
            "updated_at": now,
            "events": [],
        }
        _task_event(task, "created")
        if scanner_id is not None:
            _task_event(task, "claimed", scanner_id=scanner_id, detail="扫码员提链成功后自动领取")
        tasks.append(task)
        changed = True
    return changed


def sync_scan_tasks() -> bool:
    """用当前账号套餐状态同步扫码任务，供后台检测服务调用。"""
    with _LOCK:
        accounts = _load_accounts()
        tasks = _load_scan_tasks()
        changed = _sync_scan_tasks_locked(accounts, tasks)
        if changed:
            _save_scan_tasks(tasks)
        return changed


def _decorate_scan_task(
    task: dict,
    account: dict | None,
    scanner_by_id: dict[int, dict],
    *,
    reveal_link: bool = False,
) -> dict:
    email = str((account or {}).get("email") or "")
    masked_email = _masked_scan_email(email)
    scanner = scanner_by_id.get(int(task.get("scanner_id") or 0))
    out = {
        "id": int(task.get("id") or 0),
        "account_id": int(task.get("account_id") or 0),
        "email": email if reveal_link else masked_email,
        "status": task.get("status"),
        "scanner_id": task.get("scanner_id"),
        "scanner_name": scanner.get("name") if scanner else "",
        "claimed_at": task.get("claimed_at"),
        "lease_expires_at": task.get("lease_expires_at"),
        "scanned_at": task.get("scanned_at"),
        "completed_at": task.get("completed_at"),
        "link_expires_at": task.get("link_expires_at"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "link_type": (account or {}).get("extract_link_type"),
        "payment_method": (account or {}).get("extract_link_payment_method"),
    }
    if reveal_link:
        out["qr_url"] = _safe_scan_url((
            (account or {}).get("extract_link_image_url_png")
            or (account or {}).get("extract_link_image_url_svg")
        ), {"http", "https"})
        out["payment_url"] = _safe_scan_url((
            (account or {}).get("extract_link_long_url")
            or (account or {}).get("extract_link_copy_paste")
        ), {"http", "https", "upi"})
    return out


def _decorate_account(row: dict) -> dict:
    out = dict(row)
    out["note"] = out.get("note") or ""
    out["note_updated_at"] = out.get("note_updated_at") or ""
    extract_expiry = _extract_link_expiry_timestamp(out.get("extract_link_expires_at"))
    out["extract_link_expired"] = bool(extract_expiry is not None and time.time() >= extract_expiry)
    plan_status = out.get("plan_check_status")
    if plan_status in {"queued", "running"}:
        try:
            stamp_key = "plan_check_queued_at" if plan_status == "queued" else "plan_check_started_at"
            stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if plan_status == "queued" else _PLAN_CHECK_STALE_SECONDS
            started_at = datetime.fromisoformat(str(out.get(stamp_key) or ""))
            if (datetime.now() - started_at).total_seconds() >= stale_after:
                out["plan_check_status"] = "failed"
                out["plan_check_error"] = "上次套餐查询状态已超时，可重新查询"
                out["plan_check_stale"] = True
        except (TypeError, ValueError):
            out["plan_check_status"] = "failed"
            out["plan_check_error"] = "上次套餐查询状态异常，可重新查询"
            out["plan_check_stale"] = True
    out["copy_line"] = _account_line(out)
    return out


def _account_matches_plan_filter(row: dict, plan_filter: str | None = None) -> bool:
    """账号套餐过滤。plus_trial 表示 free 且具备 Plus 试用资格。"""
    f = str(plan_filter or "").strip().lower()
    if not f or f in {"all", "any"}:
        return True
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    if f == "plus":
        # “free(可Plus试用)”/plus_trial_eligible 只是可试用，不算已开通 Plus。
        # 只有套餐字段本身是 Plus/ChatGPT Plus/plus_* 且不含 free 时才命中。
        return "plus" in plan and "free" not in plan
    if f in {"plus_trial", "trial_eligible"}:
        return plan == "free" and bool(row.get("plus_trial_eligible"))
    if f == "free":
        return plan == "free"
    return plan == f


def _decorate_outlook(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _outlook_line(out)
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _decorate_generic_api_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _generic_api_email_line(out)
    out["password"] = out.get("password") or ""
    out["client_id"] = out.get("client_id") or ""
    out["refresh_token"] = out.get("refresh_token") or ""
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _get_conn() -> None:
    """兼容旧入口：初始化文件存储目录。"""
    _ensure_storage()
    return None


def _row_to_dict(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


# ============================================================
# registered_accounts
# ============================================================

def insert_account(
    *,
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    plan_type: str | None = None,
    expires_at: str | None = None,
    device_id: str | None = None,
    proxy_used: str | None = None,
    email_source: str | None = None,
    extra: dict | None = None,
    codex_status: str | None = None,   # success / failed / skipped / missing
    codex_error: str | None = None,    # 失败原因（仅 codex_status=failed 时有意义）
) -> int:
    """插入或更新注册成功账号，返回本地文件中的 id。"""
    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        existing = _find_by_email(accounts, email)
        outlook_row = _find_by_email(outlook_rows, email)
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None

        if existing is None:
            row_id = _next_id(accounts)
            row = {
                "id": row_id,
                "email": email,
                "created_at": _now(),
            }
            accounts.append(row)
        else:
            row = existing
            row_id = int(row["id"])

        row.update({
            "access_token": access_token,
            "totp_secret": totp_secret if totp_secret is not None else row.get("totp_secret"),
            "user_id": user_id if user_id is not None else row.get("user_id"),
            "user_name": user_name if user_name is not None else row.get("user_name"),
            "plan_type": plan_type if plan_type is not None else row.get("plan_type"),
            "expires_at": expires_at if expires_at is not None else row.get("expires_at"),
            "device_id": device_id if device_id is not None else row.get("device_id"),
            "proxy_used": proxy_used if proxy_used is not None else row.get("proxy_used"),
            "email_source": email_source if email_source is not None else row.get("email_source"),
            "extra_json": extra_json if extra_json is not None else row.get("extra_json"),
            "codex_status": codex_status if codex_status is not None else row.get("codex_status"),
            "codex_error": codex_error if codex_error is not None else row.get("codex_error"),
            "updated_at": _now(),
        })

        if outlook_row:
            row["password"] = outlook_row.get("password")
            row["client_id"] = outlook_row.get("client_id")
            row["refresh_token"] = outlook_row.get("refresh_token")
            row["original_email_line"] = _outlook_line(outlook_row)
            outlook_row["status"] = "used"
            outlook_row["used_at"] = outlook_row.get("used_at") or _now()
            outlook_row["registered_account_id"] = row_id
            outlook_row["access_token"] = access_token
            outlook_row["completed_at"] = _now()
            if totp_secret:
                outlook_row["totp_secret"] = totp_secret

        row["copy_line"] = _account_line(row)
        _save_accounts(accounts)
        _save_outlook(outlook_rows)
        return row_id


def update_account_codex_status(email: str, codex_status: str, codex_error: str | None = None) -> bool:
    """
    单独更新某账号的 codex_status / codex_error（手动补跑 Codex 时用）。
    返回是否找到该账号。
    """
    with _LOCK:
        accounts = _load_accounts()
        row = _find_by_email(accounts, email)
        if row is None:
            return False
        row["codex_status"] = codex_status
        row["codex_error"] = codex_error
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_codex_agent(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 Codex Agent Token 生成任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("codex_agent_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "codex_agent_queued_at" if current_status == "queued" else "codex_agent_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["codex_agent_status"] = "queued"
        row["codex_agent_ok"] = False
        row["codex_agent_trigger"] = str(trigger or "manual")
        row["codex_agent_queued_at"] = now
        row["codex_agent_started_at"] = None
        row["codex_agent_completed_at"] = None
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_codex_agent_running(acc_id: int) -> bool:
    """把 Codex Agent Token 生成任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("codex_agent_status") not in {"queued", "running"}:
            return False
        row["codex_agent_status"] = "running"
        row["codex_agent_started_at"] = _now()
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "正在生成 Codex Agent Token"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_codex_agent(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 Codex Agent Token 生成结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["codex_agent_status"] = status
        row["codex_agent_ok"] = ok
        row["codex_agent_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "unsupported", "stopped"}:
            row["codex_agent_completed_at"] = _now()
        row["codex_agent_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["codex_agent_message"] = result.get("message")
        if result.get("agent_runtime_id") is not None:
            row["codex_agent_runtime_id"] = result.get("agent_runtime_id")
        if result.get("auth_path") is not None:
            row["codex_agent_auth_path"] = result.get("auth_path")
        if isinstance(result.get("auth_json"), dict):
            row["codex_agent_token"] = json.dumps(result.get("auth_json"), ensure_ascii=False)
        for _k in (
            "codex_agent_network_route",
            "codex_agent_proxy_mode",
            "codex_agent_proxy_used",
            "codex_agent_proxy_fallback_reason",
            "codex_agent_device_id",
            "codex_agent_oai_session_id",
            "codex_agent_attempt_count",
            "codex_agent_max_attempts",
            "codex_agent_request_timeout",
            "codex_agent_sub2api_path",
            "codex_agent_sub2api_url",
            "codex_agent_sub2api_mode",
            "codex_agent_sub2api_total",
        ):
            src_key = _k.replace("codex_agent_", "", 1)
            if result.get(src_key) is not None:
                row[_k] = result.get(src_key)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_codex_agents() -> int:
    """服务启动时恢复上次进程中断的 Codex Agent 任务状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("codex_agent_status") not in {"queued", "running"}:
                continue
            row["codex_agent_status"] = "failed"
            row["codex_agent_ok"] = False
            row["codex_agent_error"] = "WebUI 重启导致 Codex Agent Token 任务中断，请重新生成"
            row["codex_agent_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def claim_account_plan_check(
    acc_id: int | None = None,
    email: str | None = None,
    trigger: str = "manual",
) -> bool:
    """原子占用账号的套餐查询；已有未超时查询时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        current_status = row.get("plan_check_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "plan_check_queued_at" if current_status == "queued" else "plan_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass

        now = _now()
        row["plan_check_status"] = "queued"
        row["plan_check_trigger"] = str(trigger or "manual")
        row["plan_check_queued_at"] = now
        row["plan_check_started_at"] = None
        row["plan_check_completed_at"] = None
        row["plan_check_error"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_plan_check_running(acc_id: int) -> bool:
    """把已排队的套餐查询标记为执行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("plan_check_status") not in {"queued", "running"}:
            return False
        row["plan_check_status"] = "running"
        row["plan_check_started_at"] = _now()
        row["plan_check_error"] = None
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_plan_checks() -> int:
    """服务启动时把上次进程遗留的内存队列状态恢复为可重试失败。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("plan_check_status") not in {"queued", "running"}:
                continue
            row["plan_check_status"] = "failed"
            row["plan_check_ok"] = False
            row["plan_check_error"] = "WebUI 重启导致套餐查询中断，请重新查询"
            row["plan_check_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def update_account_plan_check(acc_id: int | None = None, email: str | None = None, result: dict | None = None) -> bool:
    """更新账号套餐/Plus 试用资格查询结果。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        ok = bool(result.get("ok"))
        row["plan_check_status"] = "success" if ok else "failed"
        row["plan_check_ok"] = ok
        row["plan_checked_at"] = result.get("checked_at") or _now()
        row["plan_check_completed_at"] = _now()
        row["plan_check_http_status"] = result.get("http_status")
        row["plan_check_error"] = None if ok else result.get("error")

        if result.get("account_id"):
            row["account_id"] = result.get("account_id")
        # 查询失败只更新本次错误和网络信息，不覆盖上一次成功拿到的套餐、
        # 试用资格、优惠及有效期，避免临时网络故障把真实权益清空。
        if ok:
            if result.get("current_plan_type"):
                row["current_plan_type"] = result.get("current_plan_type")
                row["plan_type"] = result.get("current_plan_type")
            if result.get("subscription_plan") is not None:
                row["subscription_plan"] = result.get("subscription_plan")
            if result.get("has_active_subscription") is not None:
                row["has_active_subscription"] = bool(result.get("has_active_subscription"))
            if result.get("expires_at") is not None:
                row["plan_expires_at"] = result.get("expires_at")
            if result.get("renews_at") is not None:
                row["plan_renews_at"] = result.get("renews_at")
            if result.get("cancels_at") is not None:
                row["plan_cancels_at"] = result.get("cancels_at")
            if result.get("billing_period") is not None:
                row["billing_period"] = result.get("billing_period")
            if result.get("billing_currency") is not None:
                row["billing_currency"] = result.get("billing_currency")
            if result.get("is_delinquent") is not None:
                row["is_delinquent"] = bool(result.get("is_delinquent"))
            for _k in (
                "discount_type",
                "discount_amount",
                "discount_duration_num_periods",
                "discount_expires_at",
                "discount_cancellation_policy",
                "discount_promo_campaign_id",
                "last_purchase_origin_platform",
                "last_will_renew",
            ):
                if result.get(_k) is not None:
                    row[_k] = result.get(_k)

            row["plus_trial_eligible"] = bool(result.get("plus_trial_eligible"))
            row["plus_trial_campaign_id"] = result.get("plus_trial_campaign_id")
            row["plus_trial_title"] = result.get("plus_trial_title")
            row["plus_trial_discount_percentage"] = result.get("plus_trial_discount_percentage")
            row["plus_trial_duration_num_periods"] = result.get("plus_trial_duration_num_periods")
            row["plus_trial_duration_period"] = result.get("plus_trial_duration_period")
            row["eligible_offer_ids"] = result.get("eligible_offer_ids") or []
            row["plan_last_success_at"] = result.get("checked_at") or _now()
            row["plan_last_success_result_json"] = json.dumps(result, ensure_ascii=False)
        row["plan_check_proxy_mode"] = result.get("proxy_mode")
        row["plan_check_network_route"] = result.get("network_route")
        row["plan_check_proxy_used"] = result.get("proxy_used")
        row["plan_check_proxy_fallback_reason"] = result.get("proxy_fallback_reason")
        row["token_expired"] = result.get("token_expired")
        row["token_expires_at"] = result.get("token_expires_at")
        row["plan_check_result_json"] = json.dumps(result, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_extract(
    acc_id: int,
    trigger: str = "manual",
    link_type: str = "pix",
    scanner_id: int | None = None,
) -> bool:
    """原子占用账号提链任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        if scanner_id is not None:
            scanners = _load_scanners()
            scanner = next((
                item for item in scanners
                if int(item.get("id") or 0) == int(scanner_id) and bool(item.get("enabled"))
            ), None)
            if scanner is None:
                return False
            tasks = _load_scan_tasks()
            if _sync_scan_tasks_locked(accounts, tasks):
                _save_scan_tasks(tasks)
            if _scanner_active_claim_count_locked(int(scanner_id), accounts, tasks) >= _SCAN_MAX_ACTIVE_TASKS:
                return False
            expires_ts = _extract_link_expiry_timestamp(row.get("extract_link_expires_at"))
            if _account_has_scannable_link(row) and (expires_ts is None or expires_ts > time.time()):
                return False
        current_status = row.get("extract_link_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "extract_link_queued_at" if current_status == "queued" else "extract_link_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["extract_link_status"] = "queued"
        row["extract_link_ok"] = False
        row["extract_link_trigger"] = str(trigger or "manual")
        row["extract_link_type"] = str(link_type or "pix").lower()
        row["extract_link_scanner_id"] = int(scanner_id) if scanner_id is not None else None
        row["extract_link_queued_at"] = now
        row["extract_link_started_at"] = None
        row["extract_link_completed_at"] = None
        row["extract_link_error"] = None
        row["extract_link_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_extract_running(acc_id: int) -> bool:
    """把提链任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("extract_link_status") not in {"queued", "running"}:
            return False
        row["extract_link_status"] = "running"
        row["extract_link_started_at"] = _now()
        row["extract_link_error"] = None
        row["extract_link_message"] = "任务运行中"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_extract(acc_id: int, result: dict | None = None) -> bool:
    """更新账号提链任务结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["extract_link_status"] = status
        row["extract_link_ok"] = ok
        row["extract_link_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["extract_link_completed_at"] = _now()
        row["extract_link_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["extract_link_message"] = result.get("message")
        if result.get("job_id") is not None:
            row["extract_link_job_id"] = result.get("job_id")
        if result.get("link_type") is not None:
            row["extract_link_type"] = result.get("link_type")
        if result.get("cdk_remaining") is not None:
            row["extract_link_cdk_remaining"] = result.get("cdk_remaining")
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        if payload:
            row["extract_link_long_url"] = payload.get("long_url")
            row["extract_link_copy_paste"] = payload.get("copy_paste")
            row["extract_link_image_url_png"] = payload.get("image_url_png")
            row["extract_link_image_url_svg"] = payload.get("image_url_svg")
            row["extract_link_payment_method"] = payload.get("payment_method")
            row["extract_link_payment_link_type"] = payload.get("payment_link_type")
            row["extract_link_expires_at"] = payload.get("expires_at")
            if payload.get("cdk_remaining") is not None:
                row["extract_link_cdk_remaining"] = payload.get("cdk_remaining")
            row["extract_link_result_json"] = json.dumps(payload, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        tasks = _load_scan_tasks()
        if _sync_scan_tasks_locked(accounts, tasks):
            _save_scan_tasks(tasks)
        return True


def recover_interrupted_extract_links() -> int:
    """服务启动时恢复上次进程中断的提链状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("extract_link_status") not in {"queued", "running"}:
                continue
            row["extract_link_status"] = "failed"
            row["extract_link_ok"] = False
            row["extract_link_error"] = "WebUI 重启导致提链任务中断，请重新提链"
            row["extract_link_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def list_account_plan_check_statuses(limit: int = 5000, archived: str | bool | None = False, plan_filter: str | None = None) -> dict:
    """返回不含 Token/邮箱密码的套餐查询轻量状态快照。"""
    fields = (
        "id", "email", "updated_at", "archived", "archived_at", "plan_type", "current_plan_type",
        "plan_check_status", "plan_check_trigger", "plan_check_queued_at",
        "plan_check_started_at", "plan_check_completed_at", "plan_check_ok",
        "plan_check_error", "plan_checked_at", "plan_last_success_at",
        "plus_trial_eligible", "plan_check_network_route",
        "extract_link_status", "extract_link_ok", "extract_link_type",
        "extract_link_job_id", "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste",
        "extract_link_image_url_png", "extract_link_image_url_svg",
        "extract_link_expires_at", "extract_link_expired", "extract_link_payment_method",
        "extract_link_payment_link_type",
        "extract_link_checked_at", "extract_link_completed_at",
        "codex_agent_status", "codex_agent_ok", "codex_agent_message",
        "codex_agent_error", "codex_agent_runtime_id", "codex_agent_token",
        "codex_agent_auth_path", "codex_agent_checked_at", "codex_agent_completed_at",
        "codex_agent_network_route", "codex_agent_proxy_mode", "codex_agent_proxy_used",
        "codex_agent_proxy_fallback_reason", "codex_agent_device_id", "codex_agent_oai_session_id",
        "codex_agent_attempt_count", "codex_agent_max_attempts", "codex_agent_request_timeout",
        "codex_agent_sub2api_path", "codex_agent_sub2api_url", "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
    )
    with _LOCK:
        all_rows = _load_accounts()
        if archived in (True, "1", "true", "yes", "only"):
            all_rows = [r for r in all_rows if bool(r.get("archived"))]
        elif archived in ("all", "include"):
            pass
        else:
            all_rows = [r for r in all_rows if not bool(r.get("archived"))]
        decorated_rows = [_decorate_account(r) for r in all_rows]
        decorated_rows = [r for r in decorated_rows if _account_matches_plan_filter(r, plan_filter)]
        rows = sorted(decorated_rows, key=lambda x: int(x.get("id") or 0), reverse=True)[:max(1, int(limit))]
        items = []
        for row in rows:
            items.append({key: row.get(key) for key in fields})
        latest = max((str(row.get("updated_at") or "") for row in rows), default="")
        expired_count = sum(bool(row.get("extract_link_expired")) for row in rows)
        return {"items": items, "revision": f"{len(rows)}:{latest}:expired={expired_count}"}


def list_accounts(limit: int = 500, offset: int = 0, archived: str | bool | None = False, plan_filter: str | None = None) -> list[dict]:
    with _LOCK:
        rows = _load_accounts()
        if archived in (True, "1", "true", "yes", "only"):
            rows = [r for r in rows if bool(r.get("archived"))]
        elif archived in ("all", "include"):
            pass
        else:
            rows = [r for r in rows if not bool(r.get("archived"))]
        decorated = [_decorate_account(r) for r in rows]
        decorated = [r for r in decorated if _account_matches_plan_filter(r, plan_filter)]
        rows = sorted(decorated, key=lambda x: int(x.get("id") or 0), reverse=True)
        return rows[offset: offset + limit]


def get_account(acc_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_accounts() if int(r.get("id") or 0) == int(acc_id)), None)
        return _decorate_account(row) if row else None


def get_account_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_accounts(), email)
        return _decorate_account(row) if row else None


def create_scanner(name: str) -> tuple[dict, str]:
    """创建扫码员并返回一次性明文密钥。持久化文件中只保存摘要。"""
    normalized = " ".join(str(name or "").strip().split())
    if not normalized:
        raise ValueError("扫码员名称不能为空")
    if len(normalized) > 64:
        raise ValueError("扫码员名称最多 64 个字符")
    with _LOCK:
        scanners = _load_scanners()
        if any(str(row.get("name") or "").casefold() == normalized.casefold() for row in scanners):
            raise ValueError("扫码员名称已存在")
        key = _new_scanner_key()
        now = _now()
        row = {
            "id": _next_id(scanners),
            "name": normalized,
            "key_hash": _scanner_key_hash(key),
            "enabled": True,
            "key_version": 1,
            "created_at": now,
            "updated_at": now,
            "last_login_at": None,
        }
        scanners.append(row)
        _save_scanners(scanners)
        return _public_scanner(row), key


def get_scanner(scanner_id: int) -> dict | None:
    with _LOCK:
        row = next((
            row for row in _load_scanners()
            if int(row.get("id") or 0) == int(scanner_id)
        ), None)
        return _public_scanner(row) if row else None


def authenticate_scanner_key(key: str) -> dict | None:
    candidate = _scanner_key_hash(str(key or ""))
    with _LOCK:
        scanners = _load_scanners()
        matched = next((
            row for row in scanners
            if row.get("key_hash")
            and hmac.compare_digest(str(row.get("key_hash")), candidate)
        ), None)
        if matched is None or not bool(matched.get("enabled")):
            return None
        matched["last_login_at"] = _now()
        matched["updated_at"] = _now()
        _save_scanners(scanners)
        return _public_scanner(matched)


def reset_scanner_key(scanner_id: int) -> tuple[dict, str]:
    with _LOCK:
        scanners = _load_scanners()
        row = next((
            row for row in scanners
            if int(row.get("id") or 0) == int(scanner_id)
        ), None)
        if row is None:
            raise LookupError("扫码员不存在")
        key = _new_scanner_key()
        row["key_hash"] = _scanner_key_hash(key)
        row["key_version"] = int(row.get("key_version") or 1) + 1
        row["updated_at"] = _now()
        _save_scanners(scanners)
        return _public_scanner(row), key


def set_scanner_enabled(scanner_id: int, enabled: bool) -> dict:
    with _LOCK:
        scanners = _load_scanners()
        row = next((
            row for row in scanners
            if int(row.get("id") or 0) == int(scanner_id)
        ), None)
        if row is None:
            raise LookupError("扫码员不存在")
        row["enabled"] = bool(enabled)
        row["updated_at"] = _now()
        _save_scanners(scanners)
        if not enabled:
            accounts = _load_accounts()
            tasks = _load_scan_tasks()
            changed = _sync_scan_tasks_locked(accounts, tasks)
            for task in tasks:
                if (
                    task.get("status") == "claimed"
                    and int(task.get("scanner_id") or 0) == int(scanner_id)
                ):
                    task["status"] = "pending"
                    task["scanner_id"] = None
                    task["claimed_at"] = None
                    task["lease_expires_at"] = None
                    task["updated_at"] = _now()
                    _task_event(task, "released", scanner_id=scanner_id, detail="扫码员已停用")
                    changed = True
            if changed:
                _save_scan_tasks(tasks)
        return _public_scanner(row)


def list_scanners() -> list[dict]:
    with _LOCK:
        scanners = _load_scanners()
        tasks = _load_scan_tasks()
        accounts = _load_accounts()
        if _sync_scan_tasks_locked(accounts, tasks):
            _save_scan_tasks(tasks)
        rows = []
        for scanner in scanners:
            scanner_id = int(scanner.get("id") or 0)
            out = _public_scanner(scanner)
            own = [task for task in tasks if int(task.get("scanner_id") or 0) == scanner_id]
            out["claimed_count"] = sum(task.get("status") == "claimed" for task in own)
            out["scanned_count"] = sum(task.get("status") == "scanned" for task in own)
            out["completed_count"] = sum(task.get("status") == "completed" for task in own)
            rows.append(out)
        return sorted(rows, key=lambda row: int(row.get("id") or 0), reverse=True)


def list_scan_tasks_admin(limit: int = 500) -> list[dict]:
    with _LOCK:
        accounts = _load_accounts()
        tasks = _load_scan_tasks()
        scanners = _load_scanners()
        if _sync_scan_tasks_locked(accounts, tasks):
            _save_scan_tasks(tasks)
        account_by_id = {int(row.get("id") or 0): row for row in accounts}
        scanner_by_id = {int(row.get("id") or 0): row for row in scanners}
        rows = sorted(tasks, key=lambda row: int(row.get("id") or 0), reverse=True)
        return [
            _decorate_scan_task(
                task,
                account_by_id.get(int(task.get("account_id") or 0)),
                scanner_by_id,
                reveal_link=True,
            )
            for task in rows[:max(1, min(int(limit), 5000))]
        ]


def _scanner_active_claim_count_locked(
    scanner_id: int,
    accounts: list[dict],
    tasks: list[dict],
) -> int:
    scanner_id = int(scanner_id)
    task_count = sum(
        int(task.get("scanner_id") or 0) == scanner_id
        and task.get("status") in {"claimed", "scanned"}
        for task in tasks
    )
    extract_count = sum(
        int(account.get("extract_link_scanner_id") or 0) == scanner_id
        and account.get("extract_link_status") in {"queued", "running"}
        for account in accounts
    )
    return task_count + extract_count


def scanner_active_claim_limit() -> int:
    return _SCAN_MAX_ACTIVE_TASKS


def scanner_active_claim_count(scanner_id: int) -> int:
    with _LOCK:
        accounts = _load_accounts()
        tasks = _load_scan_tasks()
        if _sync_scan_tasks_locked(accounts, tasks):
            _save_scan_tasks(tasks)
        return _scanner_active_claim_count_locked(int(scanner_id), accounts, tasks)


def _scanner_extract_candidates(
    accounts: list[dict],
    tasks: list[dict],
    scanner_id: int,
    limit: int = 200,
) -> list[dict]:
    """返回扫码员可发起提链的账号，以及该扫码员正在提链的账号。"""
    now_ts = time.time()
    active_count = _scanner_active_claim_count_locked(int(scanner_id), accounts, tasks)
    at_limit = active_count >= _SCAN_MAX_ACTIVE_TASKS
    active_account_ids = {
        int(task.get("account_id") or 0)
        for task in tasks
        if task.get("status") in {"pending", "claimed", "scanned"}
    }
    rows: list[dict] = []
    for account in accounts:
        account_id = int(account.get("id") or 0)
        plan = str(account.get("current_plan_type") or account.get("plan_type") or "").strip().lower()
        if (
            account_id <= 0
            or account_id in active_account_ids
            or plan != "free"
            or not bool(account.get("plus_trial_eligible"))
            or not str(account.get("access_token") or "").strip()
        ):
            continue

        status = str(account.get("extract_link_status") or "ready").strip().lower()
        owner_id = int(account.get("extract_link_scanner_id") or 0)
        in_progress = status in {"queued", "running"}
        if in_progress and owner_id != int(scanner_id):
            continue

        expires_ts = _extract_link_expiry_timestamp(account.get("extract_link_expires_at"))
        link_is_current = expires_ts is None or expires_ts > now_ts
        if _account_has_scannable_link(account) and link_is_current:
            continue

        rows.append({
            "account_id": account_id,
            "email": _masked_scan_email(account.get("email") or ""),
            "extract_link_status": status,
            "extract_link_type": account.get("extract_link_type") or "",
            "extract_link_message": account.get("extract_link_error") or account.get("extract_link_message") or "",
            "owned": owner_id == int(scanner_id),
            "can_extract": not in_progress and not at_limit,
            "limit_reached": not in_progress and at_limit,
        })

    rows.sort(key=lambda item: (
        0 if item.get("owned") and item.get("extract_link_status") in {"queued", "running"} else 1,
        -int(item.get("account_id") or 0),
    ))
    return rows[:max(1, min(int(limit), 500))]


def get_scanner_queue(scanner_id: int) -> dict:
    with _LOCK:
        scanners = _load_scanners()
        scanner = next((
            row for row in scanners
            if int(row.get("id") or 0) == int(scanner_id)
        ), None)
        if scanner is None or not bool(scanner.get("enabled")):
            raise PermissionError("扫码员已停用或不存在")
        accounts = _load_accounts()
        tasks = _load_scan_tasks()
        if _sync_scan_tasks_locked(accounts, tasks):
            _save_scan_tasks(tasks)
        account_by_id = {int(row.get("id") or 0): row for row in accounts}
        scanner_by_id = {int(row.get("id") or 0): row for row in scanners}
        pending = [
            _decorate_scan_task(
                task,
                account_by_id.get(int(task.get("account_id") or 0)),
                scanner_by_id,
                reveal_link=False,
            )
            for task in sorted(tasks, key=lambda row: int(row.get("id") or 0))
            if task.get("status") == "pending"
        ]
        mine = [
            _decorate_scan_task(
                task,
                account_by_id.get(int(task.get("account_id") or 0)),
                scanner_by_id,
                reveal_link=task.get("status") == "claimed",
            )
            for task in sorted(tasks, key=lambda row: int(row.get("id") or 0), reverse=True)
            if (
                int(task.get("scanner_id") or 0) == int(scanner_id)
                and task.get("status") in {"claimed", "scanned", "completed"}
            )
        ]
        extract_candidates = _scanner_extract_candidates(accounts, tasks, int(scanner_id))
        active_count = _scanner_active_claim_count_locked(int(scanner_id), accounts, tasks)
        return {
            "scanner": _public_scanner(scanner),
            "extract_candidates": extract_candidates,
            "pending": pending,
            "mine": mine,
            "counts": {
                "active": active_count,
                "active_limit": _SCAN_MAX_ACTIVE_TASKS,
                "extractable": sum(bool(item.get("can_extract")) for item in extract_candidates),
                "extracting": sum(
                    item.get("extract_link_status") in {"queued", "running"}
                    for item in extract_candidates
                ),
                "pending": len(pending),
                "claimed": sum(task.get("status") == "claimed" for task in mine),
                "scanned": sum(task.get("status") == "scanned" for task in mine),
                "completed": sum(task.get("status") == "completed" for task in mine),
            },
        }


def claim_scan_task(task_id: int, scanner_id: int, lease_seconds: int = _SCAN_LEASE_SECONDS) -> dict:
    """原子领取扫码任务；同一任务只会被一个扫码员领取。"""
    with _LOCK:
        scanners = _load_scanners()
        scanner = next((
            row for row in scanners
            if int(row.get("id") or 0) == int(scanner_id)
        ), None)
        if scanner is None or not bool(scanner.get("enabled")):
            raise PermissionError("扫码员已停用或不存在")
        accounts = _load_accounts()
        tasks = _load_scan_tasks()
        changed = _sync_scan_tasks_locked(accounts, tasks)
        task = next((row for row in tasks if int(row.get("id") or 0) == int(task_id)), None)
        if task is None:
            if changed:
                _save_scan_tasks(tasks)
            raise LookupError("扫码任务不存在")
        if task.get("status") != "pending":
            if changed:
                _save_scan_tasks(tasks)
            raise RuntimeError("该任务已被领取或已结束")
        if _scanner_active_claim_count_locked(int(scanner_id), accounts, tasks) >= _SCAN_MAX_ACTIVE_TASKS:
            if changed:
                _save_scan_tasks(tasks)
            raise RuntimeError(f"每个扫码员最多同时领取 {_SCAN_MAX_ACTIVE_TASKS} 个任务")
        now_ts = time.time()
        lease_end = now_ts + max(60, min(int(lease_seconds), 3600))
        expires_ts = float(task.get("link_expires_ts") or 0)
        if expires_ts:
            lease_end = min(lease_end, expires_ts)
        if lease_end <= now_ts:
            task["status"] = "expired"
            task["updated_at"] = _now()
            _task_event(task, "expired")
            _save_scan_tasks(tasks)
            raise RuntimeError("支付链接已过期")
        task["status"] = "claimed"
        task["scanner_id"] = int(scanner_id)
        task["claimed_at"] = _now()
        task["lease_expires_at"] = lease_end
        task["updated_at"] = _now()
        _task_event(task, "claimed", scanner_id=scanner_id)
        _save_scan_tasks(tasks)
        account_by_id = {int(row.get("id") or 0): row for row in accounts}
        scanner_by_id = {int(row.get("id") or 0): row for row in scanners}
        return _decorate_scan_task(
            task,
            account_by_id.get(int(task.get("account_id") or 0)),
            scanner_by_id,
            reveal_link=True,
        )


def update_scan_task_by_scanner(task_id: int, scanner_id: int, action: str) -> dict:
    action = str(action or "").strip().lower()
    if action != "release":
        raise ValueError("扫码结果由系统自动检测，扫码员仅可释放任务")
    with _LOCK:
        scanners = _load_scanners()
        scanner = next((
            row for row in scanners
            if int(row.get("id") or 0) == int(scanner_id)
        ), None)
        if scanner is None or not bool(scanner.get("enabled")):
            raise PermissionError("扫码员已停用或不存在")
        accounts = _load_accounts()
        tasks = _load_scan_tasks()
        changed = _sync_scan_tasks_locked(accounts, tasks)
        task = next((row for row in tasks if int(row.get("id") or 0) == int(task_id)), None)
        if task is None:
            if changed:
                _save_scan_tasks(tasks)
            raise LookupError("扫码任务不存在")
        if (
            task.get("status") != "claimed"
            or int(task.get("scanner_id") or 0) != int(scanner_id)
        ):
            if changed:
                _save_scan_tasks(tasks)
            raise PermissionError("该任务不属于当前扫码员或领取已失效")
        now = _now()
        task["status"] = "pending"
        task["scanner_id"] = None
        task["claimed_at"] = None
        task["lease_expires_at"] = None
        _task_event(task, "released", scanner_id=scanner_id)
        task["updated_at"] = now
        _save_scan_tasks(tasks)
        account_by_id = {int(row.get("id") or 0): row for row in accounts}
        scanner_by_id = {int(row.get("id") or 0): row for row in scanners}
        return _decorate_scan_task(
            task,
            account_by_id.get(int(task.get("account_id") or 0)),
            scanner_by_id,
            reveal_link=task.get("status") == "claimed",
        )


def release_scan_task_by_admin(task_id: int) -> dict:
    with _LOCK:
        scanners = _load_scanners()
        accounts = _load_accounts()
        tasks = _load_scan_tasks()
        changed = _sync_scan_tasks_locked(accounts, tasks)
        task = next((row for row in tasks if int(row.get("id") or 0) == int(task_id)), None)
        if task is None:
            if changed:
                _save_scan_tasks(tasks)
            raise LookupError("扫码任务不存在")
        if task.get("status") != "claimed":
            if changed:
                _save_scan_tasks(tasks)
            raise RuntimeError("只有已领取任务可以释放")
        scanner_id = task.get("scanner_id")
        task["status"] = "pending"
        task["scanner_id"] = None
        task["claimed_at"] = None
        task["lease_expires_at"] = None
        task["updated_at"] = _now()
        _task_event(task, "released", scanner_id=scanner_id, detail="管理员释放")
        _save_scan_tasks(tasks)
        account_by_id = {int(row.get("id") or 0): row for row in accounts}
        scanner_by_id = {int(row.get("id") or 0): row for row in scanners}
        return _decorate_scan_task(
            task,
            account_by_id.get(int(task.get("account_id") or 0)),
            scanner_by_id,
            reveal_link=True,
        )


def update_account_note(acc_id: int, note: str) -> bool:
    """更新单个已注册账号备注。note 为空字符串时表示清空备注。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["note"] = str(note or "")
        row["note_updated_at"] = now
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_accounts_note(account_ids: list[int] | None, note: str) -> tuple[list[dict], list[dict]]:
    """
    批量更新已注册账号备注。
    返回 (updated, skipped)，updated/skipped 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        text = str(note or "")
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["note"] = text
            row["note_updated_at"] = now
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "note": text, "note_updated_at": now})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def archive_account(acc_id: int, archived: bool = True) -> bool:
    """归档/取消归档单个已注册账号。归档不会删除 token，只影响默认账号列表查询。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["archived"] = bool(archived)
        row["archived_at"] = now if archived else None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def archive_accounts(account_ids: list[int] | None, archived: bool = True) -> tuple[list[dict], list[dict]]:
    """批量归档/取消归档账号。返回 (updated, skipped)。"""
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["archived"] = bool(archived)
            row["archived_at"] = now if archived else None
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "archived": bool(archived), "archived_at": row.get("archived_at")})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def count_accounts() -> int:
    with _LOCK:
        return len(_load_accounts())


def delete_account(acc_id: int | None = None, email: str | None = None) -> bool:
    """删除一个已注册账号记录，并同步刷新 注册成功的邮箱.txt / token.txt / 静态查看页。"""
    with _LOCK:
        rows = _load_accounts()
        target_email = (email or "").lower()
        new_rows = []
        deleted = False
        for row in rows:
            match_id = acc_id is not None and int(row.get("id") or 0) == int(acc_id)
            match_email = bool(target_email) and (row.get("email") or "").lower() == target_email
            if match_id or match_email:
                deleted = True
                continue
            new_rows.append(row)
        if not deleted:
            return False
        _save_accounts(new_rows)
        return True


def delete_accounts(account_ids: list[int] | None = None, emails: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """
    批量删除已注册账号。
    返回 (deleted, skipped)，deleted 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().isdigit()}
    email_set = {(e or "").lower() for e in (emails or []) if e}
    deleted: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        new_rows = []
        seen_ids: set[int] = set()
        seen_emails: set[str] = set()
        for row in rows:
            row_id = int(row.get("id") or 0)
            row_email = (row.get("email") or "").lower()
            if row_id in ids or row_email in email_set:
                deleted.append({"id": row_id, "email": row.get("email")})
                seen_ids.add(row_id)
                seen_emails.add(row_email)
                continue
            new_rows.append(row)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        for item in email_set - seen_emails:
            skipped.append({"email": item, "reason": "账号不存在"})
        if deleted:
            _save_accounts(new_rows)
    return deleted, skipped


# ============================================================
# outlook_pool
# ============================================================

def import_outlook_accounts(records: list[dict]) -> tuple[int, int]:
    """
    批量导入 Outlook 账号。
    records 元素：{email, password, client_id, refresh_token}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_outlook()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "password": (raw.get("password") or "").strip(),
                "client_id": (raw.get("client_id") or raw.get("clientId") or "").strip(),
                "refresh_token": (raw.get("refresh_token") or raw.get("refreshToken") or "").strip(),
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _outlook_line(row)
            rows.append(row)
            inserted += 1
        _save_outlook(rows)
        return inserted, skipped


def import_registered_email_accounts(records: list[dict], source: str | None) -> tuple[int, int]:
    """
    把邮箱素材直接导入为“已注册成功账号”，用于跳过注册、直接在账号页补跑 Codex 授权。

    source:
      - outlook: records 元素 {email,password,client_id,refresh_token[,access_token,totp_secret]}
      - generic_api: records 元素 {email,code_url[,access_token,totp_secret]}

    返回 (新增账号数, 跳过数)。已存在账号会跳过；邮箱池中已存在的素材会复用并标记 used。
    """
    source = (source or "").strip().lower()
    if source not in ("outlook", "generic_api"):
        raise ValueError("source 必须显式传入 outlook / generic_api")

    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        inserted = skipped = 0

        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(accounts, email):
                skipped += 1
                continue

            now = _now()
            original_line = email
            pool_row = None

            if source == "generic_api":
                code_url = (raw.get("code_url") or raw.get("url") or "").strip()
                if not code_url:
                    skipped += 1
                    continue
                pool_row = _find_by_email(generic_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(generic_rows),
                        "email": email,
                        "code_url": code_url,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    generic_rows.append(pool_row)
                else:
                    pool_row["code_url"] = code_url or pool_row.get("code_url")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _generic_api_email_line(pool_row)
                original_line = _generic_api_email_line(pool_row)
            else:
                password = (raw.get("password") or "").strip()
                client_id = (raw.get("client_id") or raw.get("clientId") or "").strip()
                refresh_token = (raw.get("refresh_token") or raw.get("refreshToken") or "").strip()
                if not (password and client_id and refresh_token):
                    skipped += 1
                    continue
                pool_row = _find_by_email(outlook_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(outlook_rows),
                        "email": email,
                        "password": password,
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    outlook_rows.append(pool_row)
                else:
                    pool_row["password"] = password or pool_row.get("password")
                    pool_row["client_id"] = client_id or pool_row.get("client_id")
                    pool_row["refresh_token"] = refresh_token or pool_row.get("refresh_token")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _outlook_line(pool_row)
                original_line = _outlook_line(pool_row)

            row_id = _next_id(accounts)
            access_token = (raw.get("access_token") or raw.get("token") or "").strip()
            totp_secret = (raw.get("totp_secret") or raw.get("totp") or "").strip() or None
            account = {
                "id": row_id,
                "email": email,
                "created_at": now,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "user_id": raw.get("user_id"),
                "user_name": raw.get("user_name") or "Imported Account",
                "plan_type": raw.get("plan_type"),
                "expires_at": raw.get("expires_at"),
                "device_id": raw.get("device_id"),
                "proxy_used": raw.get("proxy_used"),
                "email_source": source,
                "extra_json": json.dumps({"imported_registered": True}, ensure_ascii=False),
                "codex_status": raw.get("codex_status") or "",
                "codex_error": raw.get("codex_error"),
                "updated_at": now,
                "original_email_line": original_line,
            }
            if source == "outlook":
                account["password"] = pool_row.get("password")
                account["client_id"] = pool_row.get("client_id")
                account["refresh_token"] = pool_row.get("refresh_token")
            account["copy_line"] = _account_line(account)
            accounts.append(account)

            pool_row["registered_account_id"] = row_id
            pool_row["access_token"] = access_token
            if totp_secret:
                pool_row["totp_secret"] = totp_secret
            inserted += 1

        _save_outlook(outlook_rows)
        _save_generic_api_emails(generic_rows)
        _save_accounts(accounts)
        return inserted, skipped


def claim_next_outlook() -> dict | None:
    """原子领取一个可用 Outlook 账号并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_outlook(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_outlook(rows)
        return _decorate_outlook(row)


def release_outlook(email: str, status: str = "available", note: str | None = None) -> None:
    """把账号状态改回 available，或标记为 used/failed/disabled。"""
    with _LOCK:
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_outlook(rows)


def release_unconsumed_outlook(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的 Outlook 邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_outlook(rows)
        return True


def delete_outlook(email: str) -> bool:
    """从邮箱池彻底删除一个邮箱（按 email 匹配）。返回是否删到。"""
    with _LOCK:
        rows = _load_outlook()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_outlook(new_rows)
        return True


def list_outlook_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = _load_outlook()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [_decorate_outlook(r, account_by_email) for r in rows[:limit]]


def outlook_pool_summary() -> dict:
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        for row in _load_outlook():
            status = row.get("status") or "available"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_outlook_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_outlook(), email)
        return _decorate_outlook(row) if row else None


# ============================================================
# generic_api email pool
# ============================================================

def import_generic_api_emails(records: list[dict]) -> tuple[int, int]:
    """
    批量导入通用 API 取码邮箱。
    records 元素：{email, code_url}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_generic_api_emails()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            code_url = (raw.get("code_url") or raw.get("url") or "").strip()
            if not email or not code_url:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": code_url,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _generic_api_email_line(row)
            rows.append(row)
            inserted += 1
        _save_generic_api_emails(rows)
        return inserted, skipped


def claim_next_generic_api_email() -> dict | None:
    """原子领取一个可用通用 API 邮箱并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_generic_api_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_generic_api_emails(rows)
        return _decorate_generic_api_email(row)


def release_generic_api_email(email: str, status: str = "available", note: str | None = None) -> None:
    """把通用 API 邮箱状态改回 available，或标记为 failed/used。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)


def release_unconsumed_generic_api_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的通用 API 邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)
        return True


def delete_generic_api_email(email: str) -> bool:
    """从通用 API 邮箱池彻底删除一个邮箱。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_generic_api_emails(new_rows)
        return True


def list_generic_api_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = _load_generic_api_emails()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [_decorate_generic_api_email(r, account_by_email) for r in rows[:limit]]


def generic_api_email_pool_summary() -> dict:
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        for row in _load_generic_api_emails():
            status = row.get("status") or "available"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_generic_api_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_generic_api_emails(), email)
        return _decorate_generic_api_email(row) if row else None


# ============================================================
# Codex 授权账号（来自 codex_accounts/codex-邮箱-plan.json）
# ============================================================

def _load_codex_export_state() -> dict:
    """读导出状态映射 {filename: {exported_at, exported_count}}。不存在返回 {}。"""
    data = _read_json(_CODEX_EXPORT_STATE, {})
    return data if isinstance(data, dict) else {}


def _save_codex_export_state(state: dict) -> None:
    _write_json(_CODEX_EXPORT_STATE, state)


def list_codex_accounts() -> list[dict]:
    """
    扫 codex_accounts/ 目录，每个 codex-*.json 是一条 CPA 兼容凭证。
    返回带元信息的列表（含导出状态、文件大小、token 预览等）。
    """
    with _LOCK:
        out = []
        if not _CODEX_DIR.exists():
            return out
        export_state = _load_codex_export_state()
        for path in sorted(_CODEX_DIR.glob("codex-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            fname = path.name
            es = export_state.get(fname) or {}
            # 从文件名抽 email 和 plan：codex-{email}.json 或 codex-{email}-{plan}.json
            stem = path.stem  # codex-邮箱-plan
            without_prefix = stem[len("codex-"):] if stem.startswith("codex-") else stem
            # plan 可能为空。简单做法：直接读 JSON 里的 email（更准），文件名只做 fallback
            email = content.get("email") or ""
            if not email:
                # JSON 里 email 为空（旧 bug 产物），从文件名兜底
                # 文件名格式 codex-{email}-{plan}.json，email 里可能有 - 但是常见邮箱不会有
                # 简单做法：去掉末尾 -plan（如 -free / -plus / -team），剩下的当 email
                parts = without_prefix.rsplit("-", 1)
                if len(parts) == 2 and parts[1].lower() in ("free", "plus", "team", "pro", "enterprise"):
                    email = parts[0]
                else:
                    email = without_prefix
            # 推断 plan
            plan = ""
            if "-" in without_prefix:
                tail = without_prefix.rsplit("-", 1)[-1].lower()
                if tail in ("free", "plus", "team", "pro", "enterprise"):
                    plan = tail
            out.append({
                "filename": fname,
                "path": str(path),
                "email": email,
                "plan": plan,
                "account_id": content.get("account_id", ""),
                "type": content.get("type", "codex"),
                "last_refresh": content.get("last_refresh", ""),
                "expired": content.get("expired", ""),
                "access_token_preview": (content.get("access_token", "") or "")[:32],
                "size": path.stat().st_size,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "exported_at": es.get("exported_at"),
                "exported_count": es.get("exported_count", 0),
            })
        return out


def read_codex_credential(filename: str) -> tuple[str, str]:
    """
    读取一个 codex-*.json 文件原始内容。
    Returns: (content_string, filename)
    抛 ValueError：文件名不合法（防目录穿越）/ 不存在。
    """
    with _LOCK:
        # 防注入：只允许 codex-*.json 模式，不允许路径分隔符
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            raise ValueError(f"文件不存在: {filename}")
        return path.read_text(encoding="utf-8"), filename


def mark_codex_exported(filename: str) -> dict:
    """
    标记某个 codex 凭证已导出（导出计数 +1，记录最近导出时间）。
    Returns: 该 filename 当前的导出状态记录。
    """
    with _LOCK:
        state = _load_codex_export_state()
        rec = state.get(filename) or {"exported_count": 0}
        rec["exported_count"] = int(rec.get("exported_count", 0)) + 1
        rec["exported_at"] = _now()
        state[filename] = rec
        _save_codex_export_state(state)
        return rec


def reset_codex_exported(filename: str) -> None:
    """清掉某个 codex 凭证的导出状态（用户想重置时用）。"""
    with _LOCK:
        state = _load_codex_export_state()
        if filename in state:
            del state[filename]
            _save_codex_export_state(state)


def delete_codex_credential(filename: str) -> bool:
    """删除一个本地 codex-*.json 凭证文件，并清理导出状态。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            return False
        path.unlink()
        state = _load_codex_export_state()
        if filename in state:
            del state[filename]
            _save_codex_export_state(state)
        return True


def codex_accounts_summary() -> dict:
    """codex 账号汇总：总数 / 已导出 / 未导出。"""
    with _LOCK:
        rows = list_codex_accounts()
        total = len(rows)
        exported = sum(1 for r in rows if r.get("exported_count", 0) > 0)
        return {
            "total": total,
            "exported": exported,
            "pending": total - exported,
        }


# ============================================================
# registration_jobs
# ============================================================

def _new_job_row(
    rows: list[dict],
    *,
    email_source: str,
    job_type: str = "registration",
    parent_job_id: int | None = None,
    root_job_id: int | None = None,
    retry_attempt: int = 0,
    retry_action: str | None = None,
    email: str | None = None,
    account_id: int | None = None,
) -> dict:
    job_uuid = str(uuid.uuid4())
    log_file = str(_LOG_DIR / f"{job_uuid}.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    return {
        "id": _next_id(rows),
        "job_uuid": job_uuid,
        "job_type": job_type,
        "parent_job_id": parent_job_id,
        "root_job_id": root_job_id,
        "retry_attempt": int(retry_attempt or 0),
        "retry_action": retry_action,
        "email_source": email_source,
        "email": email,
        "status": "pending",
        "error_message": None,
        "log_file": log_file,
        "started_at": None,
        "completed_at": None,
        "account_id": account_id,
        "created_at": _now(),
    }


def create_job(email_source: str) -> dict:
    """创建一个首次执行的 pending 注册任务。"""
    with _LOCK:
        rows = _load_jobs()
        row = _new_job_row(rows, email_source=email_source)
        rows.append(row)
        _save_jobs(rows)
        return dict(row)


def create_retry_job(
    source_job_id: int,
    *,
    job_type: str,
    email_source: str,
    email: str | None = None,
    account_id: int | None = None,
) -> tuple[dict, bool]:
    """原子创建重试子任务；同一任务链已有活跃任务时直接复用。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(source_job_id)), None)
        if source is None:
            raise LookupError("任务不存在")
        if source.get("status") not in ("failed", "stopped", "cancelled"):
            raise ValueError(f"当前状态不支持重试：{source.get('status')}")

        root_id = int(source.get("root_job_id") or source.get("id"))
        active_states = {"pending", "running", "stopping"}
        active = next((
            r for r in rows
            if int(r.get("id") or 0) != int(source_job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") in active_states
        ), None)
        if active is not None:
            if active.get("job_type", "registration") != job_type:
                raise ValueError(f"已有其他类型重试任务 #{active.get('id')} 在排队或运行中")
            return dict(active), False

        attempts = [
            int(r.get("retry_attempt") or 0)
            for r in rows
            if int(r.get("id") or 0) == root_id or int(r.get("root_job_id") or 0) == root_id
        ]
        row = _new_job_row(
            rows,
            email_source=email_source,
            job_type=job_type,
            parent_job_id=int(source_job_id),
            root_job_id=root_id,
            retry_attempt=(max(attempts) if attempts else 0) + 1,
            retry_action=("codex" if job_type == "codex_retry" else "registration"),
            email=email,
            account_id=account_id,
        )
        rows.append(row)
        _save_jobs(rows)
        return dict(row), True


def update_job(
    job_id: int,
    *,
    status: str | None = None,
    email: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    account_id: int | None = None,
) -> None:
    with _LOCK:
        rows = _load_jobs()
        row = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if row is None:
            return
        if status is not None:
            row["status"] = status
        if email is not None:
            row["email"] = email
        if error is not None:
            row["error_message"] = error
        if started_at is not None:
            row["started_at"] = started_at
        if completed_at is not None:
            row["completed_at"] = completed_at
        if account_id is not None:
            row["account_id"] = account_id
        _save_jobs(rows)


def list_jobs(limit: int = 100) -> list[dict]:
    with _LOCK:
        rows = sorted(_load_jobs(), key=lambda x: int(x.get("id") or 0), reverse=True)
        return [dict(r) for r in rows[:limit]]


def list_jobs_page(limit: int = 20, offset: int = 0) -> tuple[list[dict], int, int, int]:
    """读取任务分页数据，同时返回总数、活跃数和排队数。"""
    with _LOCK:
        rows = sorted(_load_jobs(), key=lambda x: int(x.get("id") or 0), reverse=True)
        start = max(0, int(offset or 0))
        size = max(1, int(limit or 20))
        items = [dict(r) for r in rows[start:start + size]]
        active_count = sum(1 for row in rows if row.get("status") in {"pending", "running", "stopping"})
        pending_count = sum(1 for row in rows if row.get("status") == "pending")
        return items, len(rows), active_count, pending_count


def get_job(job_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_jobs() if int(r.get("id") or 0) == int(job_id)), None)
        return dict(row) if row else None


def recover_interrupted_registration_jobs() -> dict[str, int]:
    """结束因进程重启遗留的注册任务，并回收其中尚未生成账号的邮箱。"""
    active_states = {"pending", "running", "stopping"}
    now = _now()
    reason = "WebUI 重启导致注册任务中断"
    note = "任务因 WebUI 重启中断，确认未生成账号后已自动回收"

    with _LOCK:
        jobs = _load_jobs()
        interrupted = [
            row
            for row in jobs
            if str(row.get("job_type") or "registration") == "registration"
            and row.get("status") in active_states
        ]
        if not interrupted:
            return {"jobs": 0, "emails": 0}

        account_emails = {
            str(row.get("email") or "").strip().lower()
            for row in _load_accounts()
            if str(row.get("email") or "").strip()
        }
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        domain_rows = _load_domain_pool()
        released_emails: set[str] = set()
        outlook_changed = generic_changed = domain_changed = False

        for job in interrupted:
            job["status"] = "failed"
            job["completed_at"] = now
            job["error_message"] = reason

            email = str(job.get("email") or "").strip()
            email_key = email.lower()
            if not email_key or email_key in account_emails or email_key in released_emails:
                continue

            for rows, pool_name in (
                (outlook_rows, "outlook"),
                (generic_rows, "generic"),
                (domain_rows, "domain"),
            ):
                pool_row = _find_by_email(rows, email)
                if pool_row is None or pool_row.get("status") != "used":
                    continue
                pool_row["status"] = "available"
                pool_row["used_at"] = None
                pool_row["note"] = note
                released_emails.add(email_key)
                if pool_name == "outlook":
                    outlook_changed = True
                elif pool_name == "generic":
                    generic_changed = True
                else:
                    domain_changed = True
                break

        _save_jobs(jobs)
        if outlook_changed:
            _save_outlook(outlook_rows)
        if generic_changed:
            _save_generic_api_emails(generic_rows)
        if domain_changed:
            _save_domain_pool(domain_rows)
        return {"jobs": len(interrupted), "emails": len(released_emails)}


def get_successful_retry_for_job(job_id: int) -> dict | None:
    """返回同一任务链中已成功的其他重试任务，用于保留原任务历史状态并阻止重复重试。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if source is None:
            return None
        root_id = int(source.get("root_job_id") or source.get("id") or 0)
        matches = [
            r for r in rows
            if int(r.get("id") or 0) != int(job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") == "success"
        ]
        if not matches:
            return None
        return dict(max(matches, key=lambda r: int(r.get("id") or 0)))


def get_successful_retry_map_for_jobs(job_ids: set[int] | list[int] | tuple[int, ...]) -> dict[int, dict]:
    """一次读取任务文件，返回多个任务对应的成功重试任务。"""
    wanted = set()
    for value in job_ids or ():
        try:
            wanted.add(int(value))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return {}

    with _LOCK:
        rows = _load_jobs()
        sources = {
            int(row.get("id") or 0): row
            for row in rows
            if int(row.get("id") or 0) in wanted
        }
        root_ids = {
            int(source.get("root_job_id") or source.get("id") or 0)
            for source in sources.values()
        }
        latest_by_root: dict[int, dict] = {}
        for row in rows:
            if row.get("status") != "success":
                continue
            root_id = int(row.get("root_job_id") or 0)
            if root_id not in root_ids:
                continue
            current = latest_by_root.get(root_id)
            if current is None or int(row.get("id") or 0) > int(current.get("id") or 0):
                latest_by_root[root_id] = row

        result = {}
        for job_id, source in sources.items():
            root_id = int(source.get("root_job_id") or source.get("id") or 0)
            match = latest_by_root.get(root_id)
            if match is not None and int(match.get("id") or 0) != job_id:
                result[job_id] = dict(match)
        return result


def get_retry_account_index() -> tuple[dict[int, dict], dict[str, dict]]:
    """一次读取账号文件，返回补跑判断所需的轻量账号索引。"""
    with _LOCK:
        rows = _load_accounts()
        by_id: dict[int, dict] = {}
        by_email: dict[str, dict] = {}
        for row in rows:
            item = {
                "id": row.get("id"),
                "email": row.get("email"),
                "codex_status": row.get("codex_status"),
            }
            try:
                account_id = int(row.get("id") or 0)
            except (TypeError, ValueError):
                account_id = 0
            if account_id:
                by_id[account_id] = item
            email = str(row.get("email") or "").strip().lower()
            if email:
                by_email[email] = item
        return by_id, by_email


def delete_job(job_id: int, *, delete_log: bool = True, allow_running: bool = False) -> bool:
    """
    删除一个注册任务记录；默认同时删除该任务日志文件。返回是否删除到记录。
    默认不删除 running 任务，避免后台线程仍在执行但前端记录消失。
    """
    with _LOCK:
        rows = _load_jobs()
        idx = next((i for i, r in enumerate(rows) if int(r.get("id") or 0) == int(job_id)), None)
        if idx is None:
            return False
        if not allow_running and rows[idx].get("status") in ("running", "stopping"):
            return False
        row = rows.pop(idx)
        _save_jobs(rows)

    if delete_log:
        log_file = row.get("log_file")
        if log_file:
            try:
                Path(log_file).unlink(missing_ok=True)
            except Exception:
                pass
    return True


# ============================================================
# 迁移与路径
# ============================================================

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _migrate_legacy_sqlite() -> dict:
    summary = {"sqlite_accounts_imported": 0, "sqlite_outlook_imported": 0, "sqlite_outlook_skipped": 0}
    if not _LEGACY_SQLITE.exists():
        return summary
    try:
        conn = sqlite3.connect(str(_LEGACY_SQLITE))
        conn.row_factory = sqlite3.Row
        if _table_exists(conn, "outlook_pool"):
            records = []
            statuses = []
            for row in conn.execute("SELECT * FROM outlook_pool").fetchall():
                records.append({
                    "email": row["email"],
                    "password": row["password"],
                    "client_id": row["client_id"],
                    "refresh_token": row["refresh_token"],
                })
                statuses.append({
                    "email": row["email"],
                    "status": row["status"],
                    "note": row["note"],
                })
            ins, skip = import_outlook_accounts(records)
            for item in statuses:
                if item["status"] != "available":
                    release_outlook(item["email"], status=item["status"], note=item["note"])
            summary["sqlite_outlook_imported"] += ins
            summary["sqlite_outlook_skipped"] += skip
        if _table_exists(conn, "registered_accounts"):
            for row in conn.execute("SELECT * FROM registered_accounts").fetchall():
                insert_account(
                    email=row["email"],
                    access_token=row["access_token"],
                    totp_secret=row["totp_secret"],
                    user_id=row["user_id"],
                    user_name=row["user_name"],
                    plan_type=row["plan_type"],
                    expires_at=row["expires_at"],
                    device_id=row["device_id"],
                    proxy_used=row["proxy_used"],
                    email_source=row["email_source"],
                    extra=json.loads(row["extra_json"]) if row["extra_json"] else None,
                )
                summary["sqlite_accounts_imported"] += 1
        conn.close()
    except Exception as exc:
        summary["sqlite_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def migrate_legacy_files() -> dict:
    """
    把历史 SQLite、accounts/*.json、outlook_accounts.txt、outlook_accounts_used.json
    迁移到当前 JSON/TXT 文件存储。多次调用是幂等的。
    """
    summary = {
        "accounts_imported": 0,
        "outlook_imported": 0,
        "outlook_skipped": 0,
    }
    summary.update(_migrate_legacy_sqlite())

    accounts_dir = _PROJECT_ROOT / "accounts"
    if accounts_dir.exists():
        for jf in accounts_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if not data.get("email") or not data.get("access_token"):
                    continue
                extra = data.get("extra") or {}
                user = extra.get("user") or {}
                account = extra.get("account") or {}
                insert_account(
                    email=data["email"],
                    access_token=data["access_token"],
                    totp_secret=data.get("totp_secret"),
                    user_id=user.get("id"),
                    user_name=user.get("name"),
                    plan_type=account.get("planType"),
                    expires_at=extra.get("expires"),
                    device_id=extra.get("device_id"),
                    extra=extra,
                )
                summary["accounts_imported"] += 1
            except Exception:
                continue

    for txt in (_PROJECT_ROOT / "outlook_accounts.txt", _OUTLOOK_TXT):
        if txt.exists():
            records = []
            for line in txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                # 支持 4 段或 6 段格式
                if len(parts) == 4:
                    email, password, client_id, refresh_token = (p.strip() for p in parts)
                elif len(parts) == 6:
                    email, password, client_id, refresh_token, _, _ = (p.strip() for p in parts)
                else:
                    continue
                records.append({
                    "email": email,
                    "password": password,
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                })
            ins, skip = import_outlook_accounts(records)
            summary["outlook_imported"] += ins
            summary["outlook_skipped"] += skip

    used = _PROJECT_ROOT / "outlook_accounts_used.json"
    if used.exists():
        try:
            emails = json.loads(used.read_text(encoding="utf-8"))
            for email in emails:
                release_outlook(email, status="used")
        except Exception:
            pass

    return summary


def db_path() -> Path:
    """兼容旧名称，返回当前文件存储目录。"""
    return _DATA_DIR


def storage_paths() -> dict:
    return {
        "outlook_json": str(_OUTLOOK_JSON),
        "outlook_txt": str(_OUTLOOK_TXT),
        "accounts_json": str(_ACCOUNTS_JSON),
        "accounts_txt": str(_ACCOUNTS_TXT),
        "tokens_txt": str(_TOKENS_TXT),
        "viewer_html": str(_VIEWER_HTML),
        "jobs_json": str(_JOBS_JSON),
        "logs_dir": str(_LOG_DIR),
    }


def refresh_static_viewer() -> Path:
    """手动刷新静态查看器，返回 HTML 路径。"""
    with _LOCK:
        outlook_rows = _load_outlook()
        account_rows = _load_accounts()
        _sync_outlook_txt(outlook_rows)
        _sync_accounts_txt(account_rows)
        _sync_tokens_txt(account_rows)
        return _render_static_viewer(outlook_rows=outlook_rows, account_rows=account_rows)


# ============================================================
# Domain email pool（Cloudflare 域名邮箱跟踪）
# ============================================================

_DOMAIN_EMAIL_JSON = _PROJECT_ROOT / "用于注册的域名邮箱.json"


def _load_domain_pool() -> list[dict]:
    rows = _read_json(_DOMAIN_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_domain_pool(rows: list[dict]) -> None:
    _write_json(_DOMAIN_EMAIL_JSON, rows)


def _find_domain_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def claim_next_domain_email(email: str) -> dict:
    """记录一个新的域名邮箱地址到池中（标记为 available）。"""
    with _LOCK:
        rows = _load_domain_pool()
        if _find_domain_email(rows, email):
            # 已存在，直接返回
            row = _find_domain_email(rows, email)
            return row
        row = {
            "id": _next_id(rows),
            "email": email,
            "status": "available",
            "used_at": None,
            "note": None,
            "created_at": _now(),
        }
        rows.append(row)
        _save_domain_pool(rows)
        return dict(row)


def release_domain_email(email: str, status: str = "available", note: str | None = None) -> None:
    """更新域名邮箱状态。"""
    with _LOCK:
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)


def release_unconsumed_domain_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的域名邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)
        return True


def get_domain_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_domain_email(_load_domain_pool(), email)
        return dict(row) if row else None


def list_domain_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        rows = sorted(_load_domain_pool(), key=lambda x: int(x.get("id") or 0), reverse=True)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return [dict(r) for r in rows[:limit]]


def domain_email_pool_summary() -> dict:
    with _LOCK:
        out: dict[str, int] = {"available": 0, "used": 0, "failed": 0}
        for row in _load_domain_pool():
            s = row.get("status") or "available"
            out[s] = out.get(s, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def delete_domain_email(email: str) -> bool:
    """从域名邮箱池删除一个邮箱。"""
    with _LOCK:
        rows = _load_domain_pool()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_domain_pool(new_rows)
        return True
