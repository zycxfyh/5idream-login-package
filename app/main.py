"""5idream（到梦空间）扫码登录与数据查询服务。

原命令行工具改造为 FastAPI 服务：
- 异步化：httpx + asyncio，天然支持高并发；
- 登录态：token 按用户(user_id)做个人缓存，支持 TTL 空闲过期、重复登录自动刷新；
- 分库分页：活动/部落按分类(key)独立查询，配合 page/page_size 单页请求，
  不再一次拉取全部分类；每个查询维度有独立缓存 + 锁，防止并发击穿；
- 提供极简网页 UI（/）方便直接扫码测试。

启动：uvicorn app.main:app --reload  （或 python run.py）
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

import httpx
import qrcode
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .cache import (
    LoginSessionStore,
    PageCache,
    PersonalTokenCache,
    TokenEntry,
)
from .client import (
    ACTIVITY_CATEGORIES,
    ACTIVITY_CATEGORY_MAP,
    TRIBE_CATEGORIES,
    TRIBE_CATEGORY_MAP,
    DreamAPIError,
    DreamClient,
    absolute_url,
    get_qr,
    poll_login_once,
)
from .config import settings
from .formatters import (
    build_processed_activity,
    build_processed_data,
    build_processed_tribe,
    render_activity_detail,
    render_report,
)

logger = logging.getLogger("5idream")

# ------------------------------ 全局单例（进程内内存态） ------------------------------
# 说明：当前为单进程内存缓存。若需多 worker/多机共享，请把这三个对象换成 Redis 实现。
token_cache = PersonalTokenCache(idle_ttl=settings.token_idle_ttl)
session_store = LoginSessionStore(ttl=settings.session_ttl)
page_cache = PageCache(ttl=settings.data_cache_ttl)


# ------------------------------ 生命周期 ------------------------------


async def _cleanup_loop() -> None:
    while True:
        try:
            await asyncio.sleep(60)
            token_cache.cleanup()
            session_store.cleanup()
            page_cache.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 后台清理失败不应中断服务
            logger.exception("后台缓存清理异常")


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="5idream 扫码登录与数据查询服务",
    description="到梦空间扫码登录、个人活动/部落分库分页查询 API",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------ 鉴权 ------------------------------


def _extract_token(authorization: str | None, x_token: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if x_token:
        return x_token.strip()
    return None


def require_token(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_token: str | None = Header(default=None, alias="X-Token"),
) -> TokenEntry:
    token = _extract_token(authorization, x_token)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="缺少登录凭证，请在请求头携带 Authorization: Bearer <token> 或 X-Token",
        )
    entry = token_cache.get_by_token(token)
    if not entry:
        raise HTTPException(
            status_code=401,
            detail="Token 无效或已过期，请重新扫码登录",
        )
    return entry


def _is_auth_error(exc: Exception) -> bool:
    return isinstance(exc, DreamAPIError) and "Token \u9a8c\u8bc1\u5931\u8d25" in str(exc)


# ------------------------------ 工具函数 ------------------------------


def make_qr_png(payload: str) -> str:
    """把二维码 payload 渲染成 data URI 图片，方便浏览器直接显示。"""
    buf = io.BytesIO()
    qrcode.make(payload).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _session_response(session: Any) -> dict[str, Any]:
    base = {"session_id": session.session_id, "status": session.status}
    if session.status == "success":
        user = session.user or {}
        base["token"] = session.token
        base["user"] = _profile_view(user)
    elif session.status == "failed":
        base["error"] = session.error or "登录失败"
    elif session.status == "expired":
        base["error"] = "二维码登录超时，请重新生成二维码"
    return base


def _profile_view(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id"),
        "name": user.get("name") or user.get("realname"),
        "avatar": absolute_url(user.get("logopath")) if user.get("logopath") else None,
    }


def _category_dir(categories: tuple[tuple[str, str, str], ...]) -> dict[str, Any]:
    return {"categories": [{"key": key, "label": label} for key, label, _ in categories]}


async def _fetch_page(
    entry: TokenEntry,
    scope: str,
    category: str,
    endpoint: str,
    page: int,
    page_size: int,
    refresh: bool,
) -> dict[str, Any]:
    """拉取（或复用缓存）某个用户、某个分类的指定一页。

    - 每个查询维度独立缓存 + 独立锁，防止并发翻页/刷新时重复请求 5idream。
    """
    key = page_cache.key_for(entry.user_id, scope, category, page, page_size)
    if not refresh:
        cached = page_cache.get(key)
        if cached is not None:
            return cached

    lock = page_cache.lock_for(key)
    async with lock:
        if not refresh:
            cached = page_cache.get(key)
            if cached is not None:
                return cached
        async with DreamClient(entry.token) as dc:
            try:
                records, total = await dc.fetch_page_one(
                    endpoint, str(entry.user.get("id") or ""), page, page_size
                )
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"请求 5idream 失败: {exc}") from exc
            except DreamAPIError as exc:
                if _is_auth_error(exc):
                    token_cache.remove(entry.token)
                    page_cache.invalidate_user(entry.user_id)
                    raise HTTPException(status_code=401, detail=str(exc)) from exc
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        payload = {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
            "records": records,
        }
        page_cache.put(key, payload)
        return payload


def _apply_processed(
    payload: dict[str, Any],
    kind: str,
    category: str,
    label: str,
    build_one: Any,
) -> dict[str, Any]:
    payload.update({"kind": kind, "category": category, "label": label})
    payload["records"] = [build_one(record) for record in payload["records"]]
    return payload


# ------------------------------ 登录 ------------------------------


@app.post("/api/v1/login/qr", tags=["登录"])
async def create_login_qr() -> dict[str, Any]:
    """创建扫码登录会话，返回 session_id 和二维码（payload + base64 图片）。"""
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout,
            headers={"User-Agent": "Mozilla/5.0", "Referer": f"{settings.base_url}/"},
            follow_redirects=True,
        ) as client:
            security_id, payload = await get_qr(client)
    except (httpx.HTTPError, DreamAPIError) as exc:
        raise HTTPException(status_code=502, detail=f"获取二维码失败: {exc}") from exc

    session = session_store.create(security_id, payload)
    return {
        "session_id": session.session_id,
        "qr_payload": payload,
        "qr_image_base64": make_qr_png(payload),
        "expires_in": settings.login_timeout_seconds,
        "status": "pending",
    }


def _serialize_login_poll(fn):
    @wraps(fn)
    async def wrapped(session_id: str, *args, **kwargs):
        lock = session_store.poll_lock_for(session_id)
        if lock is None:
            return await fn(session_id, *args, **kwargs)
        async with lock:
            return await fn(session_id, *args, **kwargs)

    return wrapped


@app.get("/api/v1/login/status/{session_id}", tags=["登录"])
@_serialize_login_poll
async def login_status(session_id: str) -> dict[str, Any]:
    """轮询扫码结果。pending 时请客户端每隔 2~3 秒调用一次。

    用户确认后，服务端会把 token 写入「个人缓存」（按 user_id），
    并向客户端返回该 token，用于后续数据接口鉴权。
    """
    session = session_store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="登录会话不存在或已过期")

    if session.status != "pending":
        return _session_response(session)

    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout,
            headers={"User-Agent": "Mozilla/5.0", "Referer": f"{settings.base_url}/"},
            follow_redirects=True,
        ) as client:
            token = await poll_login_once(client, session.security_id)
    except (httpx.HTTPError, DreamAPIError) as exc:
        session.status = "failed"
        session.error = f"轮询失败: {exc}"
        return _session_response(session)

    if token:
        try:
            async with DreamClient(token) as dc:
                user = await dc.check_token()
        except (httpx.HTTPError, DreamAPIError) as exc:
            session.status = "failed"
            session.error = f"Token 校验失败: {exc}"
            return _session_response(session)
        user_id = str(user.get("id") or "")
        token_cache.put(user_id, token, user)  # token 个人缓存：按 user_id 写入
        session.status = "success"
        session.token = token
        session.user = user

    return _session_response(session)


# ------------------------------ 分类目录（公开，不请求 5idream） ------------------------------


@app.get("/api/v1/activity-categories", tags=["目录"])
async def activity_categories() -> dict[str, Any]:
    """活动分类目录（分库）。"""
    return _category_dir(ACTIVITY_CATEGORIES)


@app.get("/api/v1/tribe-categories", tags=["目录"])
async def tribe_categories() -> dict[str, Any]:
    """部落分类目录（分库）。"""
    return _category_dir(TRIBE_CATEGORIES)


# ------------------------------ 数据接口（需登录，分库分页） ------------------------------


@app.get("/api/v1/activities", tags=["数据"])
async def get_activities(
    category: str = Query(..., description="活动分类 key，见 /api/v1/activity-categories"),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(10, ge=1, le=100, description="每页条数，1~100"),
    refresh: bool = Query(False, description="为 true 时强制重新请求，忽略缓存"),
    entry: TokenEntry = Depends(require_token),
) -> dict[str, Any]:
    """某个活动分类的某一页（只请求该分类该页，不一次拉全部分类）。"""
    if category not in ACTIVITY_CATEGORY_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"未知活动分类: {category}，可选: {', '.join(ACTIVITY_CATEGORY_MAP)}",
        )
    label, endpoint = ACTIVITY_CATEGORY_MAP[category]
    payload = await _fetch_page(entry, "activity", category, endpoint, page, page_size, refresh)
    payload.update({"kind": "activity", "category": category, "label": label})
    return payload


@app.get("/api/v1/tribes", tags=["数据"])
async def get_tribes(
    category: str = Query(..., description="部落分类 key，见 /api/v1/tribe-categories"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    refresh: bool = Query(False),
    entry: TokenEntry = Depends(require_token),
) -> dict[str, Any]:
    """某个部落分类的某一页。"""
    if category not in TRIBE_CATEGORY_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"未知部落分类: {category}，可选: {', '.join(TRIBE_CATEGORY_MAP)}",
        )
    label, endpoint = TRIBE_CATEGORY_MAP[category]
    payload = await _fetch_page(entry, "tribe", category, endpoint, page, page_size, refresh)
    payload.update({"kind": "tribe", "category": category, "label": label})
    return payload


@app.get("/api/v1/activities/{activity_id}", tags=["数据"])
async def get_activity_detail(
    activity_id: str,
    entry: TokenEntry = Depends(require_token),
) -> dict[str, Any]:
    """活动详情（原始字段 + 可读文本渲染）。"""
    user_id = str(entry.user.get("id") or "")
    async with DreamClient(entry.token) as dc:
        try:
            detail = await dc.fetch_activity_detail(activity_id, user_id)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"请求 5idream 失败: {exc}") from exc
        except DreamAPIError as exc:
            if _is_auth_error(exc):
                token_cache.remove(entry.token)
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"detail": detail, "rendered": render_activity_detail(detail)}


@app.get("/api/v1/data", tags=["数据"])
async def get_data(
    category: str | None = Query(None, description="分类 key；不传则只返回个人信息与分类目录"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    refresh: bool = Query(False),
    entry: TokenEntry = Depends(require_token),
) -> dict[str, Any]:
    """整理后的精简数据（同原 CLI 的 5idream-data.json 口径）。

    - 不传 category：仅返回个人信息 + 活动/部落分类目录（不请求 5idream，即时返回）；
    - 传 category：按该分类分页返回整理后的精简字段。
    """
    if category is None:
        return {
            "profile": _profile_view(entry.user),
            "activity_categories": _category_dir(ACTIVITY_CATEGORIES)["categories"],
            "tribe_categories": _category_dir(TRIBE_CATEGORIES)["categories"],
        }
    if category in ACTIVITY_CATEGORY_MAP:
        label, endpoint = ACTIVITY_CATEGORY_MAP[category]
        payload = await _fetch_page(entry, "activity", category, endpoint, page, page_size, refresh)
        return _apply_processed(payload, "activity", category, label, build_processed_activity)
    if category in TRIBE_CATEGORY_MAP:
        label, endpoint = TRIBE_CATEGORY_MAP[category]
        payload = await _fetch_page(entry, "tribe", category, endpoint, page, page_size, refresh)
        return _apply_processed(payload, "tribe", category, label, build_processed_tribe)
    raise HTTPException(status_code=400, detail=f"未知分类: {category}")


@app.get("/api/v1/report", tags=["数据"])
async def get_report(
    refresh: bool = Query(False),
    entry: TokenEntry = Depends(require_token),
) -> dict[str, Any]:
    """Markdown 汇总报告（同原 CLI 的 5idream-report.md）。

    注意：报告需要一次性拉取全部分类，耗时较长，建议低频使用。
    """
    async with DreamClient(entry.token) as dc:
        try:
            user = entry.user
            data = await dc.collect_account_data(user, max(1, settings.max_rows))
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"请求 5idream 失败: {exc}") from exc
        except DreamAPIError as exc:
            if _is_auth_error(exc):
                token_cache.remove(entry.token)
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"markdown": render_report(data), "processed": build_processed_data(data)}


@app.get("/api/v1/me", tags=["数据"])
async def get_me(entry: TokenEntry = Depends(require_token)) -> dict[str, Any]:
    """当前登录用户信息（来自个人缓存，不额外请求 5idream）。"""
    return {"user_id": entry.user_id, "user": _profile_view(entry.user)}


@app.post("/api/v1/logout", tags=["数据"])
async def logout(entry: TokenEntry = Depends(require_token)) -> dict[str, Any]:
    """登出：把该 token 从个人缓存中移除。"""
    token_cache.remove(entry.token)
    page_cache.invalidate_user(entry.user_id)
    return {"ok": True}


@app.get("/healthz", tags=["系统"])
async def healthz() -> dict[str, Any]:
    return {"ok": True, "cached_users": token_cache.count()}


# ------------------------------ 极简测试 UI ------------------------------


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>5idream 登录测试</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#222}
  h1{font-size:20px} button{margin:4px 6px 4px 0;padding:6px 12px;cursor:pointer}
  select,input{margin:4px 6px 4px 0;padding:5px}
  #qr img{width:220px;height:220px;border:1px solid #ddd;margin:10px 0}
  pre{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:12px;max-height:440px;overflow:auto;font-size:12px}
  .status{color:#555;font-size:13px} .hide{display:none}
  .bar{margin:6px 0}
</style>
</head>
<body>
<h1>到梦空间扫码登录 · FastAPI 测试页</h1>
<div>
  <button onclick="startLogin()">1. 生成二维码</button>
  <span id="qstatus" class="status"></span>
</div>
<div id="qr" class="hide"></div>
<div id="authed" class="hide">
  <div class="bar">
    <label>库类型
      <select id="scope" onchange="loadCategories()">
        <option value="activity">活动</option>
        <option value="tribe">部落</option>
        <option value="data">整理数据</option>
      </select>
    </label>
    <label>分类
      <select id="category"></select>
    </label>
    <label>每页
      <input id="pageSize" type="number" min="1" max="100" value="10" style="width:60px">
    </label>
    <button onclick="go(1)">查询</button>
    <button onclick="go(cur-1)">上一页</button>
    <button onclick="go(cur+1)">下一页</button>
    <button onclick="go(1,true)">刷新本页</button>
  </div>
  <div class="bar">
    <label>活动编号
      <input id="aid" type="text" placeholder="活动ID" style="width:120px">
    </label>
    <button onclick="loadDetail()">查活动详情</button>
    <button onclick="logout()">退出登录</button>
  </div>
  <div id="pginfo" class="status"></div>
</div>
<pre id="out">点击「生成二维码」，用到梦空间 APP 扫码确认后自动进入下一步。</pre>
<script>
let token = null, pollTimer = null, cur = 1, cats = {activity:[], tribe:[], data:[]};

async function api(url, opts) {
  const headers = {'Content-Type':'application/json'};
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const r = await fetch(url, Object.assign({headers}, opts||{}));
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch(e) { data = text; }
  if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status));
  return data;
}
function show(obj) { document.getElementById('out').textContent = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2); }

async function startLogin() {
  clearInterval(pollTimer);
  document.getElementById('authed').classList.add('hide');
  token = null;
  document.getElementById('out').textContent = '正在获取二维码...';
  try {
    const data = await api('/api/v1/login/qr', {method:'POST'});
    document.getElementById('qstatus').textContent = '等待扫码（' + data.expires_in + ' 秒内确认）...';
    const qr = document.getElementById('qr');
    qr.classList.remove('hide');
    qr.innerHTML = '<img src="' + data.qr_image_base64 + '" alt="二维码">';
    pollTimer = setInterval(async () => {
      try {
        const s = await api('/api/v1/login/status/' + data.session_id);
        if (s.status === 'success') {
          clearInterval(pollTimer);
          token = s.token;
          document.getElementById('qstatus').textContent = '登录成功：' + (s.user && s.user.name);
          document.getElementById('authed').classList.remove('hide');
          await loadCategories();
          show(s);
        } else if (s.status === 'expired' || s.status === 'failed') {
          clearInterval(pollTimer);
          document.getElementById('qstatus').textContent = s.error || s.status;
          show(s);
        }
      } catch(e) { document.getElementById('qstatus').textContent = '轮询出错：' + e.message; }
    }, 3000);
  } catch(e) { show('出错：' + e.message); }
}

async function loadCategories() {
  const scope = document.getElementById('scope').value;
  if (!cats[scope].length) {
    const url = scope === 'activity' ? '/api/v1/activity-categories'
              : scope === 'tribe'   ? '/api/v1/tribe-categories'
              : '/api/v1/data';   // data 不带 category 即返回目录
    const d = await api(url);
    cats[scope] = scope === 'data' ? d.activity_categories : d.categories;
  }
  const sel = document.getElementById('category');
  sel.innerHTML = cats[scope].map(c => '<option value="' + c.key + '">' + c.label + '</option>').join('');
  cur = 1;
}

function scopeUrl() {
  const scope = document.getElementById('scope').value;
  const category = document.getElementById('category').value;
  const pageSize = document.getElementById('pageSize').value || 10;
  if (scope === 'data') return '/api/v1/data?category=' + encodeURIComponent(category) + '&page=' + cur + '&page_size=' + pageSize;
  const base = scope === 'activity' ? '/api/v1/activities' : '/api/v1/tribes';
  return base + '?category=' + encodeURIComponent(category) + '&page=' + cur + '&page_size=' + pageSize;
}

async function go(page, force) {
  if (page < 1) return;
  cur = page;
  try {
    let url = scopeUrl();
    if (force) url += '&refresh=true';
    const d = await api(url);
    const info = document.getElementById('pginfo');
    info.textContent = d.label + '：第 ' + d.page + '/' + (d.total_pages || 1) + ' 页，共 ' + d.total + ' 条';
    show(d);
  } catch(e) { show('出错：' + e.message); }
}

async function loadDetail() {
  const id = document.getElementById('aid').value.trim();
  if (!id) { show('请先输入活动编号'); return; }
  try { show(await api('/api/v1/activities/' + encodeURIComponent(id))); }
  catch(e){ show('出错：' + e.message); }
}

async function logout() {
  try { await api('/api/v1/logout', {method:'POST'}); token = null; show('已退出登录'); }
  catch(e){ show('出错：' + e.message); }
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> str:
    return INDEX_HTML
