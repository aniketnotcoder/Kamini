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

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from fastapi import FastAPI, HTTPException, Request

from fetcher import fetch_page
from scraper.parser import (
    extract_generic_page_number_from_url,
    extract_page_number_from_url,
    normalize_slug,
    parse_listing_page,
    parse_page,
    parse_post_page,
    parser_debug_info,
)


APP_VERSION = "0.9.0"
BASE_URL = "https://desihub.sh"

# Search enrichment intentionally uses a small concurrency limit.  A search
# page normally contains 12 cards, so this avoids turning one API request into
# a burst of 12+ simultaneous upstream requests.
SEARCH_ENRICH_CONCURRENCY = 4

app = FastAPI(
    title="Desihub Scraper API",
    version=APP_VERSION,
    description=(
        "Stateless Desihub feed/post scraper using rendered HTML and "
        "Next.js RSC data."
    ),
)


# -----------------------------------------------------------------------------
# Public API URL helpers
# -----------------------------------------------------------------------------


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


# -----------------------------------------------------------------------------
# Slug helpers
# -----------------------------------------------------------------------------


def normalize_requested_slug(value: str) -> str:
    """Normalize a route slug without accepting an empty value."""
    value = unquote(value or "").strip()

    if "/feed/" in value:
        value = value.split("/feed/", 1)[1]

    if "/post/" in value:
        value = value.split("/post/", 1)[1]

    value = value.strip("/")
    slug = normalize_slug(value)

    if not slug:
        raise HTTPException(status_code=400, detail="Invalid post slug.")

    return slug


# -----------------------------------------------------------------------------
# Feed scraping
# -----------------------------------------------------------------------------


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


# -----------------------------------------------------------------------------
# Generic listing scraping
# -----------------------------------------------------------------------------


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
            if key not in {"route_kind", "route_value", "query"}:
                parsed[key] = value

    return parsed


# -----------------------------------------------------------------------------
# Discovery pagination helpers
# -----------------------------------------------------------------------------


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


# -----------------------------------------------------------------------------
# Search post enrichment
# -----------------------------------------------------------------------------


def _post_url(slug: str, route: str) -> str:
    """Build exactly one upstream post URL for the requested post family."""
    encoded = quote(slug, safe="-")
    if route == "feed":
        return f"{BASE_URL}/feed/{encoded}"
    return f"{BASE_URL}/post/{encoded}"


def _candidate_post_urls(slug: str) -> List[str]:
    """Legacy search enrichment candidates; public post routes do not use this."""
    encoded = quote(slug, safe="-")
    return [
        f"{BASE_URL}/post/{encoded}",
        f"{BASE_URL}/feed/{encoded}",
    ]


