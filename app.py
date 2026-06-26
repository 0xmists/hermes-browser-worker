"""
hermes-browser-worker — FastAPI + Playwright service for Railway.

Endpoints:
    GET  /health
    POST /browse
    POST /search
    POST /extract
    POST /markdown
    POST /screenshot
    POST /click
    POST /fill
    POST /cookies/save
    POST /cookies/load
    POST /close
    POST /raw
    POST /login/start
    GET  /login/status/{session_id}
    POST /login/authenticate
    POST /login/cancel
    POST /session/list
    POST /session/delete
    POST /session/refresh
"""

from __future__ import annotations

import json
import os
import re
import time
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from playwright.async_api import TimeoutError as PlaywrightTimeout

from browser import BrowserManager, SESSIONS_DIR, PROFILES_DIR
from login_providers import LoginProvider, StubLoginProvider
from providers.playwright_ws_provider import PlaywrightWSProvider

port = int(os.getenv("PORT", 8080))
API_KEY = os.getenv("API_KEY", "")
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "5"))

app = FastAPI(
    title="Hermes Browser Worker",
    version="0.2.0",
    description="Playwright browser automation as a service for Hermes CLI",
)

_logger = logging.getLogger("browser-worker")

_manager = BrowserManager(max_sessions=MAX_SESSIONS)
_ws_provider = PlaywrightWSProvider(_manager._profiles, PROFILES_DIR)
_manager._logins._provider = _ws_provider


# ──────────────────────────────────────────────
#   Security: API key middleware
# ──────────────────────────────────────────────

@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    # Allow health checks and docs without credentials
    if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc"):
        return await call_next(request)
    if not API_KEY:
        return await call_next(request)
    provided = request.headers.get("x-api-key", "")
    if provided != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return await call_next(request)


# ──────────────────────────────────────────────
#   Logging middleware
# ──────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    elapsed = time.time() - t0
    session_id = ""
    if response.status_code < 500 and hasattr(response, "body"):
        try:
            body = response.body
            if isinstance(body, (list, tuple)):
                body = next(iter(body), b"")
            data = json.loads(body or b"{}")
            session_id = str(data.get("session_id", ""))
        except Exception:
            pass
    _logger.info(
        "%-6s %-20s session=%-12s status=%d time=%.2fs",
        request.method,
        request.url.path,
        session_id or "-",
        response.status_code,
        elapsed,
    )
    return response


# ──────────────────────────────────────────────
#   Models
# ──────────────────────────────────────────────

class BrowseReq(BaseModel):
    url: str
    session_id: str = Field(default_factory=lambda: os.urandom(8).hex())
    page_id: str = "main"
    wait_until: str = "domcontentloaded"
    timeout_ms: int = 30000
    cookies: Optional[list[dict]] = None

class SearchReq(BaseModel):
    query: str
    engine: str = "duckduckgo"
    session_id: str = Field(default_factory=lambda: os.urandom(8).hex())
    page_id: str = "main"

class ExtractReq(BaseModel):
    url: str
    session_id: str = Field(default_factory=lambda: os.urandom(8).hex())
    page_id: str = "main"
    selector: Optional[str] = None
    timeout_ms: int = 30000

class MarkdownReq(BaseModel):
    url: str
    session_id: str = Field(default_factory=lambda: os.urandom(8).hex())
    page_id: str = "main"
    selector: Optional[str] = None
    timeout_ms: int = 30000

class ScreenshotReq(BaseModel):
    url: Optional[str] = None
    session_id: str = Field(default_factory=lambda: os.urandom(8).hex())
    page_id: str = "main"
    full_page: bool = False
    selector: Optional[str] = None
    timeout_ms: int = 30000

class ClickReq(BaseModel):
    session_id: str
    selector: str
    page_id: str = "main"
    timeout_ms: int = 10000
    wait_after_ms: int = 500

class FillReq(BaseModel):
    session_id: str
    selector: str
    value: str
    page_id: str = "main"
    timeout_ms: int = 10000
    clear_first: bool = True
    wait_after_ms: int = 300


