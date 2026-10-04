from fastapi import FastAPI, HTTPException

from scraper.fetcher import fetch_page
from scraper.parser import parse_page


app = FastAPI(
    title="Kamini Scraper API",
    version="0.1.0"
)


# ─────────────────────────────────────────────
# API ROOT
# ─────────────────────────────────────────────

@app.get("/api")
async def api_root():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "version": "0.1.0"
    }


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "kamini-scraper"
    }


# ─────────────────────────────────────────────
# FIRST FEED PAGE
# https://desihub.sh/feed
# ─────────────────────────────────────────────

@app.get("/api/feed")
async def feed_first_page():

    target = "https://desihub.sh/feed"

    try:
        result = await fetch_page(target)

        parsed = parse_page(
            result["html"],
            result["url"]
        )

        return {
            "source": target,
            "page": 1,
            "status": result["status"],
            "final_url": result["url"],
            **parsed
        }

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


# ─────────────────────────────────────────────
# NUMBERED FEED PAGES
#
# /api/feed/2
# /api/feed/3
# /api/feed/4
# ...
# ─────────────────────────────────────────────

@app.get("/api/feed/{page:int}")
async def feed_numbered_page(page: int):

    if page < 2:
        raise HTTPException(
            status_code=400,
            detail="Page 1 uses /api/feed. Numbered feed pages start from /api/feed/2."
        )

    target = f"https://desihub.sh/feed/{page}"

    try:
        result = await fetch_page(target)

        parsed = parse_page(
            result["html"],
            result["url"]
        )

        return {
            "source": target,
            "page": page,
            "status": result["status"],
            "final_url": result["url"],
            **parsed
        }

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=str(error)
        )
