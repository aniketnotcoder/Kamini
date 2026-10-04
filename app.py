from fastapi import FastAPI

from scraper.fetcher import fetch_page
from scraper.parser import parse_page


app = FastAPI(
    title="Kamini Scraper API",
    version="0.1.0"
)


@app.get("/api")
async def api_root():
    return {
        "status": "ok",
        "service": "kamini-scraper",
        "version": "0.1.0"
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "kamini-scraper"
    }


@app.get("/api/feed/{page}")
async def scrape_feed(page: int):

    target = f"https://desihub.sh/feed/{page}"

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
