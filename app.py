"""
hermes-browser-worker — FastAPI + Playwright service for Railway.
Endpoints:
    GET  /health
    POST /browse
    POST /search
    POST /extract
    POST /screenshot
    POST /click
    POST /fill
    POST /cookies/save
    POST /cookies/load
"""

import json
import os
import re
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import TimeoutError as PlaywrightTimeout

from browser import BrowserManager

app = FastAPI(
    title="Hermes Browser Worker",
    version="0.1.0",
    description="Playwright browser automation as a service for Hermes CLI",
)

_manager = BrowserManager()


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
    engine: str = "google"
    session_id: str = Field(default_factory=lambda: os.urandom(8).hex())
    page_id: str = "main"

class ExtractReq(BaseModel):
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
    state = "ready" if _manager._browser else "starting"
    return {"status": state, "timestamp": time.time()}


# ──────────────────────────────────────────────
#   Core endpoints
# ──────────────────────────────────────────────

@app.post("/browse")
async def browse(req: BrowseReq):
    t0 = time.time()
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


@app.post("/search")
async def search(req: SearchReq):
    engines = {
        "google": "https://www.google.com/search?q={q}",
        "duckduckgo": "https://duckduckgo.com/?q={q}&ia=web",
        "bing": "https://www.bing.com/search?q={q}",
    }
    tmpl = engines.get(req.engine, engines["google"])
    url = tmpl.format(q=req.query.replace(" ", "+"))
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


@app.post("/extract")
async def extract(req: ExtractReq):
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


@app.post("/screenshot")
async def screenshot(req: ScreenshotReq):
    t0 = time.time()
    page = await _manager.get_or_create_page(req.session_id, req.page_id)

    if req.url and page.url != req.url:
        try:
            await page.goto(req.url, wait_until="domcontentloaded", timeout=req.timeout_ms)
        except PlaywrightTimeout:
            raise HTTPException(status_code=408, detail="Load timed out for screenshot")

    out_path = f"/tmp/screenshot_{req.session_id}_{req.page_id}.png"
    try:
        if req.selector:
            el = await page.wait_for_selector(req.selector, timeout=req.timeout_ms)
            if el is None:
                raise HTTPException(status_code=404, detail="Selector not found for screenshot")
            await el.screenshot(path=out_path, full_page=req.full_page)
        else:
            await page.screenshot(path=out_path, full_page=req.full_page)
    except PlaywrightTimeout:
        raise HTTPException(status_code=408, detail="Selector wait timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Screenshot failed: {e}")

    await _manager.save_session(req.session_id)
    return {
        "ok": True,
        "session_id": req.session_id,
        "path": out_path,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


@app.post("/click")
async def click(req: ClickReq):
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


@app.post("/fill")
async def fill(req: FillReq):
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


# ──────────────────────────────────────────────
#   Cookie / session persistence
# ──────────────────────────────────────────────

@app.post("/cookies/save")
async def cookies_save(session_id: str):
    await _manager.save_session(session_id)
    from pathlib import Path
    p = Path("/app/sessions") / f"{session_id}.json"
    exists = p.exists()
    size = p.stat().st_size if exists else 0
    return {"ok": True, "session_id": session_id, "saved": exists, "bytes": size}


@app.post("/cookies/load")
async def cookies_load(session_id: str):
    from pathlib import Path
    p = Path("/app/sessions") / f"{session_id}.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True, "session_id": session_id, "path": str(p)}
