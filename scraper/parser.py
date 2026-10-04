import html as html_lib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


def clean_text(value):
    if value is None:
        return None

    value = html_lib.unescape(str(value))
    value = re.sub(r"\s+", " ", value)
    value = value.strip()

    return value or None


def absolute_url(url, base_url):
    if not url:
        return None

    url = html_lib.unescape(str(url)).strip()

    if url.startswith("//"):
        return "https:" + url

    return urljoin(base_url, url)


def decode_next_string(value):
    if not value:
        return None

    value = value.replace('\\"', '"')
    value = value.replace("\\/", "/")
    value = value.replace("\\u0026", "&")
    value = value.replace("\\u002F", "/")

    try:
        value = bytes(value, "utf-8").decode("unicode_escape")
    except Exception:
        pass

    return html_lib.unescape(value)


def extract_next_video_data(source_html):
    """
    Extract Desihub media information from Next.js RSC data.

    Expected fields:
      videoId
      videoUrl
      thumbnailUrl
      duration
    """

    data = {}

    # Find every videoId in the page source.
    video_matches = list(
        re.finditer(
            r'"videoId"\s*:\s*"([^"]+)"',
            source_html,
            re.DOTALL,
        )
    )

    for match in video_matches:
        video_id = decode_next_string(match.group(1))

        if not video_id:
            continue

        # Only inspect a reasonable area around this video object.
        start = match.start()
        end = min(len(source_html), start + 5000)

        block = source_html[start:end]

        video_url_match = re.search(
            r'"videoUrl"\s*:\s*"([^"]+)"',
            block,
            re.DOTALL,
        )

        thumbnail_match = re.search(
            r'"thumbnailUrl"\s*:\s*"([^"]+)"',
            block,
            re.DOTALL,
        )

        duration_match = re.search(
            r'"duration"\s*:\s*(\d+)',
            block,
            re.DOTALL,
        )

        embed_match = re.search(
            r'"url"\s*:\s*"([^"]*?/embed/[^"]+)"',
            block,
            re.DOTALL,
        )

        data[video_id] = {
            "video_id": video_id,
            "video_url": (
                decode_next_string(video_url_match.group(1))
                if video_url_match
                else None
            ),
            "thumbnail": (
                decode_next_string(thumbnail_match.group(1))
                if thumbnail_match
                else None
            ),
            "duration": (
                int(duration_match.group(1))
                if duration_match
                else None
            ),
            "embed_url": (
                decode_next_string(embed_match.group(1))
                if embed_match
                else None
            ),
        }

    return data