class LoginStartReq(BaseModel):
    site: str
    expires_in: int = 600


class LoginStartResp(BaseModel):
    session_id: str
    login_url: str
    connect_url: Optional[str] = None
    expires_in: int
    state: str = "waiting_user"
    authenticated: bool = False


class LoginStatusResp(BaseModel):
    session_id: str
    state: str
    authenticated: bool = False
    login_url: Optional[str] = None
    connect_url: Optional[str] = None


# ──────────────────────────────────────────────
#   Lifecycle
# ──────────────────────────────────────────────

@app.on_event("startup")
async def _startup():
    await _manager.start()

@app.on_event("shutdown")
async def _shutdown():
    await _manager.stop()

@app.get("/health")
async def health():
    state = "ready" if _manager.is_ready else "starting"
    return {"status": state, "timestamp": time.time()}


# ──────────────────────────────────────────────
#   Core endpoints
# ──────────────────────────────────────────────

@app.post("/browse")
async def browse(req: BrowseReq):
    t0 = time.time()
    try:
        page = await _manager.get_or_create_page(req.session_id, req.page_id)

        if req.cookies:
            try:
                await page.context.add_cookies(req.cookies)
            except Exception:
                pass

        try:
            await page.goto(req.url, wait_until=req.wait_until, timeout=req.timeout_ms)
        except PlaywrightTimeout:
            raise HTTPException(status_code=408, detail=f"Page load timed out after {req.timeout_ms}ms")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

        title = await page.title()
        url_final = page.url
        await _manager.save_session(req.session_id)
        return {
            "ok": True,
            "session_id": req.session_id,
            "page_id": req.page_id,
            "url": url_final,
            "title": title,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        _logger.error("browse handler error: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search")
async def search(req: SearchReq):
    try:
        engines = {
            "duckduckgo": "https://duckduckgo.com/?q={q}&ia=web",
            "bing": "https://www.bing.com/search?q={q}",
            "google": "https://www.google.com/search?q={q}",
        }
        if req.engine not in engines:
            raise HTTPException(status_code=400, detail=f"Unsupported engine: {req.engine}. Use: {', '.join(engines)}")
        url = engines[req.engine].format(q=req.query.replace(" ", "+"))
        page = await _manager.get_or_create_page(req.session_id, req.page_id)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        except PlaywrightTimeout:
            raise HTTPException(status_code=408, detail="Search timed out")

        links = await page.eval_on_selector_all(
            "a[href]",
            """els => Array.from(els)
                .filter(e => e.href && !e.href.startsWith('javascript') && e.innerText.trim())
                .slice(0, 20)
                .map(e => ({title: e.innerText.trim(), url: e.href}))""",
        )
        await _manager.save_session(req.session_id)
        return {
            "ok": True,
            "session_id": req.session_id,
            "query": req.query,
            "engine": req.engine,
            "results": links,
            "count": len(links),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        _logger.error("search handler error: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/extract")
async def extract(req: ExtractReq):
    try:
        t0 = time.time()
        page = await _manager.get_or_create_page(req.session_id, req.page_id)

        try:
            await page.goto(req.url, wait_until="domcontentloaded", timeout=req.timeout_ms)
        except PlaywrightTimeout:
            raise HTTPException(status_code=408, detail="Load timed out")

        if req.selector:
            try:
                el = await page.wait_for_selector(req.selector, timeout=req.timeout_ms)
                if el is None:
                    raise HTTPException(status_code=404, detail=f"Selector not found: {req.selector}")
                text = await el.inner_text()
            except PlaywrightTimeout:
                raise HTTPException(status_code=408, detail="Selector wait timed out")
        else:
            body = await page.query_selector("body")
            text = await body.inner_text() if body else ""

        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        await _manager.save_session(req.session_id)
        return {
            "ok": True,
            "session_id": req.session_id,
            "url": page.url,
            "selector": req.selector,
            "text": text,
            "chars": len(text),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        _logger.error("extract handler error: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/markdown")
async def markdown(req: MarkdownReq):
    try:
        t0 = time.time()
        page = await _manager.get_or_create_page(req.session_id, req.page_id)

        try:
            await page.goto(req.url, wait_until="domcontentloaded", timeout=req.timeout_ms)
        except PlaywrightTimeout:
            raise HTTPException(status_code=408, detail="Load timed out")

        if req.selector:
            try:
                el = await page.wait_for_selector(req.selector, timeout=req.timeout_ms)
                if el is None:
                    raise HTTPException(status_code=404, detail=f"Selector not found: {req.selector}")
                html = await el.inner_html()
            except PlaywrightTimeout:
                raise HTTPException(status_code=408, detail="Selector wait timed out")
        else:
            html = await page.content()

        from markdownify import markdownify as md
        text = md(html, heading_style="ATX")

        await _manager.save_session(req.session_id)
        return {
            "ok": True,
            "session_id": req.session_id,
            "url": page.url,
            "markdown": text,
            "chars": len(text),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        _logger.error("markdown handler error: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/screenshot")
async def screenshot(req: ScreenshotReq):
    try:
        t0 = time.time()
        page = await _manager.get_or_create_page(req.session_id, req.page_id)

        if req.url and page.url != req.url:
            try:
                await page.goto(req.url, wait_until="domcontentloaded", timeout=req.timeout_ms)
            except PlaywrightTimeout:
                raise HTTPException(status_code=408, detail="Load timed out for screenshot")

        try:
            if req.selector:
                el = await page.wait_for_selector(req.selector, timeout=req.timeout_ms)
                if el is None:
                    raise HTTPException(status_code=404, detail="Selector not found for screenshot")
                img_bytes = await el.screenshot(full_page=req.full_page)
            else:
                img_bytes = await page.screenshot(full_page=req.full_page)
        except PlaywrightTimeout:
            raise HTTPException(status_code=408, detail="Selector wait timed out")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Screenshot failed: {e}")

        import base64
        img_b64 = base64.b64encode(img_bytes).decode("ascii")

        await _manager.save_session(req.session_id)
        return {
            "ok": True,
            "session_id": req.session_id,
            "content_type": "image/png",
            "image_base64": img_b64,
            "chars": len(img_b64),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        _logger.error("screenshot handler error: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/click")
async def click(req: ClickReq):
    try:
        t0 = time.time()
        page = await _manager.get_or_create_page(req.session_id, req.page_id)

        try:
            el = await page.wait_for_selector(req.selector, state="visible", timeout=req.timeout_ms)
            if el is None:
                raise HTTPException(status_code=404, detail="Element not found")
            await el.scroll_into_view_if_needed()
            await el.click(timeout=req.timeout_ms)
        except PlaywrightTimeout:
            raise HTTPException(status_code=408, detail="Click wait timed out")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        if req.wait_after_ms:
            await page.wait_for_timeout(req.wait_after_ms)

        await _manager.save_session(req.session_id)
        return {
            "ok": True,
            "session_id": req.session_id,
            "selector": req.selector,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        _logger.error("click handler error: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/fill")
async def fill(req: FillReq):
    try:
        t0 = time.time()
        page = await _manager.get_or_create_page(req.session_id, req.page_id)

        try:
            el = await page.wait_for_selector(req.selector, state="visible", timeout=req.timeout_ms)
            if el is None:
                raise HTTPException(status_code=404, detail="Element not found")
            if req.clear_first:
                await el.fill("")
            await el.fill(req.value)
        except PlaywrightTimeout:
            raise HTTPException(status_code=408, detail="Fill wait timed out")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        if req.wait_after_ms:
            await page.wait_for_timeout(req.wait_after_ms)

        await _manager.save_session(req.session_id)
        return {
            "ok": True,
            "session_id": req.session_id,
            "selector": req.selector,
            "value": req.value,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        _logger.error("fill handler error: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────────
#   Cookie / session persistence
# ──────────────────────────────────────────────

@app.post("/cookies/save")
async def cookies_save(request: Request):
    body = await request.body()
    data = json.loads(body or b'{}')
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    await _manager.save_session(session_id)
    p = SESSIONS_DIR / f"{session_id}.json"
    exists = p.exists()
    size = p.stat().st_size if exists else 0
    return {"ok": True, "session_id": session_id, "saved": exists, "bytes": size}


@app.post("/cookies/load")
async def cookies_load(request: Request):
    body = await request.body()
    data = json.loads(body or b'{}')
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    p = SESSIONS_DIR / f"{session_id}.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True, "session_id": session_id, "path": str(p)}


@app.post("/close")
async def close_session(req: Request):
    body = await req.body()
    data = json.loads(body or b"{}")
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    await _manager.close_session(session_id)
    return {"ok": True, "session_id": session_id, "status": "closed"}


@app.post("/raw")
async def raw(req: Request):
    body = await req.body()
    data = json.loads(body or b"{}")
    session_id = data.get("session_id")
    url = data.get("url")
    selector = data.get("selector")
    timeout_ms = int(data.get("timeout_ms", 30000))
    if not session_id or not url:
        raise HTTPException(status_code=400, detail="session_id and url required")

    t0 = time.time()
    page = await _manager.get_or_create_page(session_id)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeout:
        raise HTTPException(status_code=408, detail="Load timed out")

    if selector:
        try:
            el = await page.wait_for_selector(selector, timeout=timeout_ms)
            if el is None:
                raise HTTPException(status_code=404, detail=f"Selector not found: {selector}")
            html = await el.inner_html()
        except PlaywrightTimeout:
            raise HTTPException(status_code=408, detail="Selector wait timed out")
    else:
        html = await page.content()

    await _manager.save_session(session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "url": page.url,
        "html": html,
        "chars": len(html),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


# ──────────────────────────────────────────────
#   Login flow (Phase 1)
# ──────────────────────────────────────────────

@app.post("/login/start", response_model=LoginStartResp)
async def login_start(req: LoginStartReq):
    session_id = req.site.replace(".", "_").replace("/", "_")
    try:
        result = await _manager.start_login(session_id, req.site)
        resp = LoginStartResp(**result)
        if resp.connect_url and resp.connect_url.startswith("/ws"):
            host = req.headers.get("host", "")
            scheme = "wss" if req.url.scheme == "https" else "ws"
            resp.connect_url = f"{scheme}://{host}{resp.connect_url}"
        return resp
    except Exception as e:
        import traceback
        _logger.error("login/start error: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/login/status/{session_id}", response_model=LoginStatusResp)
async def _login_status(session_id: str):
    session_data = await _manager.login_status(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Login session not found")
    resp = LoginStatusResp(**session_data)
    if resp.connect_url and resp.connect_url.startswith("/ws"):
        # Rebuild connect_url on status check (token may have been regenerated)
        pass
    return resp


@app.post("/login/authenticate")
async def login_authenticate(request: Request):
    """Manually mark a session as authenticated (for ManualDetector flow)."""
    body = await request.body()
    data = json.loads(body or b"{}")
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    result = _manager.force_authenticate(session_id)
    if not result:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True, "session_id": session_id, "authenticated": True}


@app.post("/login/cancel")
async def login_cancel(req: Request):
    body = await req.body()
    data = json.loads(body or b"{}")
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    result = await _manager.cancel_login(session_id)
    if not result:
        raise HTTPException(status_code=404, detail="Login session not found")
    return {"ok": True, "session_id": session_id, "cancelled": True}


@app.post("/session/list")
async def session_list(req: Request):
    body = await req.body()
    data = json.loads(body or b"{}")
    sessions = _manager.list_sessions()
    return {"ok": True, "sessions": sessions, **data}


@app.post("/session/delete")
async def session_delete(req: Request):
    body = await req.body()
    data = json.loads(body or b"{}")
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    result = await _manager.delete_session(session_id)
    if not result:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True, "session_id": session_id, "deleted": True}


@app.post("/session/refresh")
async def session_refresh(req: Request):
    body = await req.body()
    data = json.loads(body or b"{}")
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    try:
        result = await _manager.refresh_session(session_id)
    except RuntimeError:
        raise HTTPException(status_code=404, detail="Profile not found")
    return result
