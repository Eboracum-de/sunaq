from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from playwright.async_api import Browser, Error as PlaywrightError, Playwright, Request, Route, async_playwright

log = logging.getLogger("aki-playwright-renderer")

PLAYWRIGHT: Playwright | None = None
BROWSER: Browser | None = None
BROWSER_LAUNCH_ERROR = ""

NAV_TIMEOUT_MS = int(os.getenv("RENDER_NAV_TIMEOUT_MS", "30000"))
POSTLOAD_WAIT_MS = int(os.getenv("RENDER_POSTLOAD_WAIT_MS", "750"))
MAX_PDF_BYTES = int(os.getenv("RENDER_MAX_PDF_BYTES", str(50 * 1024 * 1024)))
MAX_CONCURRENCY = max(1, int(os.getenv("RENDER_MAX_CONCURRENCY", "1")))
RENDER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENCY)
STATE_DIR = Path(os.getenv("RENDER_STATE_DIR", "/state"))
ALLOWED_PORTS = {
    int(p.strip())
    for p in os.getenv("RENDER_ALLOWED_PORTS", "80,443").split(",")
    if p.strip()
}


class RenderRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    html: str = Field(min_length=1, max_length=20_000_000)
    print_background: bool = True
    prefer_css_page_size: bool = False
    landscape: bool = True
    viewport_width: int = Field(default=1440, ge=800, le=3840)
    viewport_height: int = Field(default=900, ge=600, le=2160)
    persist_state: bool = True
    cleanup_cookie_consent: str = "off"
    cleanup_dismiss_overlays: bool = False
    cleanup_remove_overlays: bool = False


def _state_path(url: str) -> Path:
    hostname = (urlsplit(url).hostname or "unknown").casefold()
    key = hashlib.sha256(hostname.encode("utf-8")).hexdigest()
    return STATE_DIR / f"{key}.json"


def _load_storage_state_path(url: str) -> str | None:
    path = _state_path(url)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("cookies", []), list):
            return None
    except Exception:
        return None
    return str(path)


async def _persist_storage_state(context, url: str) -> bool:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state = await context.storage_state()
        target = _state_path(url)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        return True
    except Exception:
        return False


def _merge_cleanup(first: dict[str, object], second: dict[str, object]) -> dict[str, object]:
    cookie_actions = int(first.get("cookie_actions") or 0) + int(second.get("cookie_actions") or 0)
    overlays_dismissed = int(first.get("overlays_dismissed") or 0) + int(second.get("overlays_dismissed") or 0)
    overlays_removed = int(first.get("overlays_removed") or 0) + int(second.get("overlays_removed") or 0)
    return {
        "cookie_consent": "accepted" if cookie_actions else "none",
        "cookie_actions": cookie_actions,
        "overlays_dismissed": overlays_dismissed,
        "overlays_removed": overlays_removed,
        "dom_modified": bool(first.get("dom_modified") or second.get("dom_modified")),
    }


