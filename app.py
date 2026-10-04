from __future__ import annotations

from urllib.parse import quote, urlparse, unquote

from fastapi import FastAPI, HTTPException, Request

from scraper.fetcher import fetch_page
from scraper.parser import (
    parse_page,
    parse_post_page,
)


app = FastAPI(
    title="Kamini Scraper API",
    description="Parser API for Desihub feed pages and individual posts",
    version="0.5.0",
)


BASE_URL = "https://desihub.sh"


# ---------------------------------------------------------------------------
# Public API URL helpers
# ---------------------------------------------------------------------------

def get_public_api_base(
    request: Request,
) -> str:
    """
    Build the public base URL from the incoming request.

    Example:
        https://kamini-ivory.vercel.app

    This lets pagination links point back to our API instead of exposing
    upstream Desihub URLs.
    """

    forwarded_proto = request.headers.get(
        "x-forwarded-proto"
    )

    forwarded_host = request.headers.get(
        "x-forwarded-host"
    )

    host = (
        forwarded_host
        or request.headers.get("host")
    )

    if not host:
        return str(
            request.base_url
        ).rstrip("/")

    proto = (
        forwarded_proto.split(",")[0].strip()
        if forwarded_proto
        else request.url.scheme
    )

    return f"{proto}://{host}".rstrip("/")


def _extract_feed_page_number(
    path: str,
) -> int | None:
    """
    Convert an upstream pagination path to a feed page number.

    /feed          -> 1
    /feed/page/2   -> 2
    """

    parsed = urlparse(path)

    path = parsed.path.rstrip("/")

    if path == "/feed":
        return 1

    prefix = "/feed/page/"

    if not path.startswith(prefix):
        return None

    value = path[len(prefix):]

    try:
        page = int(value)
    except ValueError:
        return None

    if page < 1:
        return None

    return page


def _build_api_feed_url(
    api_base_url: str,
    page: int,
) -> str:
    if page <= 1:
        return (
            f"{api_base_url}/api/feed"
        )

    return (
        f"{api_base_url}/api/feed/{page}"
    )


def _normalize_public_pagination(
    pagination: dict,
    api_base_url: str,
) -> dict:
    """
    Convert parser-level upstream paths into public API URLs.

    Parser:
        /feed/page/7

    Public API:
        https://kamini-ivory.vercel.app/api/feed/7
    """

    result = {
        "next": None,
        "previous": None,
    }

    if not isinstance(
        pagination,
        dict,
    ):
        return result

    for direction in (
        "next",
        "previous",
    ):
        path = pagination.get(
            direction
        )

        if not path:
            continue

        page = _extract_feed_page_number(
            path
        )

        if page is None:
            continue

        result[direction] = (
            _build_api_feed_url(
                api_base_url,
                page,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Basic endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "version": "0.5.0",
    }


@app.get("/api")
async def api_root():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "version": "0.5.0",
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "status_code": 200,
    }


# ---------------------------------------------------------------------------
# Shared feed scraper
# ---------------------------------------------------------------------------

async def scrape_feed(
    target_url: str,
    requested_page: int,
    api_base_url: str,
):
    try:
        result = await fetch_page(
            target_url
        )

        parsed = parse_page(
            result["html"],
            result["url"],
        )

        parsed["pagination"] = (
            _normalize_public_pagination(
                parsed.get(
                    "pagination",
                    {},
                ),
                api_base_url,
            )
        )

        return {
            "source": target_url,
            "page": requested_page,
            "status": result["status"],
            "final_url": result["url"],
            "content_type": result.get(
                "content_type"
            ),
            "html_length": result.get(
                "html_length"
            ),
            **parsed,
        }

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "Scraping failed: "
                f"{type(error).__name__}: "
                f"{str(error)}"
            ),
        )


# ---------------------------------------------------------------------------
# Feed endpoints
# ---------------------------------------------------------------------------

@app.get("/api/feed")
async def feed_first_page(
    request: Request,
):
    api_base_url = get_public_api_base(
        request
    )

    return await scrape_feed(
        f"{BASE_URL}/feed",
        1,
        api_base_url,
    )


@app.get("/api/feed/{page}")
async def feed_numbered_page(
    page: int,
    request: Request,
):
    if page < 1:
        raise HTTPException(
            status_code=400,
            detail="Page must be 1 or greater.",
        )

    if page == 1:
        target_url = (
            f"{BASE_URL}/feed"
        )
    else:
        target_url = (
            f"{BASE_URL}/feed/page/{page}"
        )

    api_base_url = get_public_api_base(
        request
    )

    return await scrape_feed(
        target_url,
        page,
        api_base_url,
    )


# ---------------------------------------------------------------------------
# Individual post endpoint
# ---------------------------------------------------------------------------

def normalize_requested_slug(
    slug: str,
) -> str:
    """
    Normalize the route parameter without changing the actual slug.
    """

    value = unquote(
        str(slug or "")
    ).strip()

    value = value.strip("/")

    if value.startswith("feed/"):
        value = value[5:]

    return value


@app.get("/api/post/{slug}")
async def post_by_slug(
    slug: str,
):
    requested_slug = normalize_requested_slug(
        slug
    )

    if not requested_slug:
        raise HTTPException(
            status_code=400,
            detail="A valid post slug is required.",
        )

    target_url = (
        f"{BASE_URL}/feed/"
        f"{quote(requested_slug, safe='-')}"
    )

    try:
        result = await fetch_page(
            target_url
        )

        parsed = parse_post_page(
            result["html"],
            requested_slug,
            BASE_URL,
        )

        if parsed is None:
            raise HTTPException(
                status_code=404,
                detail="Post not found.",
            )

        return {
            "source": target_url,
            "status": result["status"],
            "final_url": result["url"],
            "content_type": result.get(
                "content_type"
            ),
            "html_length": result.get(
                "html_length"
            ),
            **parsed,
        }

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "Post scraping failed: "
                f"{type(error).__name__}: "
                f"{str(error)}"
            ),
        )
