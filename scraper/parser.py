"""
Desihub scraper parser.

Phase 2:
- Parse Next.js RSC / Flight payloads.
- Extract arbitrary-length mediaItems arrays.
- Support videos, images and mixed media.
- Preserve media ordering.
- Expose a clean public API structure.
- Keep legacy single-video fields for backwards compatibility.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup


# ============================================================================
# Constants
# ============================================================================

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


# ============================================================================
# Generic helpers
# ============================================================================

def _safe_int(value: Any) -> Optional[int]:
    """
    Safely convert a value to an integer.
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
    Return a cleaned string or None.
    """

    if value is None:
        return None

    if not isinstance(value, str):
        value = str(value)

    value = value.strip()

    return value or None


# ============================================================================
# Balanced JSON scanner
# ============================================================================

def _extract_balanced_json(
    text: str,
    start_index: int,
) -> Optional[str]:
    """
    Extract a complete JSON object or array starting at start_index.

    Handles:

        {}
        []
        nested objects
        nested arrays
        quoted strings
        escaped characters
    """

    if start_index < 0:
        return None

    if start_index >= len(text):
        return None

    opening = text[start_index]

    if opening == "{":
        closing = "}"
    elif opening == "[":
        closing = "]"
    else:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(
        start_index,
        len(text),
    ):
        char = text[index]

        # ---------------------------------------------------------------
        # Inside JSON string
        # ---------------------------------------------------------------

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

        # ---------------------------------------------------------------
        # Outside string
        # ---------------------------------------------------------------

        if char == '"':
            in_string = True
            continue

        if char == opening:
            depth += 1
            continue

        if char == closing:
            depth -= 1

            if depth == 0:
                return text[
                    start_index:index + 1
                ]

    return None


# ============================================================================
# Next.js RSC extraction
# ============================================================================

def extract_next_f_payloads(
    html: str,
) -> List[str]:
    """
    Extract decoded payload strings from:

        self.__next_f.push([1, "..."])
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

        search_position = 0

        while True:

            match = NEXT_F_PUSH_PATTERN.search(
                script_text,
                search_position,
            )

            if match is None:
                break

            encoded_payload = match.group(
                "payload"
            )

            try:
                decoded_payload = json.loads(
                    encoded_payload
                )
            except json.JSONDecodeError:
                decoded_payload = None

            if isinstance(
                decoded_payload,
                str,
            ):
                payloads.append(
                    decoded_payload
                )

            search_position = match.end()

    return payloads


# ============================================================================
# RSC feed extraction
# ============================================================================

def extract_rsc_feed_objects(
    html: str,
) -> List[Dict[str, Any]]:
    """
    Extract every feed object embedded in the
    Next.js RSC payload.

    The site uses structures similar to:

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
            }
        }

    No assumptions are made about the number
    of mediaItems.
    """

    if not html:
        return []

    payloads = extract_next_f_payloads(
        html
    )

    feeds: List[Dict[str, Any]] = []

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
                search_position = (
                    feed_key_index
                    + len('"feed"')
                )
                continue

            raw_feed = _extract_balanced_json(
                payload,
                object_start,
            )

            if raw_feed is None:
                search_position = (
                    feed_key_index
                    + len('"feed"')
                )
                continue

            try:
                feed_object = json.loads(
                    raw_feed
                )
            except json.JSONDecodeError:
                search_position = (
                    feed_key_index
                    + len('"feed"')
                )
                continue

            if not isinstance(
                feed_object,
                dict,
            ):
                search_position = (
                    feed_key_index
                    + len('"feed"')
                )
                continue

            feed_id = feed_object.get(
                "_id"
            )

            feed_slug = feed_object.get(
                "slug"
            )

            duplicate = False

            if feed_id and feed_id in seen_ids:
                duplicate = True

            if feed_slug and feed_slug in seen_slugs:
                duplicate = True

            if not duplicate:

                feeds.append(
                    feed_object
                )

                if feed_id:
                    seen_ids.add(
                        feed_id
                    )

                if feed_slug:
                    seen_slugs.add(
                        feed_slug
                    )

            search_position = (
                object_start
                + len(raw_feed)
            )

    return feeds


# ============================================================================
# Media normalization
# ============================================================================