def _is_public_ip(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_url_shape(url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid URL") from exc

    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Only http/https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(status_code=400, detail="Userinfo in URLs is not allowed")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="URL has no hostname")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid URL port") from exc

    if port not in ALLOWED_PORTS:
        raise HTTPException(status_code=403, detail=f"Port {port} is not allowed")

    return parsed.hostname, port


async def _resolve_public(hostname: str, port: int) -> list[str]:
    # Literal IPs are accepted only when globally routable.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        if not _is_public_ip(str(literal)):
            raise HTTPException(status_code=403, detail="Private/non-public target blocked")
        return [str(literal)]

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise HTTPException(status_code=502, detail="DNS resolution failed") from exc

    addrs = sorted({info[4][0] for info in infos})
    if not addrs:
        raise HTTPException(status_code=502, detail="DNS resolution returned no addresses")
    if any(not _is_public_ip(addr) for addr in addrs):
        raise HTTPException(status_code=403, detail="Private/non-public DNS target blocked")
    return addrs


async def _validate_target(url: str) -> None:
    hostname, port = _validate_url_shape(url)
    await _resolve_public(hostname, port)


async def _route_guard(route: Route, request: Request) -> None:
    """Network-free snapshot renderer.

    The HTML body has already been fetched by SunaQ's IP-pinned WebFetcher.
    Chromium is therefore never allowed to perform a second HTTP(S), WebSocket,
    frame or subresource fetch.
    """
    scheme = urlsplit(request.url).scheme
    if scheme in {"data", "blob", "about"}:
        await route.continue_()
        return
    await route.abort("blockedbyclient")


async def _websocket_guard(route) -> None:
    try:
        await route.close()
    except Exception:
        log.debug("WebSocket close failed", exc_info=True)


COOKIE_ACCEPT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "[data-testid='notice-agree-button']",
    "button[mode='primary'][data-gdpr-single-choice-accept]",
)
COOKIE_ACCEPT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^alle(?:\s+cookies)?\s+akzeptieren$",
        r"^alle\s+zulassen$",
        r"^accept\s+all(?:\s+cookies)?$",
        r"^allow\s+all(?:\s+cookies)?$",
        r"^i\s+agree$",
    )
)
DISMISS_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^schlie(?:ß|ss)en$",
        r"^nicht\s+jetzt$",
        r"^nein,?\s+danke$",
        r"^close$",
        r"^not\s+now$",
        r"^no,?\s+thanks$",
        r"^dismiss$",
    )
)


async def _is_overlay_control(locator) -> bool:
    try:
        return bool(
            await locator.evaluate(
                """
                (el) => {
                  let cur = el;
                  while (cur && cur !== document.documentElement) {
                    const style = getComputedStyle(cur);
                    const marker = `${cur.id || ''} ${cur.className || ''} ${cur.getAttribute?.('aria-label') || ''}`.toLowerCase();
                    if (cur.getAttribute?.('role') === 'dialog' || cur.getAttribute?.('aria-modal') === 'true') return true;
                    if (style.position === 'fixed' || style.position === 'sticky') return true;
                    if (/cookie|consent|cmp|modal|overlay|popup|dialog|interstitial/.test(marker)) return true;
                    cur = cur.parentElement;
                  }
                  return false;
                }
                """
            )
        )
    except Exception:
        return False


async def _click_known_cookie_control(frame) -> bool:
    for selector in COOKIE_ACCEPT_SELECTORS:
        try:
            loc = frame.locator(selector).first
            if await loc.is_visible(timeout=250):
                await loc.click(timeout=1200)
                return True
        except Exception:
            log.debug("Cookie-control candidate failed: %s", selector, exc_info=True)
            continue
    return False


async def _click_text_control(frame, patterns, *, overlay_only: bool = True) -> bool:
    try:
        candidates = frame.locator(
            "button, [role='button'], input[type='button'], input[type='submit'], a"
        )
        count = min(await candidates.count(), 120)
    except Exception:
        return False

    for index in range(count):
        loc = candidates.nth(index)
        try:
            if not await loc.is_visible(timeout=100):
                continue
            text = " ".join(
                part.strip()
                for part in (
                    await loc.inner_text(timeout=200),
                    await loc.get_attribute("value"),
                    await loc.get_attribute("aria-label"),
                    await loc.get_attribute("title"),
                )
                if part and part.strip()
            )
            if not text or not any(pattern.fullmatch(text.strip()) for pattern in patterns):
                continue
            if overlay_only and not await _is_overlay_control(loc):
                continue
            await loc.click(timeout=1200)
            return True
        except Exception:
            log.debug("Text-control candidate failed at index %d", index, exc_info=True)
            continue
    return False


