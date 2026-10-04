import html as html_lib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


# ============================================================
# TEXT / URL HELPERS
# ============================================================

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


# ============================================================
# NEXT.JS MEDIA DATA
# ============================================================

def normalize_rsc_source(source_html):
    """
    Normalize the escaped strings used inside Next.js
    React Server Component payloads.
    """

    source = source_html or ""

    source = source.replace('\\"', '"')
    source = source.replace("\\/", "/")
    source = source.replace("\\u0026", "&")
    source = source.replace("\\u002F", "/")

    return source


def extract_next_video_data(source_html):
    """
    Extract Desihub video metadata from the Next.js RSC payload.

    Desihub contains objects similar to:

        "mediaItems":[
            {
                "id":"...",
                "type":"video",
                "url":"https://downloaddirect.xyz/embed/...",
                "videoId":"UUID",
                "videoUrl":"https://videos.downloaddirect.xyz/UUID.mp4",
                "thumbnailUrl":"https://images.downloaddirect.xyz/IMAGE.webp",
                "duration":123
            }
        ]
    """

    data = {}

    if not source_html:
        return data

    source = normalize_rsc_source(source_html)

    # --------------------------------------------------------
    # METHOD 1
    #
    # Match the mediaItems object itself.
    # --------------------------------------------------------

    media_pattern = re.compile(
        r'"mediaItems"\s*:\s*\[\s*\{(.*?)\}\s*\]',
        re.DOTALL,
    )

    media_blocks = media_pattern.findall(source)

    for block in media_blocks:

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

        embed_match = re.search(
            r'"url"\s*:\s*"([^"]+)"',
            block,
        )

        video_url_match = re.search(
            r'"videoUrl"\s*:\s*"([^"]+)"',
            block,
        )

        thumbnail_match = re.search(
            r'"thumbnailUrl"\s*:\s*"([^"]+)"',
            block,
        )

        duration_match = re.search(
            r'"duration"\s*:\s*(\d+(?:\.\d+)?)',
            block,
        )

        embed_url = (
            decode_next_string(
                embed_match.group(1)
            )
            if embed_match
            else None
        )

        video_url = (
            decode_next_string(
                video_url_match.group(1)
            )
            if video_url_match
            else None
        )

        thumbnail = (
            decode_next_string(
                thumbnail_match.group(1)
            )
            if thumbnail_match
            else None
        )

        duration = None

        if duration_match:
            try:
                duration = int(
                    float(
                        duration_match.group(1)
                    )
                )
            except (TypeError, ValueError):
                duration = None

        data[video_id] = {
            "video_id": video_id,
            "video_url": video_url,
            "thumbnail": thumbnail,
            "duration": duration,
            "embed_url": embed_url,
        }

    # --------------------------------------------------------
    # METHOD 2
    #
    # More tolerant fallback.
    #
    # This does NOT depend on mediaItems being matched
    # perfectly. It finds videoId and searches nearby for
    # the other fields.
    # --------------------------------------------------------

    if not data:

        video_matches = list(
            re.finditer(
                r'"videoId"\s*:\s*"([^"]+)"',
                source,
                re.DOTALL,
            )
        )

        for match in video_matches:

            video_id = clean_text(
                match.group(1)
            )

            if not video_id:
                continue

            # The fields in a mediaItems object are very close
            # together, but allow a generous window because
            # Next.js RSC serialization can vary.
            start = match.start()
            end = min(
                len(source),
                start + 4000,
            )

            block = source[start:end]

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
                r'"duration"\s*:\s*(\d+(?:\.\d+)?)',
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
                    decode_next_string(
                        video_url_match.group(1)
                    )
                    if video_url_match
                    else None
                ),
                "thumbnail": (
                    decode_next_string(
                        thumbnail_match.group(1)
                    )
                    if thumbnail_match
                    else None
                ),
                "duration": (
                    int(
                        float(
                            duration_match.group(1)
                        )
                    )
                    if duration_match
                    else None
                ),
                "embed_url": (
                    decode_next_string(
                        embed_match.group(1)
                    )
                    if embed_match
                    else None
                ),
            }

    return data


# ============================================================
# EXTRA FALLBACK MEDIA EXTRACTOR
# ============================================================

def extract_media_fallback(source_html):
    """
    Final fallback for direct video URLs.

    This is only used when the main RSC parser does not
    recover media information.
    """

    data = {}

    if not source_html:
        return data

    source = normalize_rsc_source(source_html)

    # --------------------------------------------------------
    # Direct video URLs
    # --------------------------------------------------------

    video_urls = re.findall(
        r'"videoUrl"\s*:\s*"([^"]+)"',
        source,
    )

    for raw_video_url in video_urls:

        video_url = decode_next_string(
            raw_video_url
        )

        if not video_url:
            continue

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
                    "https://downloaddirect.xyz/embed/"
                    + video_id
                ),
            },
        )

    # --------------------------------------------------------
    # Thumbnails
    # --------------------------------------------------------

    thumbnail_matches = re.finditer(
        r'"thumbnailUrl"\s*:\s*"([^"]+)"',
        source,
    )

    for match in thumbnail_matches:

        thumbnail = decode_next_string(
            match.group(1)
        )

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

    # --------------------------------------------------------
    # Durations
    # --------------------------------------------------------

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

        if video_id not in data:
            continue

        try:
            data[video_id]["duration"] = int(
                float(
                    match.group(1)
                )
            )
        except (TypeError, ValueError):
            pass

    return data


