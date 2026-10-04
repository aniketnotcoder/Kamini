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


@app.get("/api/scrape")
async def scrape():

    target = "https://desihub.sh/"

    result = await fetch_page(target)

    parsed = parse_page(
        result["html"],
        result["url"]
    )

    return {
        "source": target,
        "status": result["status"],
        "final_url": result["url"],
        **parsed
    }