async def _remove_obvious_overlays(frame) -> int:
    try:
        return int(
            await frame.evaluate(
                """
                () => {
                  let removed = 0;
                  const nodes = Array.from(document.querySelectorAll(
                    "[role='dialog'], [aria-modal='true'], .modal, .overlay, .popup, [class*='cookie'], [id*='cookie'], [class*='consent'], [id*='consent']"
                  ));
                  for (const el of nodes) {
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const marker = `${el.id || ''} ${el.className || ''}`.toLowerCase();
                    const modal = el.getAttribute('role') === 'dialog' || el.getAttribute('aria-modal') === 'true';
                    const overlayish = style.position === 'fixed' || modal || /cookie|consent|modal|overlay|popup/.test(marker);
                    if (!overlayish || rect.width < 40 || rect.height < 20) continue;
                    el.remove();
                    removed += 1;
                  }
                  if (removed) {
                    document.documentElement.style.overflow = 'auto';
                    document.body && (document.body.style.overflow = 'auto');
                  }
                  return removed;
                }
                """
            )
        )
    except Exception:
        return 0


async def _cleanup_page(page, req: RenderRequest) -> dict[str, object]:
    cookie_actions = 0
    overlays_dismissed = 0
    overlays_removed = 0

    if req.cleanup_cookie_consent.strip().lower() == "accept_all":
        for frame in list(page.frames):
            if await _click_known_cookie_control(frame):
                cookie_actions += 1
                break
        if cookie_actions == 0:
            for frame in list(page.frames):
                if await _click_text_control(frame, COOKIE_ACCEPT_PATTERNS, overlay_only=True):
                    cookie_actions += 1
                    break
        if cookie_actions:
            await page.wait_for_timeout(300)

    if req.cleanup_dismiss_overlays:
        # A few sites stack more than one harmless prompt. Keep this bounded.
        for _ in range(3):
            clicked = False
            for frame in list(page.frames):
                if await _click_text_control(frame, DISMISS_PATTERNS, overlay_only=True):
                    overlays_dismissed += 1
                    clicked = True
                    await page.wait_for_timeout(200)
                    break
            if not clicked:
                break

    if req.cleanup_remove_overlays:
        for frame in list(page.frames):
            overlays_removed += await _remove_obvious_overlays(frame)
        if overlays_removed:
            await page.wait_for_timeout(150)

    return {
        "cookie_consent": "accepted" if cookie_actions else "none",
        "cookie_actions": cookie_actions,
        "overlays_dismissed": overlays_dismissed,
        "overlays_removed": overlays_removed,
        "dom_modified": bool(overlays_removed),
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    global PLAYWRIGHT, BROWSER, BROWSER_LAUNCH_ERROR
    BROWSER_LAUNCH_ERROR = ""
    try:
        PLAYWRIGHT = await async_playwright().start()
        BROWSER = await PLAYWRIGHT.chromium.launch(headless=True)
    except Exception as exc:
        # Rendering is archival enrichment, not a prerequisite for Web search or
        # the synchronous text/metadata archive. Keep the service alive in a
        # diagnosable degraded state instead of entering a container restart loop.
        BROWSER = None
        BROWSER_LAUNCH_ERROR = f"{type(exc).__name__}: {exc}"[:1200]
        log.exception("Chromium launch failed; renderer stays up in degraded mode")
    try:
        yield
    finally:
        if BROWSER is not None:
            await BROWSER.close()
        if PLAYWRIGHT is not None:
            await PLAYWRIGHT.stop()
        BROWSER = None
        PLAYWRIGHT = None


app = FastAPI(title="RAG Playwright Renderer", version="0.2.3", lifespan=lifespan)


@app.get("/live")
async def live() -> dict[str, object]:
    return {
        "ok": BROWSER is not None,
        "service": "rag-playwright-renderer",
        "version": "0.2.3",
        "launch_error": BROWSER_LAUNCH_ERROR,
    }


@app.post("/render", response_class=Response)
async def render(req: RenderRequest) -> Response:
    if BROWSER is None:
        raise HTTPException(status_code=503, detail="Browser unavailable")

    async with RENDER_SEMAPHORE:
        return await _render_one(req)


async def _render_one(req: RenderRequest) -> Response:
    # The source URL is provenance only. No browser navigation to it occurs.
    _validate_url_shape(req.url)
    started = time.monotonic()

    context = await BROWSER.new_context(
        accept_downloads=False,
        java_script_enabled=False,
        service_workers="block",
        viewport={"width": req.viewport_width, "height": req.viewport_height},
    )
    page = await context.new_page()
    await context.route("**/*", _route_guard)
    if hasattr(context, "route_web_socket"):
        await context.route_web_socket("**/*", _websocket_guard)

    try:
        try:
            await page.set_content(
                req.html,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_MS,
            )
        except PlaywrightError as exc:
            message = re.sub(r"\s+", " ", str(exc or "Playwright snapshot render failed")).strip()
            raise HTTPException(status_code=502, detail=f"Snapshot render failed: {message[:700]}") from exc

        final_url = req.url
        if POSTLOAD_WAIT_MS > 0:
            await page.wait_for_timeout(POSTLOAD_WAIT_MS)

        cleanup = await _cleanup_page(page, req)
        await page.wait_for_timeout(200)
        cleanup = _merge_cleanup(cleanup, await _cleanup_page(page, req))
        state_persisted = False
        title = (await page.title()).strip()

        await page.emulate_media(media="screen")

        # Network is disabled, so only already embedded data/blob poster images
        # can survive as video preview replacements.
        poster_count = await page.evaluate(
            """
            async () => {
              const replacements = [];
              for (const video of document.querySelectorAll('video')) {
                const poster = video.poster || video.getAttribute('poster');
                if (!poster || !/^(?:data:|blob:)/i.test(poster)) continue;
                const rect = video.getBoundingClientRect();
                const computed = getComputedStyle(video);
                const img = document.createElement('img');
                img.src = poster;
                img.alt = video.getAttribute('aria-label') || video.getAttribute('title') || 'Video preview';
                img.className = video.className;
                img.style.cssText = video.style.cssText;
                img.style.objectFit = computed.objectFit || 'cover';
                img.style.objectPosition = computed.objectPosition || '50% 50%';
                img.style.display = computed.display === 'inline' ? 'inline-block' : computed.display;
                if (rect.width > 0) img.style.width = `${rect.width}px`;
                if (rect.height > 0) img.style.height = `${rect.height}px`;
                video.replaceWith(img);
                replacements.push(img);
              }
              return replacements.length;
            }
            """
        )

        pdf = await page.pdf(
            format="A4",
            print_background=req.print_background,
            prefer_css_page_size=req.prefer_css_page_size,
            landscape=req.landscape,
            display_header_footer=False,
        )
        if len(pdf) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="Rendered PDF exceeds configured size limit")

        sha256 = hashlib.sha256(pdf).hexdigest()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        headers = {
            "X-Render-Requested-URL": quote(req.url, safe=""),
            "X-Render-Final-URL": quote(final_url, safe=""),
            "X-Render-Title": quote(title[:1024], safe=""),
            "X-Render-SHA256": sha256,
            "X-Render-Elapsed-MS": str(elapsed_ms),
            "X-Render-Media": "screen",
            "X-Render-Video-Posters": str(poster_count),
            "X-Render-Cookie-Consent": str(cleanup["cookie_consent"]),
            "X-Render-Cookie-Actions": str(cleanup["cookie_actions"]),
            "X-Render-Overlays-Dismissed": str(cleanup["overlays_dismissed"]),
            "X-Render-Overlays-Removed": str(cleanup["overlays_removed"]),
            "X-Render-DOM-Modified": "true" if cleanup["dom_modified"] else "false",
            "X-Render-Landscape": "true" if req.landscape else "false",
            "X-Render-Viewport": f"{req.viewport_width}x{req.viewport_height}",
            "X-Render-State-Reused": "false",
            "X-Render-State-Persisted": "true" if state_persisted else "false",
            "Cache-Control": "no-store",
        }
        return Response(content=pdf, media_type="application/pdf", headers=headers)
    finally:
        await context.close()