def extract_video_id(embed_url):
    if not embed_url:
        return None

    parsed = urlparse(embed_url)

    path = parsed.path.rstrip("/")

    match = re.search(
        r"/embed/([^/?#]+)",
        path,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return None


def extract_channel_data(post_link, base_url):
    """
    The channel link is normally a sibling of the post link
    inside the same card/container.
    """

    channel = {
        "name": None,
        "username": None,
        "url": None,
        "avatar": None,
    }

    parent = post_link.parent

    if not parent:
        return channel

    # Search current container first.
    candidates = parent.find_all(
        "a",
        href=True,
    )

    # If not found, search a slightly larger container.
    if not candidates and parent.parent:
        candidates = parent.parent.find_all(
            "a",
            href=True,
        )

    for link in candidates:

        href = link.get("href")

        if not href:
            continue

        if "/channels/" not in href:
            continue

        channel["url"] = absolute_url(
            href,
            base_url,
        )

        text = clean_text(
            link.get_text(
                " ",
                strip=True,
            )
        )

        image = link.find("img")

        if image:
            channel["avatar"] = absolute_url(
                image.get("src")
                or image.get("data-src")
                or image.get("data-lazy-src"),
                base_url,
            )

        if text:
            # Usually:
            # Channel Name@username
            if "@" in text:
                name, username = text.rsplit(
                    "@",
                    1,
                )

                channel["name"] = clean_text(name)
                channel["username"] = clean_text(
                    "@" + username
                )
            else:
                channel["name"] = text

        break

    return channel


def extract_pagination(soup, base_url):
    next_url = None
    previous_url = None

    for link in soup.find_all("a", href=True):

        text = clean_text(
            link.get_text(
                " ",
                strip=True,
            )
        )

        href = link.get("href")

        if not href:
            continue

        href_lower = href.lower()

        if (
            text
            and "next" in text.lower()
        ) or "next" in href_lower:

            next_url = absolute_url(
                href,
                base_url,
            )

        if (
            text
            and (
                "previous" in text.lower()
                or "prev" in text.lower()
            )
        ) or "previous" in href_lower:

            previous_url = absolute_url(
                href,
                base_url,
            )

    return {
        "next": next_url,
        "previous": previous_url,
    }


def extract_page_number(page_url):
    match = re.search(
        r"/feed/page/(\d+)",
        page_url,
        re.IGNORECASE,
    )

    if match:
        return int(match.group(1))

    return 1


def parse_page(source_html, page_url):

    soup = BeautifulSoup(
        source_html,
        "html.parser",
    )

    # ---------------------------------------------------------
    # PAGE TITLE
    # ---------------------------------------------------------

    title = None

    if soup.title:
        title = clean_text(
            soup.title.get_text(
                " ",
                strip=True,
            )
        )

    # ---------------------------------------------------------
    # NEXT.JS MEDIA DATA
    # ---------------------------------------------------------

    media_data = extract_next_video_data(
        source_html
    )

    # ---------------------------------------------------------
    # FEED POSTS
    # ---------------------------------------------------------

    post_links = []

    for link in soup.find_all(
        "a",
        href=True,
    ):

        href = link.get("href")

        if not href:
            continue

        # Feed posts:
        #
        # /feed/something
        #
        # But NOT:
        #
        # /feed/page/2
        #

        if not re.match(
            r"^/feed/(?!page/)[^/?#]+",
            href,
            re.IGNORECASE,
        ):
            continue

        post_links.append(link)

    # ---------------------------------------------------------
    # ITEMS
    # ---------------------------------------------------------

    items = []
    seen_urls = set()

    for post_link in post_links:

        href = post_link.get("href")

        post_url = absolute_url(
            href,
            page_url,
        )

        if not post_url:
            continue

        if post_url in seen_urls:
            continue

        seen_urls.add(post_url)

        # -----------------------------------------------------
        # TITLE
        # -----------------------------------------------------

        heading = post_link.find("h2")

        if heading:
            item_title = clean_text(
                heading.get_text(
                    " ",
                    strip=True,
                )
            )
        else:
            item_title = clean_text(
                post_link.get_text(
                    " ",
                    strip=True,
                )
            )

        # -----------------------------------------------------
        # EMBED
        # -----------------------------------------------------

        iframe = post_link.find(
            "iframe",
            src=True,
        )

        embed_url = None

        if iframe:
            embed_url = absolute_url(
                iframe.get("src"),
                page_url,
            )

        # -----------------------------------------------------
        # VIDEO ID
        # -----------------------------------------------------

        video_id = extract_video_id(
            embed_url
        )

        # -----------------------------------------------------
        # MEDIA DATA
        # -----------------------------------------------------

        media = None

        if video_id:
            media = media_data.get(
                video_id
            )

        # -----------------------------------------------------
        # CHANNEL
        # -----------------------------------------------------

        channel = extract_channel_data(
            post_link,
            page_url,
        )

        # -----------------------------------------------------
        # FINAL ITEM
        # -----------------------------------------------------

        items.append(
            {
                "title": item_title,
                "url": post_url,
                "channel": channel,
                "embed_url": embed_url,
                "video_id": video_id,
                "video_url": (
                    media.get("video_url")
                    if media
                    else None
                ),
                "thumbnail": (
                    media.get("thumbnail")
                    if media
                    else None
                ),
                "duration": (
                    media.get("duration")
                    if media
                    else None
                ),
            }
        )

    # ---------------------------------------------------------
    # PAGINATION
    # ---------------------------------------------------------

    pagination = extract_pagination(
        soup,
        page_url,
    )

    # The site's pagination can be represented
    # more reliably from the current page number.
    current_page = extract_page_number(
        page_url
    )

    if not pagination["previous"] and current_page > 1:
        pagination["previous"] = (
            f"{urljoin(page_url, '/feed')}"
            if current_page == 2
            else f"{urljoin(page_url, f'/feed/page/{current_page - 1}')}"
        )

    if not pagination["next"]:
        pagination["next"] = None

    return {
        "title": title,
        "page": current_page,
        "count": len(items),
        "items": items,
        "pagination": pagination,
    }
