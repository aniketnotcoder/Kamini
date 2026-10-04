from fastapi import FastAPI, HTTPException

from scraper.fetcher import fetch_page
from scraper.parser import parse_page


# IMPORTANT:
# This must exist at the TOP LEVEL for Vercel.
app = FastAPI(
    title="Kamini Scraper API",
    description="Parser API for Desihub feed pages",
    version="0.3.0",
)

BASE_URL = "https://desihub.sh"


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "version": "0.3.0",
    }


@app.get("/api")
async def api_root():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "version": "0.3.0",
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "status_code": 200,
    }


async def scrape_feed(target_url: str, requested_page: int):
    try:
        result = await fetch_page(target_url)

        parsed = parse_page(
            result["html"],
            result["url"],
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
            detail=f"Scraping failed: {type(error).__name__}: {str(error)}",
        )


@app.get("/api/feed")
async def feed_first_page():
    return await scrape_feed(
        f"{BASE_URL}/feed",
        1,
    )


@app.get("/api/feed/{page}")
async def feed_numbered_page(page: int):

    if page < 1:
        raise HTTPException(
            status_code=400,
            detail="Page must be 1 or greater.",
        )

    if page == 1:
        target_url = f"{BASE_URL}/feed"
    else:
        target_url = f"{BASE_URL}/feed/page/{page}"

    return await scrape_feed(
        target_url,
        page,
    )
