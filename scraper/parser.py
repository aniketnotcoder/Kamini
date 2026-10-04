import re
import html as html_lib
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


def clean_text(value):
    if not value:
        return None

    value = html_lib.unescape(value)
    value = re.sub(r"\s+", " ", value)

    return value.strip() or None


def absolute_url(url, base_url):
    if not url:
        return None

    return urljoin(base_url, html_lib.unescape(url))


def extract_next_video_data(source_html):
    """
    Extract video metadata embedded inside Next.js __next_f data.

    The feed source contains records like:

    "videoId":"...",
    "videoUrl":"https://videos.downloaddirect.xyz/...mp4",
    "thumbnailUrl":"https://images.downloaddirect.xyz/....webp",
    "duration":93
    """

    videos = {}

    pattern = re.compile(
        r'"videoId"\s*:\s*"([^"]+)"'
        r'.{0,1000}?'
        r'"videoUrl"\s*:\s*"([^"]+)"'
        r'.{0,1000}?'
        r'"thumbnailUrl"\s*:\s*"([^"]+)"'
        r'(?:.{0,300}?"duration"\s*:\s*(\d+))?',
        re.DOTALL
    )

    for match in pattern.finditer(source_html):
        video_id = match.group(1)
        video_url = html_lib.unescape(match.group(2))
        thumbnail_url = html_lib.unescape(match.group(3))

        duration = match.group(4)

        videos[video_id] = {
            "video_id": video_id,
            "video_url": video_url,
            "thumbnail": thumbnail_url,
            "duration": int(duration) if duration else None,
        }

    return videos


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


def parse_page(source_html, page_url):
    """
    Parse one Desihub feed page.

    Returns only actual feed posts instead of every <a>
    element on the website.
    """

    soup = BeautifulSoup(source_html, "html.parser")

    # ---------------------------------------------------------
    # PAGE METADATA
    # ---------------------------------------------------------

    title_tag = soup.find("title")

    title = clean_text(
        title_tag.get_text(" ", strip=True)
        if title_tag
        else None
    )

    # ---------------------------------------------------------
    # NEXT.JS EMBEDDED VIDEO DATA
    # ---------------------------------------------------------

    video_data = extract_next_video_data(source_html)

    # ---------------------------------------------------------
    # FIND FEED POSTS
    # ---------------------------------------------------------

    items = []

    # Feed posts are links whose href starts with /feed/
    # but NOT /feed/page/
    post_links = soup.find_all(
        "a",
        href=re.compile(r"^/feed/(?!page/)")
    )

    seen_urls = set()

    for post_link in post_links:

        post_url = post_link.get("href")

        if not post_url:
            continue

        post_url = absolute_url(
            post_url,
            page_url
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
            post_title = clean_text(
                heading.get_text(" ", strip=True)
            )
        else:
            post_title = None

        if not post_title:
            continue

        # -----------------------------------------------------
        # EMBED URL
        # -----------------------------------------------------

        iframe = post_link.find("iframe")

        embed_url = None

        if iframe:
            embed_url = iframe.get("src")

            if embed_url:
                embed_url = absolute_url(
                    embed_url,
                    page_url
                )

        # -----------------------------------------------------
        # VIDEO ID
        # -----------------------------------------------------

        video_id = extract_video_id(embed_url)

        # -----------------------------------------------------
        # VIDEO METADATA FROM NEXT.JS DATA
        # -----------------------------------------------------

        metadata = (
            video_data.get(video_id, {})
            if video_id
            else {}
        )

        video_url = metadata.get("video_url")
        thumbnail = metadata.get("thumbnail")
        duration = metadata.get("duration")

        # -----------------------------------------------------
        # CHANNEL INFORMATION
        # -----------------------------------------------------

        channel_link = post_link.find(
            "a",
            href=re.compile(r"^/channels/")
        )

        channel_name = None
        username = None
        channel_url = None
        avatar = None

        if channel_link:

            channel_url = absolute_url(
                channel_link.get("href"),
                page_url
            )

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

        # -----------------------------------------------------
        # FALLBACK THUMBNAIL
        # -----------------------------------------------------
        #
        # The normal <img> on the feed is the CHANNEL AVATAR,
        # not the video thumbnail.
        #
        # Actual video thumbnail comes from thumbnailUrl
        # inside Next.js data.
        #

        if not thumbnail:
            thumbnail = avatar

        # -----------------------------------------------------
        # ITEM
        # -----------------------------------------------------

        item = {
            "title": post_title,
            "url": post_url,

            "channel": {
                "name": channel_name,
                "username": username,
                "url": channel_url,
                "avatar": avatar,
            },

            "embed_url": embed_url,
            "video_id": video_id,
            "video_url": video_url,
            "thumbnail": thumbnail,
            "duration": duration,
        }

        items.append(item)

    # ---------------------------------------------------------
    # PAGINATION
    # ---------------------------------------------------------

    next_page = None
    previous_page = None

    pagination = soup.find(
        "nav",
        attrs={
            "aria-label": "Pagination navigation"
        }
    )

    if pagination:

        for link in pagination.find_all("a"):

            href = link.get("href")

            if not href:
                continue

            href = absolute_url(
                href,
                page_url
            )

            if "/feed/page/" in href:

                # Determine whether this is next/previous
                button = link.find("button")

                aria = (
                    button.get("aria-label")
                    if button
                    else ""
                )

                if aria == "Next Page":
                    next_page = href

                elif aria == "Previous Page":
                    previous_page = href

    # ---------------------------------------------------------
    # PAGE NUMBER
    # ---------------------------------------------------------

    page_number = 1

    match = re.search(
        r"/feed/page/(\d+)",
        page_url
    )

    if match:
        page_number = int(match.group(1))

    # ---------------------------------------------------------
    # RESULT
    # ---------------------------------------------------------

    return {
        "title": title,
        "page": page_number,

        "count": len(items),

        "items": items,

        "pagination": {
            "next": next_page,
            "previous": previous_page,
        },
    }
