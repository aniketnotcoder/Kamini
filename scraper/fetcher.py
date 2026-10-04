import httpx


# ============================================================
# BROWSER-LIKE HEADERS
# ============================================================

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),

    "Accept": (
        "text/html,"
        "application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,"
        "image/webp,"
        "*/*;q=0.8"
    ),

    "Accept-Language": (
        "en-US,en;q=0.9"
    ),

    "Accept-Encoding": (
        "gzip, deflate, br"
    ),

    "Cache-Control": "no-cache",

    "Pragma": "no-cache",

    "Upgrade-Insecure-Requests": "1",
}


# ============================================================
# FETCH PAGE
# ============================================================

async def fetch_page(url: str) -> dict:
    """
    Fetch a webpage asynchronously.

    Returns:

    {
        "html": "...",
        "status": 200,
        "url": "https://..."
    }
    """

    timeout = httpx.Timeout(
        connect=10.0,
        read=30.0,
        write=10.0,
        pool=10.0,
    )

    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        follow_redirects=True,
        timeout=timeout,
        http2=True,
    ) as client:

        response = await client.get(url)

        response.raise_for_status()

        return {
            "html": response.text,
            "status": response.status_code,
            "url": str(response.url),
        }