async def _resolve_search_item(
    item: Dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> Dict[str, Any]:
    """
    Resolve one search card into a full public post item.

    Search pages intentionally contain lightweight cards.  Their `media` array
    is not necessarily present there, so each card is resolved against its
    actual post page.  The first successful parser result wins.
    """
    slug = normalize_slug(item.get("slug"))
    if not slug:
        return item

    async with semaphore:
        errors: List[str] = []

        for target_url in _candidate_post_urls(slug):
            try:
                result = await fetch_page(target_url)
            except Exception as exc:
                errors.append(f"{target_url}: {exc}")
                continue

            html = result.get("html") or ""
            if not html:
                errors.append(f"{target_url}: empty response")
                continue

            try:
                parsed = parse_post_page(html, slug, BASE_URL)
            except Exception as exc:
                errors.append(f"{target_url}: parser error: {exc}")
                continue

            if parsed is None:
                errors.append(f"{target_url}: post parser returned no match")
                continue

            # Keep the search-card thumbnail if the direct page has no first
            # video thumbnail, but prefer all richer post data from the parser.
            merged = dict(item)
            merged.update(parsed)

            # Search cards use /post/<slug>; expose the exact route that was
            # successfully resolved instead of silently changing it to /feed.
            merged["url"] = target_url
            merged["post_source"] = target_url
            merged["slug"] = slug

            if not merged.get("thumbnail") and item.get("thumbnail"):
                merged["thumbnail"] = item.get("thumbnail")

            # Do not stop at the first 200 response.  Desihub currently has
            # two post URL shapes in circulation:
            #   /post/<slug>
            #   /feed/<slug>
            #
            # A /post/<slug> response can be a valid HTML page but still be
            # parsed as `unknown` when that route variant does not contain the
            # direct-post media wrapper.  In that case we MUST continue to the
            # /feed/<slug> fallback instead of returning the empty card.
            #
            # For search enrichment, a real media-bearing parse is the useful
            # success condition.  If the parser identified the post but found
            # no media, keep it only as a fallback candidate and continue trying
            # the other route first.
            parsed_media_count = parsed.get("media_count")
            parsed_type = parsed.get("type")
            if parsed_type != "unknown" or (
                isinstance(parsed_media_count, int) and parsed_media_count > 0
            ):
                merged.pop("post_resolve_error", None)
                return merged

            errors.append(
                f"{target_url}: parser matched post but returned no media "
                f"(type={parsed_type!r}, media_count={parsed_media_count!r})"
            )
            continue

        # Do not delete the search result if an individual post fails.  Keep the
        # original card and expose a small diagnostic so the caller knows why
        # it remained un-enriched.
        failed = dict(item)
        if errors:
            failed["post_resolve_error"] = errors[-1]
        return failed


async def enrich_search_results(items: Any) -> List[Dict[str, Any]]:
    """Enrich all search cards concurrently while preserving their order."""
    if not isinstance(items, list) or not items:
        return []

    semaphore = asyncio.Semaphore(SEARCH_ENRICH_CONCURRENCY)

    tasks = [
        _resolve_search_item(item, semaphore)
        if isinstance(item, dict)
        else _resolve_search_item({}, semaphore)
        for item in items
    ]

    results = await asyncio.gather(*tasks, return_exceptions=False)
    return results


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


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
        "search_enrichment": True,
        "search_enrichment_concurrency": SEARCH_ENRICH_CONCURRENCY,
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
    """Search Desihub and enrich every discovered post with its full media."""
    page = _normalize_page_value(page)
    query = unquote(q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query is required.")

    # Desihub search is path-based:
    #   /x/<query>
    #   /x/<query>/2
    #   /x/<query>/3
    encoded_query = quote(query, safe="-")
    upstream_url = f"{BASE_URL}/x/{encoded_query}"
    if page > 1:
        upstream_url += f"/{page}"

    parsed = await scrape_listing(
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

    # IMPORTANT: this is the missing Phase-5 step that the broken response
    # demonstrated.  Search card extraction alone only gives title/slug/
    # thumbnail.  Resolve each actual post page before returning the response.
    parsed["items"] = await enrich_search_results(parsed.get("items") or [])
    parsed["count"] = len(parsed["items"])

    return parsed


@app.get("/api/tag/{tag}")
async def tag_page(request: Request, tag: str) -> Dict[str, Any]:
    """Scrape one Desihub tag page."""
    clean_tag = unquote(tag or "").strip().strip("/")
    if not clean_tag:
        raise HTTPException(status_code=400, detail="Invalid tag.")

    upstream_url = f"{BASE_URL}/watch/{quote(clean_tag, safe='-')}"
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

    upstream_url = f"{BASE_URL}/channels/{quote(clean_username, safe='_-')}"
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


@app.get("/api/feed/{slug:path}")
async def feed_post_page(request: Request, slug: str) -> Dict[str, Any]:
    """Scrape one individual upstream /feed/<slug> page only."""
    requested_slug = normalize_requested_slug(slug)
    upstream_url = _post_url(requested_slug, "feed")

    try:
        result = await fetch_page(upstream_url)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch upstream feed post: {exc}",
        ) from exc

    html = result.get("html") or ""
    if not html:
        raise HTTPException(
            status_code=502,
            detail="Upstream feed post returned an empty HTML response.",
        )

    try:
        parsed = parse_post_page(
            html,
            requested_slug,
            BASE_URL,
            route_hint="feed",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Feed post parser error: {exc}",
        ) from exc

    if parsed is None:
        debug = parser_debug_info(html)
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Feed post not found.",
                "requested_slug": requested_slug,
                "upstream_url": result.get("url") or upstream_url,
                "html_length": result.get("html_length", len(html)),
                "rsc_feed_count": debug.get("feed_object_count", 0),
            },
        )

    return {
        **parsed,
        "source": upstream_url,
        "post_source": upstream_url,
        "requested_slug": requested_slug,
    }


@app.get("/api/post/{slug:path}")
async def post_page(request: Request, slug: str) -> Dict[str, Any]:
    """Scrape one individual upstream /post/<slug> page only."""
    requested_slug = normalize_requested_slug(slug)
    upstream_url = _post_url(requested_slug, "post")

    try:
        result = await fetch_page(upstream_url)
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

    try:
        parsed = parse_post_page(
            html,
            requested_slug,
            BASE_URL,
            route_hint="post",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Post parser error: {exc}",
        ) from exc

    if parsed is None:
        debug = parser_debug_info(html)
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Post not found.",
                "requested_slug": requested_slug,
                "upstream_url": result.get("url") or upstream_url,
                "html_length": result.get("html_length", len(html)),
                "rsc_feed_count": debug.get("feed_object_count", 0),
            },
        )

    return {
        **parsed,
        "source": upstream_url,
        "post_source": upstream_url,
        "requested_slug": requested_slug,
    }