# ============================================================
# VIDEO ID
# ============================================================

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


# ============================================================
# CHANNEL
# ============================================================

def extract_channel_data(post_link, base_url):

    channel = {
        "name": None,
        "username": None,
        "url": None,
        "avatar": None,
    }

    parent = post_link.parent

    if not parent:
        return channel

    # Search current card/container.
    candidates = parent.find_all(
        "a",
        href=True,
    )

    # Search one level higher if necessary.
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

            if "@" in text:

                name, username = text.rsplit(
                    "@",
                    1,
                )

                channel["name"] = clean_text(
                    name
                )

                channel["username"] = clean_text(
                    "@" + username
                )

            else:
                channel["name"] = text

        break

    return channel


# ============================================================
# PAGINATION
# ============================================================

def extract_pagination(soup, base_url, current_page):

    next_url = None
    previous_url = None

    # --------------------------------------------------------
    # Previous page
    # --------------------------------------------------------

    if current_page > 1:

        if current_page == 2:
            previous_url = urljoin(
                base_url,
                "/feed",
            )

        else:
            previous_url = urljoin(
                base_url,
                f"/feed/page/{current_page - 1}",
            )

    # --------------------------------------------------------
    # Next page
    # --------------------------------------------------------

    # Look for an actual numbered feed page link.
    # If page N+1 exists, use it.
    expected_next = (
        "/feed"
        if current_page == 1
        else f"/feed/page/{current_page + 1}"
    )

    for link in soup.find_all(
        "a",
        href=True,
    ):

        href = link.get("href")

        if not href:
            continue

        parsed_href = urlparse(href)

        path = parsed_href.path.rstrip("/")

        # Page 1 -> /feed/page/2
        if current_page == 1:

            if path == "/feed/page/2":
                next_url = absolute_url(
                    href,
                    base_url,
                )
                break

        # Page 2+ -> /feed/page/N+1
        else:

            if path == expected_next:
                next_url = absolute_url(
                    href,
                    base_url,
                )
                break

    return {
        "next": next_url,
        "previous": previous_url,
    }


# ============================================================
# PAGE NUMBER
# ============================================================

def extract_page_number(page_url):

    match = re.search(
        r"/feed/page/(\d+)",
        page_url,
        re.IGNORECASE,
    )

    if match:
        return int(
            match.group(1)
        )

    return 1
# ============================================================
# MAIN PAGE PARSER
# ============================================================

def parse_page(source_html, page_url):

    soup = BeautifulSoup(
        source_html,
        "html.parser",
    )

    # ========================================================
    # PAGE TITLE
    # ========================================================

    title = None

    if soup.title:

        title = clean_text(
            soup.title.get_text(
                " ",
                strip=True,
            )
        )

    # ========================================================
    # NEXT.JS MEDIA DATA
    # ========================================================

    media_data = extract_next_video_data(
        source_html
    )

    # Final fallback.
    if not media_data:

        media_data = extract_media_fallback(
            source_html
        )

    # ========================================================
    # FEED POST LINKS
    # ========================================================

    post_links = []

    for link in soup.find_all(
        "a",
        href=True,
    ):

        href = link.get("href")

        if not href:
            continue

        # Match:
        #
        # /feed/something
        #
        # but NOT:
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

    # ========================================================
    # ITEMS
    # ========================================================

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

        # ----------------------------------------------------
        # TITLE
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # EMBED
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # VIDEO ID
        # ----------------------------------------------------

        video_id = extract_video_id(
            embed_url
        )

        # ----------------------------------------------------
        # MEDIA
        # ----------------------------------------------------

        media = None

        if video_id:

            media = media_data.get(
                video_id
            )

        # ----------------------------------------------------
        # CHANNEL
        # ----------------------------------------------------

        channel = extract_channel_data(
            post_link,
            page_url,
        )

        # ----------------------------------------------------
        # FINAL ITEM
        # ----------------------------------------------------

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

# ========================================================
# PAGINATION
# ========================================================

current_page = extract_page_number(
    page_url
)

pagination = extract_pagination(
    soup,
    page_url,
    current_page,
)


    # Page 2+ previous-page fallback.
    if (
        not pagination["previous"]
        and current_page > 1
    ):

        if current_page == 2:

            pagination["previous"] = (
                urljoin(
                    page_url,
                    "/feed",
                )
            )

        else:

            pagination["previous"] = (
                urljoin(
                    page_url,
                    f"/feed/page/{current_page - 1}",
                )
            )

    if not pagination["next"]:
        pagination["next"] = None

    # ========================================================
    # RESULT
    # ========================================================

    return {
        "title": title,
        "page": current_page,
        "count": len(items),
        "items": items,
        "pagination": pagination,
    }
