"""
Desihub scraper parser.

Phase 1:
- Extract Next.js RSC / Flight payloads from HTML.
- Decode self.__next_f.push([1, "..."]) payloads.
- Extract feed objects from decoded RSC data.
- Parse arbitrary-length mediaItems arrays.
- Support:
    - 1 video
    - multiple videos
    - 1 image
    - multiple images
    - mixed image + video
- Preserve the original media order.
- Normalize media into a predictable Python structure.

This phase intentionally keeps the existing public parse_page()
interface so the current FastAPI application does not break.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NEXT_F_PUSH_PATTERN = re.compile(
    r"""
    self\.__next_f\.push
    \(
        \[
            \s*1\s*,\s*
            (?P<payload>"(?:\\.|[^"\\])*")
        \]
    \)
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _safe_int(value: Any) -> Optional[int]:
    """
    Convert a value to int when possible.

    Returns None instead of raising if conversion is impossible.
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_string(value: Any) -> Optional[str]:
    """
    Return a stripped string or None.
    """
    if value is None:
        return None

    if not isinstance(value, str):
        return str(value)

    value = value.strip()

    return value or None


# ---------------------------------------------------------------------------
# Balanced JSON extraction
# ---------------------------------------------------------------------------

def _extract_balanced_json(
    text: str,
    start_index: int,
) -> Optional[str]:
    """
    Extract one complete JSON object/array beginning at start_index.

    This is intentionally brace-aware instead of using a regex.

    Why:
        mediaItems can contain any number of objects:

        [
            {...},
            {...},
            {...},
            ...
        ]

    A regex such as:

        "mediaItems"\\s*:\\s*\\[(.*?)\\]

    becomes fragile as soon as nested objects/arrays or escaped
    characters are involved.

    This scanner understands:
        {}
        []
        strings
        escaped characters inside strings
    """

    if start_index < 0 or start_index >= len(text):
        return None

    opening = text[start_index]

    if opening not in "{[":
        return None

    if opening == "{":
        closing = "}"
    else:
        closing = "]"

    depth = 0
    in_string = False
    escaped = False

    for index in range(start_index, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
                continue

            if char == "\\":
                escaped = True
                continue

            if char == '"':
                in_string = False

            continue

        if char == '"':
            in_string = True
            continue

        if char == opening:
            depth += 1
            continue

        if char == closing:
            depth -= 1

            if depth == 0:
                return text[start_index:index + 1]

    return None


def _extract_json_value_after_key(
    text: str,
    key: str,
    start_at: int = 0,
) -> Optional[Any]:
    """
    Find a JSON key and decode the JSON value immediately following it.

    Example:

        "mediaItems":[{...},{...}]

    The function finds the '[' and then uses the balanced scanner
    to extract the entire array.
    """

    key_index = text.find(key, start_at)

    if key_index == -1:
        return None

    colon_index = text.find(":", key_index + len(key))

    if colon_index == -1:
        return None

    value_start = colon_index + 1

    while value_start < len(text) and text[value_start].isspace():
        value_start += 1

    if value_start >= len(text):
        return None

    if text[value_start] not in "{[":
        return None

    raw_value = _extract_balanced_json(
        text,
        value_start,
    )

    if raw_value is None:
        return None

    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Next.js RSC / Flight extraction
# ---------------------------------------------------------------------------

def extract_next_f_payloads(html: str) -> List[str]:
    """
    Extract and decode Next.js self.__next_f.push([1, "..."]) payloads.

    Next.js pages can contain many RSC script chunks.

    Example source shape:

        <script>
        self.__next_f.push([1,"22:[...]"])
        </script>

    The returned strings are decoded versions of the second argument.

    Example:

        22:["$","$L21",...]
    """

    if not html:
        return []

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    payloads: List[str] = []

    for script in soup.find_all("script"):
        script_text = script.string

        if script_text is None:
            script_text = script.get_text()

        if not script_text:
            continue

        match = NEXT_F_PUSH_PATTERN.search(script_text)

        while match:
            encoded_payload = match.group("payload")

            try:
                decoded_payload = json.loads(
                    encoded_payload
                )
            except json.JSONDecodeError:
                decoded_payload = None

            if isinstance(decoded_payload, str):
                payloads.append(decoded_payload)

            next_start = match.end()

            next_match = NEXT_F_PUSH_PATTERN.search(
                script_text,
                next_start,
            )

            if next_match is None:
                break

            match = next_match

    return payloads


# ---------------------------------------------------------------------------
# Feed object extraction from decoded RSC payloads
# ---------------------------------------------------------------------------

def extract_rsc_feed_objects(
    html: str,
) -> List[Dict[str, Any]]:
    """
    Extract all feed objects from Next.js RSC payloads.

    The site embeds records in structures similar to:

        {
            "feed": {
                "_id": "...",
                "channelId": "...",
                "channelName": "...",
                "username": "...",
                "avatar": "...",
                "title": "...",
                "slug": "...",
                "mediaItems": [...],
                "createdAt": "..."
            },
            "index": 0
        }

    This function extracts the actual feed object.

    Important:
        We do NOT assume a specific RSC chunk number.

        We do NOT assume mediaItems has one object.

        We do NOT rely on iframe elements.

        We do NOT use a regex to parse the complete mediaItems array.
    """

    if not html:
        return []

    payloads = extract_next_f_payloads(html)

    feed_objects: List[Dict[str, Any]] = []

    seen_ids = set()
    seen_slugs = set()

    for payload in payloads:
        search_position = 0

        while True:
            feed_key_index = payload.find(
                '"feed"',
                search_position,
            )

            if feed_key_index == -1:
                break

            colon_index = payload.find(
                ":",
                feed_key_index + len('"feed"'),
            )

            if colon_index == -1:
                break

            object_start = colon_index + 1

            while (
                object_start < len(payload)
                and payload[object_start].isspace()
            ):
                object_start += 1

            if (
                object_start >= len(payload)
                or payload[object_start] != "{"
            ):
                search_position = feed_key_index + len('"feed"')
                continue

            raw_feed = _extract_balanced_json(
                payload,
                object_start,
            )

            if raw_feed is None:
                search_position = feed_key_index + len('"feed"')
                continue

            try:
                feed_object = json.loads(raw_feed)
            except json.JSONDecodeError:
                search_position = feed_key_index + len('"feed"')
                continue

            if not isinstance(feed_object, dict):
                search_position = feed_key_index + len('"feed"')
                continue

            feed_id = feed_object.get("_id")
            feed_slug = feed_object.get("slug")

            duplicate = False

            if feed_id and feed_id in seen_ids:
                duplicate = True

            if feed_slug and feed_slug in seen_slugs:
                duplicate = True

            if not duplicate:
                feed_objects.append(feed_object)

                if feed_id:
                    seen_ids.add(feed_id)

                if feed_slug:
                    seen_slugs.add(feed_slug)

            search_position = object_start + len(raw_feed)

    return feed_objects


# ---------------------------------------------------------------------------
# Media item normalization
# ---------------------------------------------------------------------------

def normalize_media_item(
    item: Any,
    position: int,
) -> Optional[Dict[str, Any]]:
    """
    Normalize one raw mediaItems entry.

    Supported source formats:

        Video:
        {
            "id": "...",
            "type": "video",
            "url": "https://downloaddirect.xyz/embed/...",
            "videoId": "...",
            "videoUrl": "https://videos.downloaddirect.xyz/....mp4",
            "thumbnailUrl": "...",
            "duration": 123
        }

        Image:
        {
            "id": "...",
            "type": "image",
            "url": "https://images.downloaddirect.xyz/....webp"
        }

    The normalized result intentionally uses snake_case names.
    """

    if not isinstance(item, dict):
        return None

    media_type = _clean_string(
        item.get("type")
    )

    if media_type:
        media_type = media_type.lower()

    media_id = _clean_string(
        item.get("id")
    )

    source_url = _clean_string(
        item.get("url")
    )

    # ---------------------------------------------------------------
    # Video
    # ---------------------------------------------------------------

    if media_type == "video":
        embed_url = source_url

        video_id = _clean_string(
            item.get("videoId")
        )

        video_url = _clean_string(
            item.get("videoUrl")
        )

        thumbnail = _clean_string(
            item.get("thumbnailUrl")
        )

        duration = _safe_int(
            item.get("duration")
        )

        return {
            "position": position,
            "type": "video",
            "id": media_id,
            "embed_url": embed_url,
            "video_id": video_id,
            "video_url": video_url,
            "thumbnail": thumbnail,
            "duration": duration,
        }

    # ---------------------------------------------------------------
    # Image
    # ---------------------------------------------------------------

    if media_type == "image":
        return {
            "position": position,
            "type": "image",
            "id": media_id,
            "url": source_url,
        }

    # ---------------------------------------------------------------
    # Unknown media type
    # ---------------------------------------------------------------

    # We do not silently throw unknown media away.
    #
    # Keeping the item allows us to inspect future source changes
    # instead of losing the media completely.

    if source_url or media_id:
        return {
            "position": position,
            "type": media_type or "unknown",
            "id": media_id,
            "url": source_url,
        }

    return None


def normalize_media_items(
    media_items: Any,
) -> List[Dict[str, Any]]:
    """
    Normalize an arbitrary-length mediaItems array.

    The original ordering is preserved.

    Example:

        [
            image,
            video,
            video,
            image
        ]

    remains:

        [
            {
                "position": 0,
                "type": "image",
                ...
            },
            {
                "position": 1,
                "type": "video",
                ...
            },
            {
                "position": 2,
                "type": "video",
                ...
            },
            {
                "position": 3,
                "type": "image",
                ...
            }
        ]
    """

    if not isinstance(media_items, list):
        return []

    normalized: List[Dict[str, Any]] = []

    for position, item in enumerate(media_items):
        media = normalize_media_item(
            item,
            position,
        )

        if media is not None:
            normalized.append(media)

    return normalized


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def get_feed_media_items(
    feed_object: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Get normalized media from one feed object.
    """

    if not isinstance(feed_object, dict):
        return []

    return normalize_media_items(
        feed_object.get("mediaItems")
    )


