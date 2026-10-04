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
    Extract media information from the Next.js RSC payload.

    Desihub serializes feed data roughly like:

        "mediaItems":[
            {
                "id":"...",
                "type":"video",
                "url":"https://downloaddirect.xyz/embed/...",
                "videoId":"...",
                "videoUrl":"https://videos.downloaddirect.xyz/....mp4",
                "thumbnailUrl":"https://images.downloaddirect.xyz/....webp",
                "duration":957
            }
        ]

    The RSC payload may be escaped, so normalize the quotes first.
    """

    data = {}

    if not source_html:
        return data

    # ---------------------------------------------------------
    # Normalize Next.js escaped JSON
    # ---------------------------------------------------------

    source = source_html

    source = source.replace('\\"', '"')
    source = source.replace("\\/", "/")
    source = source.replace("\\u0026", "&")
    source = source.replace("\\u002F", "/")

    # ---------------------------------------------------------
    # Extract mediaItems objects
    # ---------------------------------------------------------

    pattern = re.compile(
        r'"mediaItems"\s*:\s*\[\s*\{(.*?)\}\s*\]',
        re.DOTALL,
    )

    media_blocks = pattern.findall(source)

    for block in media_blocks:

        # -----------------------------------------------------
        # videoId
        # -----------------------------------------------------

        video_id_match = re.search(
            r'"videoId"\s*:\s*"([^"]+)"',
            block,
        )

        if not video_id_match:
            continue

        video_id = clean_text(
            video_id_match.group(1)
        )

        if not video_id:
            continue

        # -----------------------------------------------------
        # embed URL
        # -----------------------------------------------------

        embed_match = re.search(
            r'"url"\s*:\s*"([^"]+)"',
            block,
        )

        embed_url = (
            decode_next_string(
                embed_match.group(1)
            )
            if embed_match
            else None
        )

        # -----------------------------------------------------
        # direct video URL
        # -----------------------------------------------------

        video_url_match = re.search(
            r'"videoUrl"\s*:\s*"([^"]+)"',
            block,
        )

        video_url = (
            decode_next_string(
                video_url_match.group(1)
            )
            if video_url_match
            else None
        )

        # -----------------------------------------------------
        # thumbnail
        # -----------------------------------------------------

        thumbnail_match = re.search(
            r'"thumbnailUrl"\s*:\s*"([^"]+)"',
            block,
        )

        thumbnail = (
            decode_next_string(
                thumbnail_match.group(1)
            )
            if thumbnail_match
            else None
        )

        # -----------------------------------------------------
        # duration
        # -----------------------------------------------------

        duration_match = re.search(
            r'"duration"\s*:\s*(\d+(?:\.\d+)?)',
            block,
        )

        duration = None

        if duration_match:
            try:
                duration = int(
                    float(duration_match.group(1))
                )
            except (TypeError, ValueError):
                duration = None

        # -----------------------------------------------------
        # Save
        # -----------------------------------------------------

        data[video_id] = {
            "video_id": video_id,
            "video_url": video_url,
            "thumbnail": thumbnail,
            "duration": duration,
            "embed_url": embed_url,
        }

    return data

def extract_media_fallback(source_html):
    """
    Fallback extraction for cases where the mediaItems object
    cannot be matched cleanly.
    """

    data = {}

    if not source_html:
        return data

    source = source_html

    source = source.replace('\\"', '"')
    source = source.replace("\\/", "/")
    source = source.replace("\\u0026", "&")
    source = source.replace("\\u002F", "/")

    # Find video URLs directly.
    video_urls = re.findall(
        r'"videoUrl"\s*:\s*"([^"]+)"',
        source,
    )

    for video_url in video_urls:

        video_url = decode_next_string(
            video_url
        )

        if not video_url:
            continue

        # UUID is part of the video filename.
        match = re.search(
            r'/([0-9a-fA-F-]{36})\.mp4',
            video_url,
        )

        if not match:
            continue

        video_id = match.group(1)

        data.setdefault(
            video_id,
            {
                "video_id": video_id,
                "video_url": video_url,
                "thumbnail": None,
                "duration": None,
                "embed_url": (
                    f"https://downloaddirect.xyz/embed/{video_id}"
                ),
            },
        )

    # Find thumbnails and pair them using their surrounding
    # media object whenever possible.
    thumbnail_matches = re.finditer(
        r'"thumbnailUrl"\s*:\s*"([^"]+)"',
        source,
    )

    for match in thumbnail_matches:

        thumbnail = decode_next_string(
            match.group(1)
        )

        # Search nearby for videoId.
        start = max(
            0,
            match.start() - 1500,
        )

        end = min(
            len(source),
            match.end() + 1500,
        )

        nearby = source[start:end]

        video_id_match = re.search(
            r'"videoId"\s*:\s*"([^"]+)"',
            nearby,
        )

        if not video_id_match:
            continue

        video_id = video_id_match.group(1)

        if video_id in data:
            data[video_id]["thumbnail"] = thumbnail

    # Durations
    duration_matches = re.finditer(
        r'"duration"\s*:\s*(\d+(?:\.\d+)?)',
        source,
    )

    for match in duration_matches:

        start = max(
            0,
            match.start() - 1500,
        )

        end = min(
            len(source),
            match.end() + 500,
        )

        nearby = source[start:end]

        video_id_match = re.search(
            r'"videoId"\s*:\s*"([^"]+)"',
            nearby,
        )

        if not video_id_match:
            continue

        video_id = video_id_match.group(1)

        if video_id in data:
            try:
                data[video_id]["duration"] = int(
                    float(match.group(1))
                )
            except (TypeError, ValueError):
                pass

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

# Fallback if the RSC mediaItems parser didn't
# recover anything.
if not media_data:
    media_data = extract_media_fallback(
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
