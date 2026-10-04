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
import httpx
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from fastapi import FastAPI, HTTPException, Request

from fetcher import DEFAULT_HEADERS, fetch_page
from scraper.parser import (
    extract_embed_video_metadata,
    extract_generic_page_number_from_url,
    extract_page_number_from_url,
    normalize_slug,
    parse_feed_post_page,
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
FEED_MEDIA_ENRICH_CONCURRENCY = 4

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


def detect_post_route(value: Any) -> Optional[str]:
    """Detect whether a listing URL belongs to /feed/ or /post/."""
    if not value:
        return None
    text = unquote(str(value)).strip()
    parsed = urlparse(text)
    path = parsed.path or text
    if re.search(r"(?:^|/)feed/", path, flags=re.IGNORECASE):
        return "feed"
    if re.search(r"(?:^|/)post/", path, flags=re.IGNORECASE):
        return "post"
    return None


def _post_url(slug: str, route: str) -> str:
    """Build exactly one upstream post URL for the requested post family."""
    encoded = quote(slug, safe="-")
    if route == "feed":
        return f"{BASE_URL}/feed/{encoded}"
    return f"{BASE_URL}/post/{encoded}"


def _candidate_post_urls(item: Dict[str, Any], slug: str) -> List[Tuple[str, str]]:
    """Choose the exact upstream post family represented by a listing card."""
    encoded = quote(slug, safe="-")
    route = detect_post_route(item.get("url")) or detect_post_route(item.get("source"))

    # Search cards on Desihub use /post/<slug>.
    if route == "post":
        return [("post", f"{BASE_URL}/post/{encoded}")]

    # Feed/channel cards use /feed/<slug>.
    if route == "feed":
        return [("feed", f"{BASE_URL}/feed/{encoded}")]

    # Unknown listing shape: prefer /post because this resolver is used by
    # search enrichment, while explicit Feed cards are handled above.
    return [("post", f"{BASE_URL}/post/{encoded}")]


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

        for route_hint, target_url in _candidate_post_urls(item, slug):
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
                parsed = parse_post_page(html, slug, BASE_URL, route_hint=route_hint)
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

            # Only accept a media-bearing result for enrichment.  The route is
            # already fixed by the source card, so there is no cross-route fallback.
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
# Individual Feed media metadata enrichment
# -----------------------------------------------------------------------------


async def _fetch_mp4_duration(video_url: str) -> int | None:
    """Read MP4 movie metadata with small HTTP range requests.

    This is only a fallback when the embed page does not expose duration.
    It never downloads the complete video intentionally.
    """
    if not video_url:
        return None

    timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)
    headers = {
        "User-Agent": DEFAULT_HEADERS.get("User-Agent", "Mozilla/5.0"),
        "Range": "bytes=0-2097151",
        "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, http2=False) as client:
        try:
            async with client.stream("GET", video_url, headers=headers) as response:
                if response.status_code not in (200, 206):
                    return None
                data = await response.aread()
                data = data[:2097152]
        except Exception:
            return None

    duration = _parse_mp4_mvhd_duration(data)
    if duration is not None:
        return duration

    # Fast-start MP4s normally put `moov` near the beginning. For files where
    # it is at the end, make one bounded tail request when Content-Length is
    # available.
    try:
        timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, http2=False) as client:
            head = await client.head(video_url, headers={"User-Agent": DEFAULT_HEADERS.get("User-Agent", "Mozilla/5.0")})
            total = int(head.headers.get("content-length", "0") or 0)
            if total <= 2097152:
                return None
            start = max(0, total - 2097152)
            async with client.stream(
                "GET",
                video_url,
                headers={
                    "User-Agent": DEFAULT_HEADERS.get("User-Agent", "Mozilla/5.0"),
                    "Range": f"bytes={start}-{total - 1}",
                },
            ) as response:
                if response.status_code not in (200, 206):
                    return None
                tail = (await response.aread())[:2097152]
    except Exception:
        return None

    return _parse_mp4_mvhd_duration(tail)


def _parse_mp4_mvhd_duration(data: bytes) -> int | None:
    """Extract integer seconds from an MP4 mvhd atom when present."""
    if not data:
        return None

    pos = 0
    length = len(data)
    while pos + 8 <= length:
        idx = data.find(b"mvhd", pos)
        if idx < 0 or idx + 24 > length:
            return None

        version = data[idx + 4]
        try:
            if version == 0 and idx + 24 <= length:
                timescale = int.from_bytes(data[idx + 16:idx + 20], "big")
                duration = int.from_bytes(data[idx + 20:idx + 24], "big")
            elif version == 1 and idx + 40 <= length:
                timescale = int.from_bytes(data[idx + 28:idx + 32], "big")
                duration = int.from_bytes(data[idx + 32:idx + 40], "big")
            else:
                pos = idx + 4
                continue
        except Exception:
            pos = idx + 4
            continue

        if timescale > 0 and duration >= 0:
            return int(duration / timescale)
        pos = idx + 4

    return None