def normalize_media_item(
    item: Any,
    position: int,
) -> Optional[Dict[str, Any]]:
    """
    Normalize one mediaItems object.

    Video source:

        {
            "id": "...",
            "type": "video",
            "url": "...",
            "videoId": "...",
            "videoUrl": "...",
            "thumbnailUrl": "...",
            "duration": 123
        }

    Image source:

        {
            "id": "...",
            "type": "image",
            "url": "..."
        }
    """

    if not isinstance(
        item,
        dict,
    ):
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

    # ========================================================================
    # VIDEO
    # ========================================================================

    if media_type == "video":

        return {
            "position": position,
            "type": "video",
            "id": media_id,
            "embed_url": source_url,
            "video_id": _clean_string(
                item.get("videoId")
            ),
            "video_url": _clean_string(
                item.get("videoUrl")
            ),
            "thumbnail": _clean_string(
                item.get("thumbnailUrl")
            ),
            "duration": _safe_int(
                item.get("duration")
            ),
        }

    # ========================================================================
    # IMAGE
    # ========================================================================

    if media_type == "image":

        return {
            "position": position,
            "type": "image",
            "id": media_id,
            "url": source_url,
        }

    # ========================================================================
    # UNKNOWN
    # ========================================================================

    if media_id or source_url:

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

    Original order is preserved.
    """

    if not isinstance(
        media_items,
        list,
    ):
        return []

    normalized: List[
        Dict[str, Any]
    ] = []

    for position, item in enumerate(
        media_items
    ):

        normalized_item = (
            normalize_media_item(
                item,
                position,
            )
        )

        if normalized_item is not None:

            normalized.append(
                normalized_item
            )

    return normalized


# ============================================================================
# Media classification
# ============================================================================

def classify_media(
    media_items: List[Dict[str, Any]],
) -> str:
    """
    Classify the post.

    Possible values:

        unknown
        video
        video_collection
        image_gallery
        mixed
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

    # ------------------------------------------------------------------------
    # Mixed
    # ------------------------------------------------------------------------

    if (
        video_count > 0
        and image_count > 0
    ):
        return "mixed"

    # ------------------------------------------------------------------------
    # One video
    # ------------------------------------------------------------------------

    if video_count == 1:
        return "video"

    # ------------------------------------------------------------------------
    # Multiple videos
    # ------------------------------------------------------------------------

    if video_count > 1:
        return "video_collection"

    # ------------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------------

    if image_count > 0:
        return "image_gallery"

    return "unknown"


# ============================================================================
# Feed media helpers
# ============================================================================

