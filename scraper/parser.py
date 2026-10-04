import json
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import ScrapedItem


def detect_framework(soup: BeautifulSoup, html: str):

    if soup.find(id="__next") or "__next_f.push" in html:
        return "Next.js"

    if soup.find("div", id="root"):
        return "React"

    return None


def extract_next_data(soup: BeautifulSoup):

    script = soup.find("script", id="__NEXT_DATA__")

    if not script or not script.string:
        return None

    try:
        return json.loads(script.string)
    except json.JSONDecodeError:
        return None


def parse_page(html: str, base_url: str):

    soup = BeautifulSoup(html, "html.parser")

    title = None

    if soup.title:
        title = soup.title.get_text(" ", strip=True)

    links = soup.find_all("a", href=True)
    images = soup.find_all("img")
    scripts = soup.find_all("script")

    items = []

    for link in links:

        href = link.get("href")

        if not href:
            continue

        url = urljoin(base_url, href)

        text = link.get_text(" ", strip=True)

        image = link.find("img")

        thumbnail = None

        if image:
            thumbnail = (
                image.get("src")
                or image.get("data-src")
                or image.get("data-lazy-src")
            )

            if thumbnail:
                thumbnail = urljoin(base_url, thumbnail)

        # Ignore completely empty links
        if not text and not thumbnail:
            continue

        items.append(
            ScrapedItem(
                title=text or None,
                url=url,
                thumbnail=thumbnail,
            )
        )

    # Basic duplicate removal
    unique = {}

    for item in items:

        key = item.url

        if key and key not in unique:
            unique[key] = item

    return {
        "title": title,
        "framework": detect_framework(soup, html),
        "next_data": extract_next_data(soup) is not None,
        "links_found": len(links),
        "images_found": len(images),
        "scripts_found": len(scripts),
        "items": list(unique.values()),
    }
