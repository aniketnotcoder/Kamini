from fastapi import FastAPI, HTTPException

from scraper.fetcher import fetch_page
from scraper.parser import parse_page


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="Kamini Scraper API",
    description="Fast parser API for Desihub feed pages",
    version="0.2.0",
)


BASE_URL = "https://desihub.sh"


# ============================================================
# API ROOT
# ============================================================

@app.get("/api")
async def api_root():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "version": "0.2.0",
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "status_code": 200,
    }


# ============================================================
# FEED PAGE 1
#
# API:
# /api/feed
#
# TARGET:
# https://desihub.sh/feed
# ============================================================

@app.get("/api/feed")
async def feed_first_page():

    target_url = f"{BASE_URL}/feed"

    try:

        result = await fetch_page(target_url)

        parsed = parse_page(
            result["html"],
            result["url"],
        )

        return {
            "source": target_url,
            "page": 1,
            "status": result["status"],
            "final_url": result["url"],
            **parsed,
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=f"Feed scraping failed: {str(error)}",
        )


# ============================================================
# FEED PAGE 2+
#
# API:
# /api/feed/2
# /api/feed/3
# /api/feed/4
#
# TARGET:
# https://desihub.sh/feed/page/2
# https://desihub.sh/feed/page/3
# ============================================================

@app.get("/api/feed/{page:int}")
async def feed_numbered_page(page: int):

    if page < 2:

        raise HTTPException(
            status_code=400,
            detail=(
                "Page 1 uses /api/feed. "
                "Numbered pages start from /api/feed/2."
            ),
        )

    target_url = f"{BASE_URL}/feed/page/{page}"

    try:

        result = await fetch_page(target_url)

        parsed = parse_page(
            result["html"],
            result["url"],
        )

        return {
            "source": target_url,
            "page": page,
            "status": result["status"],
            "final_url": result["url"],
            **parsed,
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=f"Feed page {page} scraping failed: {str(error)}",
        )
