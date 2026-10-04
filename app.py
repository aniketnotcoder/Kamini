from __future__ import annotations

import os
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request

from scraper.fetcher import fetch_page
from scraper.parser import (
    normalize_requested_slug,
    parse_page,
    parse_post_page,
)


app = FastAPI(
    title="Kamini Scraper API",
    description="Parser API for Desihub feed pages and individual posts",
    version="0.5.0",
)


BASE_URL = "https://desihub.sh"
PUBLIC_API_BASE_URL = os.getenv("PUBLIC_API_BASE_URL", "").strip()


def get_public_api_base_url(request: Request) -> str:
    """
    Determine the public origin of this API.

    If PUBLIC_API_BASE_URL is configured, it wins.

    Otherwise use Vercel/proxy forwarding headers when available, then the
    FastAPI request URL.
    """
    if PUBLIC_API_BASE_URL:
        return PUBLIC_API_BASE_URL.rstrip("/")

    forwarded_host = request.headers.get("x-forwarded-host")
    forwarded_proto = request.headers.get("x-forwarded-proto")

    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{forwarded_host}".rstrip("/")

    host = request.headers.get("host")

    if host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{host}".rstrip("/")

    parsed = urlparse(str(request.base_url))

    if parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    return str(request.base_url).rstrip("/")


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
        "endpoints": {
            "health": "/api/health",
            "feed": "/api/feed",
            "feed_page": "/api/feed/{page}",
            "post": "/api/post/{slug}",
        },
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "status_code": 200,
    }


async def scrape_feed(
    target_url: str,
    requested_page: int,
    api_base_url: str,
):
    try:
        result = await fetch_page(target_url)

        parsed = parse_page(
            result["html"],
            BASE_URL,
            api_base_url=api_base_url,
        )

        return {
            "source": target_url,
            "page": requested_page,
            "status": result["status"],
            "final_url": result["url"],
            "content_type": result.get("content_type"),
            "html_length": result.get("html_length"),
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


@app.get("/api/feed")
async def feed_first_page(request: Request):
    api_base_url = get_public_api_base_url(request)

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
        target_url = f"{BASE_URL}/feed"
    else:
        target_url = f"{BASE_URL}/feed/page/{page}"

    api_base_url = get_public_api_base_url(request)

    return await scrape_feed(
        target_url,
        page,
        api_base_url,
    )


@app.get("/api/post/{slug}")
async def post_by_slug(
    slug: str,
):
    requested_slug = normalize_requested_slug(slug)

    if not requested_slug:
        raise HTTPException(
            status_code=400,
            detail="A valid post slug is required.",
        )

    target_url = f"{BASE_URL}/feed/{requested_slug}"

    try:
        result = await fetch_page(target_url)

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
            "content_type": result.get("content_type"),
            "html_length": result.get("html_length"),
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