def get_video_media(
    feed_object: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Return only video media from a feed object.
    """

    return [
        item
        for item in get_feed_media_items(feed_object)
        if item.get("type") == "video"
    ]


def get_image_media(
    feed_object: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Return only image media from a feed object.
    """

    return [
        item
        for item in get_feed_media_items(feed_object)
        if item.get("type") == "image"
    ]


# ---------------------------------------------------------------------------
# Media classification
# ---------------------------------------------------------------------------

def classify_media(
    media_items: List[Dict[str, Any]],
) -> str:
    """
    Classify a post based on its media.

    Results:

        no media
            -> "unknown"

        one video
            -> "video"

        multiple videos
            -> "video_collection"

        one or more images and no videos
            -> "image_gallery"

        images + videos
            -> "mixed"
    """

    if not media_items:
        return "unknown"

    video_count = sum(
        1
        for item in media_items
        if item.get("type") == "video"
    )

    image_count = sum(
        1
        for item in media_items
        if item.get("type") == "image"
    )

    if video_count > 0 and image_count > 0:
        return "mixed"

    if video_count == 1:
        return "video"

    if video_count > 1:
        return "video_collection"

    if image_count > 0:
        return "image_gallery"

    return "unknown"


# ---------------------------------------------------------------------------
# Debug-friendly RSC media extraction
# ---------------------------------------------------------------------------

def extract_rsc_media(
    html: str,
) -> List[Dict[str, Any]]:
    """
    Extract every feed object's mediaItems from the page.

    This is the main Phase 1 helper.

    Output:

        [
            {
                "feed_id": "...",
                "slug": "...",
                "title": "...",
                "media": [...]
            },
            ...
        ]
    """

    feed_objects = extract_rsc_feed_objects(html)

    results: List[Dict[str, Any]] = []

    for feed in feed_objects:
        media = get_feed_media_items(feed)

        results.append(
            {
                "feed_id": _clean_string(
                    feed.get("_id")
                ),
                "slug": _clean_string(
                    feed.get("slug")
                ),
                "title": _clean_string(
                    feed.get("title")
                ),
                "media": media,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Legacy DOM helpers
# ---------------------------------------------------------------------------

def _absolute_or_original(
    url: Optional[str],
    base_url: str,
) -> Optional[str]:
    """
    Convert relative URLs into absolute URLs.

    Kept intentionally dependency-free.
    """

    if not url:
        return None

    if url.startswith("http://") or url.startswith("https://"):
        return url

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return base_url.rstrip("/") + url

    return url


def extract_slug_from_url(
    url: Optional[str],
) -> Optional[str]:
    """
    Extract the last meaningful path component from a URL.

    This helper is retained for compatibility with the previous parser.
    """

    if not url:
        return None

    clean_url = url.split("?", 1)[0]
    clean_url = clean_url.rstrip("/")

    if not clean_url:
        return None

    return clean_url.rsplit("/", 1)[-1] or None


# ---------------------------------------------------------------------------
# Existing page parser
# ---------------------------------------------------------------------------

def parse_page(
    html: str,
    base_url: str,
) -> Dict[str, Any]:
    """
    Parse a Desihub page.

    IMPORTANT:
        Phase 1 introduces the new RSC parser but does not yet replace
        the complete feed response architecture.

    The current response still exposes the legacy item structure so
    app.py remains compatible.

    The RSC media information is attached internally to each item
    through `_rsc_media`.

    Phase 2 will promote this into the public `media[]` response.
    """

    if not html:
        return {
            "title": None,
            "count": 0,
            "items": [],
            "pagination": {
                "next": None,
                "previous": None,
            },
        }

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # ---------------------------------------------------------------
    # Page title
    # ---------------------------------------------------------------

    title = None

    title_tag = soup.find("title")

    if title_tag:
        title = title_tag.get_text(
            " ",
            strip=True,
        )

    # ---------------------------------------------------------------
    # Phase 1 RSC extraction
    # ---------------------------------------------------------------

    rsc_feed_objects = extract_rsc_feed_objects(
        html
    )

    rsc_by_slug: Dict[str, Dict[str, Any]] = {}

    for feed in rsc_feed_objects:
        slug = _clean_string(
            feed.get("slug")
        )

        if slug:
            rsc_by_slug[slug] = feed

    # ---------------------------------------------------------------
    # Feed links
    # ---------------------------------------------------------------

    items: List[Dict[str, Any]] = []

    seen_slugs = set()

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        href = anchor.get("href")

        if not href:
            continue

        # Only process likely post URLs.
        #
        # Feed pagination and generic navigation are excluded.
        if "/feed" in href:
            continue

        slug = extract_slug_from_url(
            href
        )

        if not slug:
            continue

        if slug in seen_slugs:
            continue

        # A real feed post should normally correspond to one of the
        # RSC feed objects.
        feed_object = rsc_by_slug.get(slug)

        if feed_object is None:
            continue

        seen_slugs.add(slug)

        absolute_url = _absolute_or_original(
            href,
            base_url,
        )

        media_items = get_feed_media_items(
            feed_object
        )

        video_items = [
            media
            for media in media_items
            if media.get("type") == "video"
        ]

        first_video = (
            video_items[0]
            if video_items
            else None
        )

        items.append(
            {
                "title": _clean_string(
                    feed_object.get("title")
                ),
                "slug": slug,
                "url": absolute_url,

                # ---------------------------------------------------
                # Legacy single-video fields
                # ---------------------------------------------------
                "embed_url": (
                    first_video.get("embed_url")
                    if first_video
                    else None
                ),
                "video_id": (
                    first_video.get("video_id")
                    if first_video
                    else None
                ),
                "video_url": (
                    first_video.get("video_url")
                    if first_video
                    else None
                ),
                "thumbnail": (
                    first_video.get("thumbnail")
                    if first_video
                    else None
                ),
                "duration": (
                    first_video.get("duration")
                    if first_video
                    else None
                ),

                # ---------------------------------------------------
                # Phase 1 internal data
                #
                # Phase 2 will make these public API fields.
                # ---------------------------------------------------
                "_rsc_media": media_items,
                "_rsc_media_count": len(media_items),
                "_rsc_video_count": len(video_items),
                "_rsc_image_count": sum(
                    1
                    for media in media_items
                    if media.get("type") == "image"
                ),
                "_rsc_type": classify_media(
                    media_items
                ),
            }
        )

    # ---------------------------------------------------------------
    # Fallback:
    #
    # If the HTML DOM does not expose normal feed anchors but the
    # RSC payload does, still return the feed objects.
    #
    # This is useful for Next.js markup changes.
    # ---------------------------------------------------------------

    if not items and rsc_feed_objects:
        for feed_object in rsc_feed_objects:
            slug = _clean_string(
                feed_object.get("slug")
            )

            if not slug:
                continue

            media_items = get_feed_media_items(
                feed_object
            )

            video_items = [
                media
                for media in media_items
                if media.get("type") == "video"
            ]

            first_video = (
                video_items[0]
                if video_items
                else None
            )

            items.append(
                {
                    "title": _clean_string(
                        feed_object.get("title")
                    ),
                    "slug": slug,
                    "url": (
                        f"{base_url.rstrip('/')}/{slug}"
                    ),

                    "embed_url": (
                        first_video.get("embed_url")
                        if first_video
                        else None
                    ),
                    "video_id": (
                        first_video.get("video_id")
                        if first_video
                        else None
                    ),
                    "video_url": (
                        first_video.get("video_url")
                        if first_video
                        else None
                    ),
                    "thumbnail": (
                        first_video.get("thumbnail")
                        if first_video
                        else None
                    ),
                    "duration": (
                        first_video.get("duration")
                        if first_video
                        else None
                    ),

                    "_rsc_media": media_items,
                    "_rsc_media_count": len(media_items),
                    "_rsc_video_count": len(video_items),
                    "_rsc_image_count": sum(
                        1
                        for media in media_items
                        if media.get("type") == "image"
                    ),
                    "_rsc_type": classify_media(
                        media_items
                    ),
                }
            )

    # ---------------------------------------------------------------
    # Pagination
    # ---------------------------------------------------------------

    next_url = None
    previous_url = None

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        text = anchor.get_text(
            " ",
            strip=True,
        ).lower()

        href = anchor.get("href")

        if not href:
            continue

        absolute_href = _absolute_or_original(
            href,
            base_url,
        )

        if (
            "next" in text
            or "next" in anchor.get(
                "aria-label",
                ""
            ).lower()
        ):
            next_url = absolute_href

        if (
            "previous" in text
            or "prev" in text
            or "previous" in anchor.get(
                "aria-label",
                ""
            ).lower()
        ):
            previous_url = absolute_href

    return {
        "title": title,
        "count": len(items),
        "items": items,
        "pagination": {
            "next": next_url,
            "previous": previous_url,
        },
    }
