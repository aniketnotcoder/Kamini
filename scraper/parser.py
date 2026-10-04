import re
import html as html_lib
from urllib.parse import urljoin

from bs4 import BeautifulSoup


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value):
    if not value:
        return None

    value = html_lib.unescape(value)
    value = re.sub(r"\s+", " ", value)

    return value.strip() or None


def absolute_url(url, base_url):
    if not url:
        return None

    url = html_lib.unescape(url)

    return urljoin(base_url, url)


# ============================================================
# NEXT.JS MEDIA EXTRACTION
# ============================================================

def extract_next_video_data(source_html):
    """
    Extract video metadata from Next.js __next_f RSC data.

    Desihub feed source contains structures similar to:

        "mediaItems":[
            {
                "id":"...",
                "type":"video",
                "url":"https://downloaddirect.xyz/embed/UUID",
                "videoId":"UUID",
                "videoUrl":"https://videos.downloaddirect.xyz/UUID.mp4",
                "thumbnailUrl":"https://images.downloaddirect.xyz/....webp",
                "duration":957
            }
        ]

    We index the data by videoId so the rendered HTML iframe
    can be matched against the actual MP4/thumbnail metadata.
    """

    videos = {}

    if not source_html:
        return videos

    # --------------------------------------------------------
    # Next.js may contain escaped JSON:
    #
    # \"videoId\":\"...\"
    #
    # or normal JSON:
    #
    # "videoId":"..."
    #
    # This regex supports both.
    # --------------------------------------------------------

    video_id_pattern = re.compile(
        r'\\?"videoId\\?"\s*:\s*\\?"([^"\\]+)\\?"',
        re.IGNORECASE
    )

    matches = list(video_id_pattern.finditer(source_html))

    for match in matches:

        video_id = match.group(1)

        # ----------------------------------------------------
        # Only inspect a local window around this media object.
        #
        # This prevents accidentally pairing one video's
        # thumbnail with another video's videoUrl.
        # ----------------------------------------------------

        start = match.start()
        end = min(
            len(source_html),
            match.end() + 2500
        )

        block = source_html[start:end]

        # ----------------------------------------------------
        # VIDEO URL
        # ----------------------------------------------------

        video_url_match = re.search(
            r'\\?"videoUrl\\?"\s*:\s*\\?"([^"\\]+)\\?"',
            block,
            re.IGNORECASE
        )

        video_url = (
            html_lib.unescape(video_url_match.group(1))
            if video_url_match
            else None
        )

        # ----------------------------------------------------
        # THUMBNAIL
        # ----------------------------------------------------

        thumbnail_match = re.search(
            r'\\?"thumbnailUrl\\?"\s*:\s*\\?"([^"\\]+)\\?"',
            block,
            re.IGNORECASE
        )

        thumbnail = (
            html_lib.unescape(thumbnail_match.group(1))
            if thumbnail_match
            else None
        )

        # ----------------------------------------------------
        # DURATION
        # ----------------------------------------------------

        duration_match = re.search(
            r'\\?"duration\\?"\s*:\s*(\d+)',
            block,
            re.IGNORECASE
        )

        duration = (
            int(duration_match.group(1))
            if duration_match
            else None
        )

        # ----------------------------------------------------
        # EMBED URL
        # ----------------------------------------------------

        embed_match = re.search(
            r'\\?"url\\?"\s*:\s*\\?"'
            r'(https?://[^"\\]*?/embed/[^"\\]+)'
            r'\\?"',
            block,
            re.IGNORECASE
        )

        embed_url = (
            html_lib.unescape(embed_match.group(1))
            if embed_match
            else None
        )

        videos[video_id] = {
            "video_id": video_id,
            "embed_url": embed_url,
            "video_url": video_url,
            "thumbnail": thumbnail,
            "duration": duration,
        }

    return videos


# ============================================================
# VIDEO ID
# ============================================================

def extract_video_id(embed_url):
    """
    Extract UUID from:

        https://downloaddirect.xyz/embed/<UUID>
    """

    if not embed_url:
        return None

    match = re.search(
        r"/embed/([a-zA-Z0-9-]+)",
        embed_url
    )

    if not match:
        return None

    return match.group(1)


# ============================================================
# CHANNEL EXTRACTION
# ============================================================

def extract_channel_data(post_link, page_url):
    """
    Extract channel information from the feed card.

    Important:

    The channel <a> is NOT inside the post <a>.

    They are siblings inside the same outer card.

    Therefore we inspect the post link's parent container.
    """

    channel_name = None
    username = None
    channel_url = None
    avatar = None

    # --------------------------------------------------------
    # Find the outer feed card.
    #
    # Usually:
    #
    # <div class="p-4 sm:p-6 ...">
    #
    #   <div>CHANNEL</div>
    #
    #   <a href="/feed/...">POST</a>
    #
    # </div>
    #
    # --------------------------------------------------------

    container = post_link.parent

    if container:

        channel_link = container.find(
            "a",
            href=re.compile(r"^/channels/")
        )

        if channel_link:

            channel_url = absolute_url(
                channel_link.get("href"),
                page_url
            )

            # ------------------------------------------------
            # Avatar
            # ------------------------------------------------

            avatar_img = channel_link.find("img")

            if avatar_img:

                avatar = (
                    avatar_img.get("src")
                    or avatar_img.get("data-src")
                )

                if avatar:
                    avatar = absolute_url(
                        avatar,
                        page_url
                    )

            # ------------------------------------------------
            # Channel name
            # ------------------------------------------------

            channel_name_tag = channel_link.find(
                "p",
                class_=re.compile(r"font-semibold")
            )

            if channel_name_tag:

                channel_name = clean_text(
                    channel_name_tag.get_text(
                        " ",
                        strip=True
                    )
                )

            # ------------------------------------------------
            # Username
            # ------------------------------------------------

            username_tag = channel_link.find(
                "p",
                class_=re.compile(r"text-sm")
            )

            if username_tag:

                username = clean_text(
                    username_tag.get_text(
                        " ",
                        strip=True
                    )
                )

                if username:
                    username = username.lstrip("@")

    return {
        "name": channel_name,
        "username": username,
        "url": channel_url,
        "avatar": avatar,
    }


