"""
Desihub scraper API.

FastAPI wrapper around the stateless HTML/RSC parser.

Endpoints
---------
GET /api
GET /api/health
GET /api/feed
GET /api/feed/{page}
GET /api/post/{slug}

There is intentionally no database/cache layer in this phase.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote, unquote

from fastapi import FastAPI, HTTPException, Request

from fetcher import fetch_page
from scraper.parser import (
    extract_page_number_from_url,
    normalize_slug,
    parse_page,
    parse_post_page,
    parser_debug_info,
)


APP_VERSION = "0.5.2"
BASE_URL = "https://desihub.sh"

app = FastAPI(
    title="Desihub Scraper API",
    version=APP_VERSION,
    description="Stateless Desihub feed/post scraper using rendered HTML and Next.js RSC data.",
)


def get_public_api_base(request: Request) -> str:
    """Return the public base URL of this API."""
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")

    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{forwarded_host}".rstrip("/")

    return str(request.base_url).rstrip("/")


def _build_api_feed_url(api_base: str, page: int) -> str:
    """Build this API's feed URL for a page number."""
    page = max(1, int(page))
    if page == 1:
        return f"{api_base}/api/feed"
    return f"{api_base}/api/feed/{page}"


def _upstream_page_number(url: Optional[str]) -> Optional[int]:
    """Return the page number represented by an upstream pagination URL."""
    if not url:
        return None

    page = extract_page_number_from_url(url)
    if page is None:
        cleaned = url.rstrip("/")
        if cleaned.endswith("/feed"):
            return 1
        return None

    return page


def _normalize_public_pagination(
    pagination: Dict[str, Optional[str]],
    api_base: str,
) -> Dict[str, Optional[str]]:
    """Convert upstream /feed/page/N links into API /api/feed/N links."""
    result: Dict[str, Optional[str]] = {"next": None, "previous": None}

    for key in ("next", "previous"):
        upstream = pagination.get(key)
        page = _upstream_page_number(upstream)
        if page is not None:
            result[key] = _build_api_feed_url(api_base, page)

    return result


def normalize_requested_slug(value: str) -> str:
    """Normalize a route slug without accepting an empty value."""
    value = unquote(value or "").strip()

    if "/feed/" in value:
        value = value.split("/feed/", 1)[1]

    value = value.strip("/")
    slug = normalize_slug(value)

    if not slug:
        raise HTTPException(status_code=400, detail="Invalid post slug.")

    return slug


async def scrape_feed(
    request: Request,
    upstream_url: str,
    page: int,
) -> Dict[str, Any]:
    """Fetch and parse one upstream feed page."""
    try:
        result = await fetch_page(upstream_url)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch upstream feed: {exc}",
        ) from exc

    html = result.get("html") or ""

    if not html:
        raise HTTPException(
            status_code=502,
            detail="Upstream feed returned an empty HTML response.",
        )

    parsed = parse_page(html, BASE_URL, current_page=page)

    api_base = get_public_api_base(request)
    parsed["pagination"] = _normalize_public_pagination(
        parsed.get("pagination") or {},
        api_base,
    )

    parsed["page"] = page
    parsed["source"] = upstream_url

    return parsed


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "name": "Desihub Scraper API",
        "version": APP_VERSION,
        "status": "ok",
        "endpoints": [
            "/api",
            "/api/health",
            "/api/feed",
            "/api/feed/{page}",
            "/api/post/{slug}",
        ],
    }


@app.get("/api")
async def api_info() -> Dict[str, Any]:
    return {
        "name": "Desihub Scraper API",
        "version": APP_VERSION,
        "status": "ok",
        "upstream": BASE_URL,
        "database": False,
        "cache": False,
        "parser": "html + Next.js RSC/Flight",
    }


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/feed")
async def feed_page_one(request: Request) -> Dict[str, Any]:
    """Scrape upstream /feed."""
    return await scrape_feed(request, f"{BASE_URL}/feed", 1)


@app.get("/api/feed/{page}")
async def feed_page(request: Request, page: int) -> Dict[str, Any]:
    """Scrape upstream /feed/page/N."""
    if page < 1:
        raise HTTPException(status_code=400, detail="Page must be 1 or greater.")

    return await scrape_feed(request, f"{BASE_URL}/feed/page/{page}", page)


@app.get("/api/post/{slug:path}")
async def post_page(request: Request, slug: str) -> Dict[str, Any]:
    """Scrape one individual upstream /feed/<slug> page directly."""
    requested_slug = normalize_requested_slug(slug)
    encoded_slug = quote(requested_slug, safe="-")
    target_url = f"{BASE_URL}/feed/{encoded_slug}"

    try:
        result = await fetch_page(target_url)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch upstream post: {exc}",
        ) from exc

    html = result.get("html") or ""

    if not html:
        raise HTTPException(
            status_code=502,
            detail="Upstream post returned an empty HTML response.",
        )

    parsed = parse_post_page(html, requested_slug, BASE_URL)

    if parsed is None:
        debug = parser_debug_info(html)
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Post not found.",
                "requested_slug": requested_slug,
                "upstream_status": result.get("status"),
                "upstream_url": result.get("url") or target_url,
                "html_length": result.get("html_length", len(html)),
                "rsc_feed_count": debug.get("feed_object_count", 0),
            },
        )

    return {
        **parsed,
        "source": target_url,
        "requested_slug": requested_slug,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