def get_feed_media_items(
    feed_object: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Return normalized mediaItems for a feed object.
    """

    if not isinstance(
        feed_object,
        dict,
    ):
        return []

    return normalize_media_items(
        feed_object.get(
            "mediaItems"
        )
    )


def get_video_media(
    feed_object: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Return only videos.
    """

    return [
        media
        for media in get_feed_media_items(
            feed_object
        )
        if media.get("type") == "video"
    ]


def get_image_media(
    feed_object: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Return only images.
    """

    return [
        media
        for media in get_feed_media_items(
            feed_object
        )
        if media.get("type") == "image"
    ]


# ============================================================================
# URL helpers
# ============================================================================

def _absolute_or_original(
    url: Optional[str],
    base_url: str,
) -> Optional[str]:
    """
    Convert a relative URL into an absolute URL.
    """

    if not url:
        return None

    if (
        url.startswith("http://")
        or url.startswith("https://")
    ):
        return url

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return (
            base_url.rstrip("/")
            + url
        )

    return url


def extract_slug_from_url(
    url: Optional[str],
) -> Optional[str]:
    """
    Extract the final path segment.
    """

    if not url:
        return None

    clean_url = url.split(
        "?",
        1,
    )[0]

    clean_url = clean_url.rstrip(
        "/"
    )

    if not clean_url:
        return None

    return (
        clean_url.rsplit(
            "/",
            1,
        )[-1]
        or None
    )


# ============================================================================
# Public API item builder
# ============================================================================

def build_public_item(
    feed_object: Dict[str, Any],
    base_url: str,
) -> Dict[str, Any]:
    """
    Convert one raw RSC feed object into the
    clean public API representation.
    """

    media = get_feed_media_items(
        feed_object
    )

    video_media = [
        item
        for item in media
        if item.get("type") == "video"
    ]

    image_media = [
        item
        for item in media
        if item.get("type") == "image"
    ]

    first_video = (
        video_media[0]
        if video_media
        else None
    )

    slug = _clean_string(
        feed_object.get("slug")
    )

    # ========================================================================
    # Public response
    # ========================================================================

    return {
        # --------------------------------------------------------------------
        # Basic post information
        # --------------------------------------------------------------------

        "title": _clean_string(
            feed_object.get("title")
        ),

        "slug": slug,

        "url": (
            f"{base_url.rstrip('/')}/{slug}"
            if slug
            else None
        ),

        # --------------------------------------------------------------------
        # Media classification
        # --------------------------------------------------------------------

        "type": classify_media(
            media
        ),

        "media_count": len(
            media
        ),

        "video_count": len(
            video_media
        ),

        "image_count": len(
            image_media
        ),

        # --------------------------------------------------------------------
        # Universal media array
        # --------------------------------------------------------------------

        "media": media,

        # --------------------------------------------------------------------
        # Legacy single-video fields
        #
        # These always represent the FIRST video.
        #
        # This keeps old clients working while the new media[]
        # structure becomes the proper API.
        # --------------------------------------------------------------------

        "embed_url": (
            first_video.get(
                "embed_url"
            )
            if first_video
            else None
        ),

        "video_id": (
            first_video.get(
                "video_id"
            )
            if first_video
            else None
        ),

        "video_url": (
            first_video.get(
                "video_url"
            )
            if first_video
            else None
        ),

        "thumbnail": (
            first_video.get(
                "thumbnail"
            )
            if first_video
            else None
        ),

        "duration": (
            first_video.get(
                "duration"
            )
            if first_video
            else None
        ),
    }


# ============================================================================
# Page parser
# ============================================================================

def parse_page(
    html: str,
    base_url: str,
) -> Dict[str, Any]:
    """
    Parse a Desihub feed page.

    Phase 2 public response:
        - media
        - media_count
        - video_count
        - image_count
        - type

    Legacy video fields are preserved.
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

    # ========================================================================
    # Page title
    # ========================================================================

    title = None

    title_tag = soup.find(
        "title"
    )

    if title_tag:

        title = title_tag.get_text(
            " ",
            strip=True,
        )

    # ========================================================================
    # RSC feed objects
    # ========================================================================

    feed_objects = (
        extract_rsc_feed_objects(
            html
        )
    )

    # ========================================================================
    # Build lookup by slug
    # ========================================================================

    feeds_by_slug: Dict[
        str,
        Dict[str, Any],
    ] = {}

    for feed in feed_objects:

        slug = _clean_string(
            feed.get("slug")
        )

        if slug:

            feeds_by_slug[
                slug
            ] = feed

    # ========================================================================
    # Detect feed post order from DOM
    # ========================================================================

    items: List[
        Dict[str, Any]
    ] = []

    seen_slugs = set()

    for anchor in soup.find_all(
        "a",
        href=True,
    ):

        href = anchor.get(
            "href"
        )

        if not href:
            continue

        # --------------------------------------------------------------------
        # Ignore feed navigation
        # --------------------------------------------------------------------

        if "/feed" in href:
            continue

        slug = extract_slug_from_url(
            href
        )

        if not slug:
            continue

        if slug in seen_slugs:
            continue

        # --------------------------------------------------------------------
        # Match against RSC feed object
        # --------------------------------------------------------------------

        feed_object = feeds_by_slug.get(
            slug
        )

        if feed_object is None:
            continue

        seen_slugs.add(
            slug
        )

        item = build_public_item(
            feed_object,
            base_url,
        )

        # Preserve the actual
        # discovered URL.
        item["url"] = (
            _absolute_or_original(
                href,
                base_url,
            )
        )

        items.append(
            item
        )

    # ========================================================================
    # RSC fallback
    # ========================================================================

    if not items and feed_objects:

        for feed_object in feed_objects:

            slug = _clean_string(
                feed_object.get(
                    "slug"
                )
            )

            if not slug:
                continue

            if slug in seen_slugs:
                continue

            seen_slugs.add(
                slug
            )

            items.append(
                build_public_item(
                    feed_object,
                    base_url,
                )
            )

    # ========================================================================
    # Pagination
    # ========================================================================

    next_url = None
    previous_url = None

    for anchor in soup.find_all(
        "a",
        href=True,
    ):

        href = anchor.get(
            "href"
        )

        if not href:
            continue

        text = anchor.get_text(
            " ",
            strip=True,
        ).lower()

        aria_label = anchor.get(
            "aria-label",
            "",
        ).lower()

        absolute_href = (
            _absolute_or_original(
                href,
                base_url,
            )
        )

        # --------------------------------------------------------------------
        # Next
        # --------------------------------------------------------------------

        if (
            "next" in text
            or "next" in aria_label
        ):
            next_url = absolute_href

        # --------------------------------------------------------------------
        # Previous
        # --------------------------------------------------------------------

        if (
            "previous" in text
            or "prev" in text
            or "previous" in aria_label
            or "prev" in aria_label
        ):
            previous_url = absolute_href

    # ========================================================================
    # Final response
    # ========================================================================

    return {
        "title": title,
        "count": len(items),
        "items": items,
        "pagination": {
            "next": next_url,
            "previous": previous_url,
        },
    }