# ============================================================
# PAGINATION
# ============================================================

def extract_pagination(soup, page_url):
    """
    Extract next/previous feed pages.
    """

    next_page = None
    previous_page = None

    pagination = soup.find(
        "nav",
        attrs={
            "aria-label": "Pagination navigation"
        }
    )

    if not pagination:
        return {
            "next": None,
            "previous": None,
        }

    for link in pagination.find_all("a"):

        href = link.get("href")

        if not href:
            continue

        href = absolute_url(
            href,
            page_url
        )

        if not href:
            continue

        button = link.find("button")

        aria = (
            button.get("aria-label", "")
            if button
            else ""
        )

        aria = aria.strip()

        if aria == "Next Page":
            next_page = href

        elif aria == "Previous Page":
            previous_page = href

    return {
        "next": next_page,
        "previous": previous_page,
    }


# ============================================================
# PAGE NUMBER
# ============================================================

def extract_page_number(page_url):
    """
    /feed       -> 1
    /feed/page/2 -> 2
    /feed/page/25 -> 25
    """

    if not page_url:
        return 1

    match = re.search(
        r"/feed/page/(\d+)",
        page_url
    )

    if match:
        return int(match.group(1))

    return 1


# ============================================================
# MAIN PARSER
# ============================================================

def parse_page(source_html, page_url):
    """
    Parse one Desihub feed page.

    Returns actual feed posts only.

    Example:

        /feed
        /feed/page/2
        /feed/page/3

    Each item contains:

        title
        url
        channel
        embed_url
        video_id
        video_url
        thumbnail
        duration
    """

    if not source_html:
        return {
            "title": None,
            "page": extract_page_number(page_url),
            "count": 0,
            "items": [],
            "pagination": {
                "next": None,
                "previous": None,
            },
        }

    soup = BeautifulSoup(
        source_html,
        "html.parser"
    )

    # ========================================================
    # PAGE TITLE
    # ========================================================

    title_tag = soup.find("title")

    title = clean_text(
        title_tag.get_text(
            " ",
            strip=True
        )
        if title_tag
        else None
    )

    # ========================================================
    # NEXT.JS VIDEO DATA
    # ========================================================

    video_data = extract_next_video_data(
        source_html
    )

    # ========================================================
    # FEED POST LINKS
    # ========================================================

    post_links = soup.find_all(
        "a",
        href=re.compile(
            r"^/feed/(?!page/)"
        )
    )

    items = []

    seen_urls = set()

    for post_link in post_links:

        href = post_link.get("href")

        if not href:
            continue

        post_url = absolute_url(
            href,
            page_url
        )

        if not post_url:
            continue

        # ----------------------------------------------------
        # Prevent duplicate cards
        # ----------------------------------------------------

        if post_url in seen_urls:
            continue

        seen_urls.add(post_url)

        # ====================================================
        # TITLE
        # ====================================================

        heading = post_link.find("h2")

        if heading:

            post_title = clean_text(
                heading.get_text(
                    " ",
                    strip=True
                )
            )

        else:

            post_title = clean_text(
                post_link.get_text(
                    " ",
                    strip=True
                )
            )

        if not post_title:
            continue

        # ====================================================
        # EMBED URL
        # ====================================================

        iframe = post_link.find("iframe")

        embed_url = None

        if iframe:

            embed_url = iframe.get("src")

            if embed_url:

                embed_url = absolute_url(
                    embed_url,
                    page_url
                )

        # ====================================================
        # VIDEO ID
        # ====================================================

        video_id = extract_video_id(
            embed_url
        )

        # ====================================================
        # MATCH NEXT.JS VIDEO DATA
        # ====================================================

        metadata = (
            video_data.get(video_id, {})
            if video_id
            else {}
        )

        video_url = metadata.get(
            "video_url"
        )

        thumbnail = metadata.get(
            "thumbnail"
        )

        duration = metadata.get(
            "duration"
        )

        # ====================================================
        # CHANNEL
        # ====================================================

        channel = extract_channel_data(
            post_link,
            page_url
        )

        # ====================================================
        # ITEM
        # ====================================================

        item = {
            "title": post_title,

            "url": post_url,

            "channel": channel,

            "embed_url": embed_url,

            "video_id": video_id,

            "video_url": video_url,

            "thumbnail": thumbnail,

            "duration": duration,
        }

        items.append(item)

    # ========================================================
    # PAGINATION
    # ========================================================

    pagination = extract_pagination(
        soup,
        page_url
    )

    # ========================================================
    # PAGE NUMBER
    # ========================================================

    page_number = extract_page_number(
        page_url
    )

    # ========================================================
    # RESULT
    # ========================================================

    return {
        "title": title,

        "page": page_number,

        "count": len(items),

        "items": items,

        "pagination": pagination,
    }
