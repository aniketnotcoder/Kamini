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
GET /api/search?q={query}
GET /api/search?q={query}&page={page}
GET /api/tag/{tag}
GET /api/tag/{tag}/{page}
GET /api/channel/{username}
GET /api/channel/{username}/{page}

There is intentionally no database/cache layer in this phase.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from urllib.parse import quote, unquote, urlparse
import re

from fastapi import FastAPI, HTTPException, Request

from fetcher import fetch_page
from scraper.parser import (
    extract_page_number_from_url,
    extract_generic_page_number_from_url,
    normalize_slug,
    parse_page,
    parse_listing_page,
    parse_post_page,
    parser_debug_info,
)


APP_VERSION = "0.8.1"
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


async def scrape_listing(
    request: Request,
    upstream_url: str,
    page: int = 1,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fetch and parse a generic Desihub listing page."""
    try:
        result = await fetch_page(upstream_url)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch upstream listing: {exc}",
        ) from exc

    html = result.get("html") or ""
    if not html:
        raise HTTPException(
            status_code=502,
            detail="Upstream listing returned an empty HTML response.",
        )

    parsed = parse_listing_page(html, upstream_url, current_page=page)

    api_base = get_public_api_base(request)
    parsed["pagination"] = _normalize_discovery_pagination(
        parsed.get("pagination") or {},
        api_base,
        route_kind=(extra or {}).get("route_kind"),
        route_value=(extra or {}).get("route_value"),
        query=(extra or {}).get("query"),
    )

    parsed["page"] = page
    parsed["source"] = upstream_url

    if extra:
        for key, value in extra.items():
            if key != "route_kind" and key != "route_value" and key != "query":
                parsed[key] = value

    return parsed


def _build_api_discovery_url(
    api_base: str,
    route_kind: str,
    route_value: str,
    page: int,
    query: Optional[str] = None,
) -> str:
    """Build a public API URL for discovery pagination."""
    page = max(1, int(page))

    if route_kind == "search":
        url = f"{api_base}/api/search?q={quote(query or route_value, safe='')}"
        if page > 1:
            url += f"&page={page}"
        return url

    value = quote(route_value, safe="-")
    if route_kind == "tag":
        if page == 1:
            return f"{api_base}/api/tag/{value}"
        return f"{api_base}/api/tag/{value}/{page}"

    if route_kind == "channel":
        if page == 1:
            return f"{api_base}/api/channel/{value}"
        return f"{api_base}/api/channel/{value}/{page}"

    return f"{api_base}/api/feed/{page}"


def _discovery_page_number(
    url: Optional[str],
    route_kind: Optional[str] = None,
) -> Optional[int]:
    """Extract a page number from a discovery pagination URL."""
    if not url:
        return None

    parsed = urlparse(url)
    query_page = parsed.query
    match = re.search(r"(?:^|&)page=(\d+)(?:&|$)", query_page)
    if match:
        return int(match.group(1))

    page = extract_page_number_from_url(url)
    if page is not None:
        return page

    page = extract_generic_page_number_from_url(url)
    if page is not None:
        return page

    # Page 1 links often omit `/page/1` entirely.
    path = parsed.path.rstrip("/")
    if route_kind == "tag" and "/watch/" in path:
        return 1
    if route_kind == "channel" and "/channels/" in path:
        return 1
    if route_kind == "search":
        if re.search(r"/x/[^/]+/?$", path):
            return 1
        match = re.search(r"/x/[^/]+/(\d+)/?$", path)
        if match:
            return int(match.group(1))

    return None


def _normalize_discovery_pagination(
    pagination: Dict[str, Optional[str]],
    api_base: str,
    route_kind: Optional[str],
    route_value: Optional[str],
    query: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Convert upstream discovery links into this API's discovery links."""
    result: Dict[str, Optional[str]] = {"next": None, "previous": None}

    if not route_kind or not route_value:
        return result

    for key in ("next", "previous"):
        upstream = pagination.get(key)
        page = _discovery_page_number(upstream, route_kind)
        if page is not None:
            result[key] = _build_api_discovery_url(
                api_base,
                route_kind,
                route_value,
                page,
                query=query,
            )

    return result


def _normalize_page_value(page: int) -> int:
    if page < 1:
        raise HTTPException(status_code=400, detail="Page must be 1 or greater.")
    return page


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
            "/api/search?q={query}",
            "/api/tag/{tag}",
            "/api/channel/{username}",
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


@app.get("/api/search")
async def search(request: Request, q: str, page: int = 1) -> Dict[str, Any]:
    """Search Desihub through its search page."""
    page = _normalize_page_value(page)
    query = unquote(q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query is required.")

    # Desihub search is path-based: /x/<query>.
    # IMPORTANT: search pagination is /x/<query>/2, /x/<query>/3, ...
    # (not /page/N). This is confirmed by the captured search page.
    encoded_query = quote(query, safe="-")
    upstream_url = f"{BASE_URL}/x/{encoded_query}"
    if page > 1:
        upstream_url += f"/{page}"

    return await scrape_listing(
        request,
        upstream_url,
        page,
        {
            "route_kind": "search",
            "route_value": query,
            "query": query,
            "search_query": query,
        },
    )


@app.get("/api/tag/{tag}")
async def tag_page(request: Request, tag: str) -> Dict[str, Any]:
    """Scrape one Desihub tag page."""
    clean_tag = unquote(tag or "").strip().strip("/")
    if not clean_tag:
        raise HTTPException(status_code=400, detail="Invalid tag.")

    upstream_url = f"{BASE_URL}/watch/{quote(clean_tag, safe='-') }"
    return await scrape_listing(
        request,
        upstream_url,
        1,
        {
            "route_kind": "tag",
            "route_value": clean_tag,
            "tag": clean_tag,
        },
    )


@app.get("/api/tag/{tag}/{page}")
async def tag_page_number(request: Request, tag: str, page: int) -> Dict[str, Any]:
    """Scrape a paginated Desihub tag page."""
    page = _normalize_page_value(page)
    clean_tag = unquote(tag or "").strip().strip("/")
    if not clean_tag:
        raise HTTPException(status_code=400, detail="Invalid tag.")

    upstream_url = f"{BASE_URL}/watch/{quote(clean_tag, safe='-')}/page/{page}"
    return await scrape_listing(
        request,
        upstream_url,
        page,
        {
            "route_kind": "tag",
            "route_value": clean_tag,
            "tag": clean_tag,
        },
    )


@app.get("/api/channel/{username}")
async def channel_page(request: Request, username: str) -> Dict[str, Any]:
    """Scrape one Desihub channel page."""
    clean_username = unquote(username or "").strip().strip("/")
    if not clean_username:
        raise HTTPException(status_code=400, detail="Invalid channel username.")

    upstream_url = f"{BASE_URL}/channels/{quote(clean_username, safe='_-') }"
    return await scrape_listing(
        request,
        upstream_url,
        1,
        {
            "route_kind": "channel",
            "route_value": clean_username,
            "username": clean_username,
        },
    )


@app.get("/api/channel/{username}/{page}")
async def channel_page_number(
    request: Request,
    username: str,
    page: int,
) -> Dict[str, Any]:
    """Scrape a paginated Desihub channel page."""
    page = _normalize_page_value(page)
    clean_username = unquote(username or "").strip().strip("/")
    if not clean_username:
        raise HTTPException(status_code=400, detail="Invalid channel username.")

    upstream_url = f"{BASE_URL}/channels/{quote(clean_username, safe='_-')}/page/{page}"
    return await scrape_listing(
        request,
        upstream_url,
        page,
        {
            "route_kind": "channel",
            "route_value": clean_username,
            "username": clean_username,
        },
    )


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