async def _enrich_feed_media_item(
    item: Dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> Dict[str, Any]:
    """Resolve rich metadata for one Feed-post iframe without changing its ID."""
    if not isinstance(item, dict) or item.get("type") != "video":
        return item

    embed_url = item.get("embed_url")
    if not embed_url:
        return item

    async with semaphore:
        try:
            result = await fetch_page(str(embed_url))
            embed_html = result.get("html") or ""
            if not embed_html:
                return item

            metadata = extract_embed_video_metadata(embed_html, BASE_URL)
        except Exception:
            # Metadata enrichment is deliberately best-effort.  The Feed post
            # itself is already valid, so an embed failure must never turn the
            # whole endpoint into a 502/500.
            return item

    enriched = dict(item)

    if metadata.get("video_url"):
        enriched["video_url"] = metadata["video_url"]
    if metadata.get("thumbnail"):
        enriched["thumbnail"] = metadata["thumbnail"]
    if metadata.get("duration") is not None:
        enriched["duration"] = metadata["duration"]

    # Some Downloaddirect embed revisions expose poster/MP4 but omit duration.
    # Read the MP4 mvhd atom as a bounded fallback; this does not affect the
    # recommendation extraction or any other endpoint.
    if enriched.get("duration") is None and enriched.get("video_url"):
        duration = await _fetch_mp4_duration(str(enriched["video_url"]))
        if duration is not None:
            enriched["duration"] = duration

    return enriched


async def enrich_feed_post_media(item: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich all Feed-post video media concurrently while preserving order."""
    media = item.get("media") if isinstance(item, dict) else None
    if not isinstance(media, list) or not media:
        return item

    semaphore = asyncio.Semaphore(FEED_MEDIA_ENRICH_CONCURRENCY)
    tasks = [
        _enrich_feed_media_item(media_item, semaphore)
        for media_item in media
        if isinstance(media_item, dict)
    ]

    if not tasks:
        return item

    enriched_media = await asyncio.gather(*tasks, return_exceptions=False)
    result = dict(item)
    result["media"] = enriched_media

    videos = [entry for entry in enriched_media if entry.get("type") == "video"]
    first_video = videos[0] if videos else None

    # Keep the legacy top-level fields synchronized with the enriched first
    # video, exactly like build_public_item() does for RSC feed objects.
    result["embed_url"] = first_video.get("embed_url") if first_video else None
    result["video_id"] = first_video.get("video_id") if first_video else None
    result["video_url"] = first_video.get("video_url") if first_video else None
    result["thumbnail"] = first_video.get("thumbnail") if first_video else None
    result["duration"] = first_video.get("duration") if first_video else None

    return result


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
    """Scrape upstream /feed exactly as before."""
    return await scrape_feed(request, f"{BASE_URL}/feed", 1)


@app.get("/api/feed/{page}")
async def feed_page(request: Request, page: int) -> Dict[str, Any]:
    """Scrape upstream /feed/page/N.

    This is kept as the original Feed pagination route.
    """
    if page < 1:
        raise HTTPException(status_code=400, detail="Page must be 1 or greater.")

    return await scrape_feed(request, f"{BASE_URL}/feed/page/{page}", page)


@app.get("/api/feed/slug/{slug:path}")
async def feed_post_page(request: Request, slug: str) -> Dict[str, Any]:
    """Scrape one individual upstream /feed/<slug> page.

    Feed posts intentionally use their own explicit API namespace so a slug
    can never be mistaken for the integer Feed pagination parameter.
    """
    requested_slug = normalize_requested_slug(slug)
    if not requested_slug:
        raise HTTPException(status_code=400, detail="Invalid feed post slug.")

    upstream_url = f"{BASE_URL}/feed/{quote(requested_slug, safe='-')}"

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
        parsed = parse_feed_post_page(
            html,
            requested_slug,
            BASE_URL,
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

    # Feed pages render their main videos as iframe URLs, so the page itself
    # does not carry the rich videoUrl/thumbnail/duration fields that the RSC
    # recommendation objects carry. Resolve those fields from each embed page.
    parsed = await enrich_feed_post_media(parsed)

    return {
        **parsed,
        "source": upstream_url,
        "post_source": upstream_url,
        "requested_slug": requested_slug,
    }


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
