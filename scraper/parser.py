"""
Desihub parser.

This module intentionally does not persist anything.  It fetches one HTML page,
extracts the server-rendered feed objects from the Next.js Flight/RSC payload,
normalizes media, and returns JSON-safe dictionaries.

Important implementation detail
--------------------------------
Next.js embeds Flight data in multiple self.__next_f.push([1, "..."]) chunks.
A large Flight row can be split in the middle of a JSON object, so individual
chunks must never be parsed as complete rows.  We first JSON-decode every type-1
chunk and concatenate them into the Flight stream.  Feed objects are then found
by the literal `"feed":` property and balanced-brace extraction.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

NEXT_F_PUSH_PATTERN = re.compile(r"self\.__next_f\.push\s*\(", re.DOTALL)

FEED_KEY_PATTERN = re.compile(r'"feed"\s*:\s*')

SLUG_CLEAN_PATTERN = re.compile(r"[^a-z0-9]+")

PAGE_PATH_PATTERN = re.compile(r"/feed/page/(\d+)(?:/)?$", re.IGNORECASE)

MEDIA_TYPES = {"video", "image"}


# -----------------------------------------------------------------------------
# Basic value helpers
# -----------------------------------------------------------------------------


def _clean_string(value: Any) -> Optional[str]:
    """Return a stripped string, or None for empty/non-string values."""
    if value is None:
        return None

    if isinstance(value, str):
        value = html_lib.unescape(value).strip()
        return value or None

    return None


def _safe_int(value: Any) -> Optional[int]:
    """Convert a numeric-ish value to int without raising."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value)

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    return None


def _safe_float(value: Any) -> Optional[float]:
    """Convert a numeric-ish value to float without raising."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return None


def _first_non_empty(*values: Any) -> Any:
    """Return the first non-empty value."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict, tuple, set)) and not value:
            continue
        return value
    return None


def _dedupe_preserve_order(values: Iterable[Any]) -> List[Any]:
    """Deduplicate hashable values while preserving order."""
    result: List[Any] = []
    seen = set()

    for value in values:
        try:
            key = value if isinstance(value, (str, int, float, bool, type(None))) else json.dumps(value, sort_keys=True, default=str)
        except Exception:
            key = repr(value)

        if key in seen:
            continue

        seen.add(key)
        result.append(value)

    return result


# -----------------------------------------------------------------------------
# URL / slug helpers
# -----------------------------------------------------------------------------


def normalize_slug(value: Any) -> Optional[str]:
    """Normalize a post slug for exact comparison."""
    value = _clean_string(value)
    if not value:
        return None

    value = unquote(value)
    value = value.strip()

    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        value = parsed.path

    value = value.strip("/")

    if value.startswith("feed/"):
        value = value[5:]

    if value.startswith("feed/page/"):
        return None

    value = value.strip("/")
    return value or None


def slug_key(value: Any) -> str:
    """Return a comparison-friendly slug key."""
    slug = normalize_slug(value)
    if not slug:
        return ""
    return SLUG_CLEAN_PATTERN.sub("-", slug.lower()).strip("-")


def absolute_url(value: Any, base_url: str) -> Optional[str]:
    """Turn a relative URL into an absolute URL."""
    value = _clean_string(value)
    if not value:
        return None

    if value.startswith("//"):
        scheme = urlparse(base_url).scheme or "https"
        return f"{scheme}:{value}"

    return urljoin(base_url.rstrip("/") + "/", value)


def extract_page_number_from_url(url: Any) -> Optional[int]:
    """Extract /feed/page/N from a URL/path."""
    value = _clean_string(url)
    if not value:
        return None

    parsed = urlparse(value)
    path = parsed.path or value
    match = PAGE_PATH_PATTERN.search(path.rstrip("/"))
    if not match:
        return None

    return _safe_int(match.group(1))


def page_path_for_number(page: int) -> str:
    """Return the upstream feed path for a page number."""
    page = max(1, int(page))
    return "/feed" if page == 1 else f"/feed/page/{page}"


# -----------------------------------------------------------------------------
# Next.js Flight extraction
# -----------------------------------------------------------------------------


def _decode_js_string(raw: str) -> Optional[str]:
    """Decode a JSON/JavaScript-style quoted string."""
    raw = raw.strip()
    if not raw:
        return None

    try:
        value = json.loads(raw)
        return value if isinstance(value, str) else None
    except Exception:
        return None


def _extract_balanced_js_value(text: str, start: int) -> Optional[str]:
    """
    Extract one balanced JavaScript array/object expression.

    The scanner understands quoted strings and escaped characters, so a `]` or
    `}` inside a Flight string does not terminate the expression early.
    """
    if start < 0 or start >= len(text):
        return None

    opening = text[start]
    if opening not in "[{":
        return None

    stack = ["]" if opening == "[" else "}"]
    in_string = False
    escaped = False

    for index in range(start + 1, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == "[":
            stack.append("]")
            continue

        if char == "{":
            stack.append("}")
            continue

        if char in ("]", "}"):
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return text[start:index + 1]

    return None


def extract_next_f_segments(html: str) -> List[Tuple[int, str]]:
    """
    Extract `(tag, decoded_payload)` from Next.js self.__next_f.push calls.

    We deliberately scan each push expression instead of using a regex that
    assumes the payload ends at the first `])`.  Flight payload strings can
    themselves contain brackets, quotes, and very large JSON objects, and
    Next.js may split those objects across many script tags.
    """
    if not html:
        return []

    segments: List[Tuple[int, str]] = []

    for match in NEXT_F_PUSH_PATTERN.finditer(html):
        cursor = match.end()

        # Skip whitespace between `push(` and the array expression.
        while cursor < len(html) and html[cursor].isspace():
            cursor += 1

        if cursor >= len(html) or html[cursor] != "[":
            continue

        raw_array = _extract_balanced_js_value(html, cursor)
        if raw_array is None:
            continue

        try:
            parsed = json.loads(raw_array)
        except Exception:
            continue

        if not isinstance(parsed, list) or not parsed:
            continue

        tag = _safe_int(parsed[0])
        if tag is None:
            continue

        payload = ""
        if len(parsed) > 1 and isinstance(parsed[1], str):
            payload = parsed[1]

        segments.append((tag, payload))

    return segments

def extract_next_f_payloads(html: str) -> List[str]:
    """Return decoded type-1 Flight payload chunks in source order."""
    return [payload for tag, payload in extract_next_f_segments(html) if tag == 1]


def _flight_stream_from_segments(segments: Sequence[Tuple[int, str]]) -> str:
    """
    Reconstruct the type-1 Flight stream.

    Next.js can split a single Flight row/object across multiple script tags.
    Concatenation is therefore required before searching/parsing JSON objects.
    """
    # Type 1 contains the actual Flight data. Type 0 is bootstrap metadata; it
    # is not a signal to discard already received type-1 chunks. In particular,
    # discarding earlier chunks can destroy a feed object whose JSON is split
    # across script tags.
    return "".join(payload for tag, payload in segments if tag == 1)


def combined_rsc_payload(html: str) -> str:
    """Return the reconstructed Flight/RSC text stream."""
    return _flight_stream_from_segments(extract_next_f_segments(html))


# -----------------------------------------------------------------------------
# Balanced JSON extraction
# -----------------------------------------------------------------------------


def _extract_balanced_json(text: str, start: int) -> Optional[str]:
    """
    Extract one balanced JSON object/array beginning at `start`.

    Braces inside JSON strings are ignored. Escaped quotes are handled.
    """
    if start < 0 or start >= len(text):
        return None

    opening = text[start]
    if opening not in "[{":
        return None

    closing = "]" if opening == "[" else "}"
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
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
                return text[start : index + 1]

    return None


def _parse_json_at(text: str, start: int) -> Any:
    """Parse a JSON object/array starting at a known position."""
    raw = _extract_balanced_json(text, start)
    if raw is None:
        return None

    try:
        return json.loads(raw)
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Feed-object extraction
# -----------------------------------------------------------------------------


def _looks_like_feed_object(value: Any) -> bool:
    """Check whether a dictionary looks like a Desihub feed object."""
    if not isinstance(value, dict):
        return False

    slug = value.get("slug")
    title = value.get("title")
    media_items = value.get("mediaItems")

    if not isinstance(slug, str) or not slug.strip():
        return False

    if not isinstance(title, str) or not title.strip():
        return False

    if not isinstance(media_items, list):
        return False

    return True


def _feed_object_identity(value: Dict[str, Any]) -> str:
    """Build a stable-ish deduplication key."""
    object_id = _clean_string(value.get("_id"))
    slug = normalize_slug(value.get("slug")) or ""
    return object_id or slug


def _extract_feed_objects_direct(stream: str) -> List[Dict[str, Any]]:
    """
    Extract feed objects directly from every decoded `"feed": {...}` occurrence.

    This is the primary extractor.  It intentionally does not depend on Flight
    row boundaries or `$Lxx` reference resolution.  That matters because a
    large object can be split across multiple self.__next_f.push chunks.
    """
    if not stream:
        return []

    results: List[Dict[str, Any]] = []
    seen = set()
    search_from = 0

    while True:
        match = FEED_KEY_PATTERN.search(stream, search_from)
        if match is None:
            break

        object_start = match.end()
        while object_start < len(stream) and stream[object_start].isspace():
            object_start += 1

        if object_start >= len(stream) or stream[object_start] != "{":
            search_from = match.end()
            continue

        raw = _extract_balanced_json(stream, object_start)
        if raw is None:
            # Do not get stuck if malformed text contains another `feed` key.
            search_from = match.end()
            continue

        try:
            value = json.loads(raw)
        except Exception:
            search_from = object_start + 1
            continue

        if _looks_like_feed_object(value):
            identity = _feed_object_identity(value)
            if identity not in seen:
                seen.add(identity)
                results.append(value)

        search_from = object_start + len(raw)

    return results


def _walk_json(value: Any) -> Iterator[Any]:
    """Yield every nested list/dict value recursively."""
    yield value

    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _extract_feed_objects_from_decoded_json(stream: str) -> List[Dict[str, Any]]:
    """
    Secondary fallback for streams where a feed object is already represented
    as a complete JSON value inside a Flight row.
    """
    results: List[Dict[str, Any]] = []
    seen = set()

    # Flight rows are normally newline-delimited.  Parsing each complete row is
    # useful as a fallback but is deliberately not the primary strategy.
    for line in stream.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue

        _, raw_value = line.split(":", 1)
        raw_value = raw_value.strip()

        if not raw_value or raw_value[0] not in "[{":
            continue

        parsed = _parse_json_at(raw_value, 0)
        if parsed is None:
            continue

        for value in _walk_json(parsed):
            if not _looks_like_feed_object(value):
                continue

            identity = _feed_object_identity(value)
            if identity in seen:
                continue

            seen.add(identity)
            results.append(value)

    return results


def extract_rsc_feed_objects(html: str) -> List[Dict[str, Any]]:
    """
    Extract all Desihub feed objects from an HTML page.

    Primary path:
        HTML -> __next_f segments -> decoded Flight stream -> `feed` objects.

    Fallback path:
        Complete Flight rows -> recursively locate feed-shaped dictionaries.
    """
    stream = combined_rsc_payload(html)

    direct = _extract_feed_objects_direct(stream)
    if direct:
        return direct

    return _extract_feed_objects_from_decoded_json(stream)


def _extract_feed_object_near_slug(stream: str, requested_slug: str) -> Optional[Dict[str, Any]]:
    """
    Recover one feed object when the normal `\"feed\": {...}` scan misses it.

    This is specifically for individual Next.js pages where the requested slug
    can appear inside a streamed Flight value that is not exposed as a clean
    standalone row. We locate the exact slug in the reconstructed stream, walk
    backwards to the nearest feed property, then use balanced JSON extraction.
    """
    wanted = normalize_slug(requested_slug)
    wanted_key = slug_key(wanted)
    if not wanted or not wanted_key or not stream:
        return None

    escaped_slug = json.dumps(wanted)[1:-1]
    slug_patterns = [
        f'\"slug\":\"{escaped_slug}\"',
        f'"slug": "{escaped_slug}"',
    ]

    slug_positions: List[int] = []
    for pattern in slug_patterns:
        start = 0
        while True:
            pos = stream.find(pattern, start)
            if pos < 0:
                break
            slug_positions.append(pos)
            start = pos + len(pattern)

    if not slug_positions:
        # Defensive fallback for escaped JSON fragments.
        raw_pos = stream.find(wanted)
        if raw_pos >= 0:
            slug_positions.append(raw_pos)

    for slug_pos in sorted(set(slug_positions)):
        feed_matches = list(FEED_KEY_PATTERN.finditer(stream, 0, slug_pos))
        if not feed_matches:
            continue

        feed_match = feed_matches[-1]
        object_start = feed_match.end()
        while object_start < len(stream) and stream[object_start].isspace():
            object_start += 1

        if object_start >= len(stream) or stream[object_start] != "{":
            continue

        raw = _extract_balanced_json(stream, object_start)
        if raw is None:
            continue

        try:
            value = json.loads(raw)
        except Exception:
            continue

        if _looks_like_feed_object(value) and slug_key(value.get("slug")) == wanted_key:
            return value

    return None



def _extract_post_object_from_stream_by_slug(stream: str, requested_slug: str) -> Optional[Dict[str, Any]]:
    """
    Last RSC fallback for an individual post page.

    If the page serializes the requested slug and its `mediaItems` but the
    surrounding Flight object cannot be recovered as a normal `feed` object,
    reconstruct the minimum feed-shaped dictionary directly from those fields.
    This still requires an exact slug match and never selects another post.
    """
    wanted = normalize_slug(requested_slug)
    wanted_key = slug_key(wanted)
    if not wanted or not wanted_key or not stream:
        return None

    slug_pattern = re.compile(r'"slug"\s*:\s*"(?P<slug>(?:\\.|[^"\\])*)"')

    for match in slug_pattern.finditer(stream):
        candidate = _decode_json_fragment(match.group("slug"))
        if not candidate or slug_key(candidate) != wanted_key:
            continue

        start_after_slug = match.end()
        media_match = re.search(r'"mediaItems"\s*:\s*\[', stream[start_after_slug:])
        if media_match is None:
            continue

        media_start = start_after_slug + media_match.end() - 1
        raw_media = _extract_balanced_json(stream, media_start)
        if raw_media is None:
            continue

        try:
            media_items = json.loads(raw_media)
        except Exception:
            continue

        if not isinstance(media_items, list):
            continue

        title = None
        title_matches = list(re.finditer(r'"title"\s*:\s*"(?P<title>(?:\\.|[^"\\])*)"', stream, 0, match.start()))
        if title_matches:
            title = _decode_json_fragment(title_matches[-1].group("title"))

        object_id = None
        id_matches = list(re.finditer(r'"_id"\s*:\s*"(?P<id>(?:\\.|[^"\\])*)"', stream, 0, match.start()))
        if id_matches:
            object_id = _decode_json_fragment(id_matches[-1].group("id"))

        return {
            "_id": object_id,
            "title": title or wanted,
            "slug": candidate,
            "mediaItems": media_items,
        }

    return None

def find_feed_object_by_slug(
    feed_objects: Sequence[Dict[str, Any]],
    requested_slug: str,
) -> Optional[Dict[str, Any]]:
    """Find a feed object using exact normalized slug matching first."""
    wanted = normalize_slug(requested_slug)
    wanted_key = slug_key(wanted)

    if not wanted:
        return None

    for feed in feed_objects:
        candidate = normalize_slug(feed.get("slug"))
        if candidate == wanted:
            return feed

    # A defensive comparison for URL-decoded/HTML-normalized slug differences.
    for feed in feed_objects:
        candidate_key = slug_key(feed.get("slug"))
        if candidate_key and candidate_key == wanted_key:
            return feed

    return None


# -----------------------------------------------------------------------------
# Media normalization
# -----------------------------------------------------------------------------


def _normalize_video_media(item: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    """Normalize one raw video media item."""
    embed_url = absolute_url(
        _first_non_empty(item.get("url"), item.get("embedUrl"), item.get("embed_url")),
        base_url,
    )

    video_url = absolute_url(
        _first_non_empty(item.get("videoUrl"), item.get("video_url"), item.get("src")),
        base_url,
    )

    thumbnail = absolute_url(
        _first_non_empty(
            item.get("thumbnailUrl"),
            item.get("thumbnail"),
            item.get("poster"),
        ),
        base_url,
    )

    video_id = _clean_string(
        _first_non_empty(
            item.get("videoId"),
            item.get("video_id"),
            item.get("id"),
        )
    )

    duration = _safe_int(item.get("duration"))

    result: Dict[str, Any] = {
        "type": "video",
        "id": _clean_string(item.get("id")),
        "video_id": video_id,
        "embed_url": embed_url,
        "video_url": video_url,
        "thumbnail": thumbnail,
        "duration": duration,
    }

    # Remove nulls only for optional compatibility fields.  `type` remains.
    return {key: value for key, value in result.items() if value is not None}


def _normalize_image_media(item: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    """Normalize one raw image media item."""
    image_url = absolute_url(
        _first_non_empty(
            item.get("url"),
            item.get("imageUrl"),
            item.get("image_url"),
            item.get("src"),
        ),
        base_url,
    )

    result: Dict[str, Any] = {
        "type": "image",
        "id": _clean_string(item.get("id")),
        "image_url": image_url,
    }

    return {key: value for key, value in result.items() if value is not None}


def normalize_media_item(item: Any, base_url: str) -> Optional[Dict[str, Any]]:
    """Normalize one media item while preserving source order."""
    if not isinstance(item, dict):
        return None

    raw_type = _clean_string(item.get("type"))
    raw_type = raw_type.lower() if raw_type else ""

    if raw_type == "video":
        return _normalize_video_media(item, base_url)

    if raw_type == "image":
        return _normalize_image_media(item, base_url)

    # Defensive inference for future upstream shape changes.
    if _first_non_empty(
        item.get("videoUrl"),
        item.get("video_url"),
        item.get("videoId"),
        item.get("embedUrl"),
    ):
        return _normalize_video_media(item, base_url)

    if _first_non_empty(
        item.get("imageUrl"),
        item.get("image_url"),
        item.get("src"),
    ):
        return _normalize_image_media(item, base_url)

    if _first_non_empty(item.get("url")):
        url = _clean_string(item.get("url")) or ""
        lowered = url.lower()
        if any(ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
            return _normalize_image_media(item, base_url)

        return _normalize_video_media(item, base_url)

    return None


def get_feed_media_items(feed_object: Dict[str, Any], base_url: str) -> List[Dict[str, Any]]:
    """Normalize feed_object.mediaItems preserving original order."""
    raw_items = feed_object.get("mediaItems")
    if not isinstance(raw_items, list):
        return []

    result: List[Dict[str, Any]] = []

    for raw_item in raw_items:
        normalized = normalize_media_item(raw_item, base_url)
        if normalized is not None:
            result.append(normalized)

    return result


def get_video_media(media: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return video media in original order."""
    return [item for item in media if item.get("type") == "video"]


def get_image_media(media: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return image media in original order."""
    return [item for item in media if item.get("type") == "image"]


def classify_media(media: Sequence[Dict[str, Any]]) -> str:
    """Classify a post using the Phase 2 response contract."""
    video_count = len(get_video_media(media))
    image_count = len(get_image_media(media))

    if video_count == 0 and image_count == 0:
        return "unknown"

    if video_count == 1 and image_count == 0:
        return "video"

    if video_count > 1 and image_count == 0:
        return "video_collection"

    if video_count == 0 and image_count > 0:
        return "image_gallery"

    return "mixed"


def extract_rsc_media(feed_object: Dict[str, Any], base_url: str) -> List[Dict[str, Any]]:
    """Compatibility wrapper for older code."""
    return get_feed_media_items(feed_object, base_url)


# -----------------------------------------------------------------------------
# Public item builder
# -----------------------------------------------------------------------------


def _feed_url(feed_object: Dict[str, Any], base_url: str) -> Optional[str]:
    """Build the upstream public post URL."""
    slug = normalize_slug(feed_object.get("slug"))
    if not slug:
        return None

    return f"{base_url.rstrip('/')}/feed/{slug}"


def build_public_item(feed_object: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    """
    Convert one raw Desihub feed object to the public API item.

    Legacy first-video fields are kept for compatibility while `media` contains
    every media item in original order.
    """
    media = get_feed_media_items(feed_object, base_url)
    videos = get_video_media(media)
    images = get_image_media(media)
    first_video = videos[0] if videos else None

    slug = normalize_slug(feed_object.get("slug"))

    item: Dict[str, Any] = {
        "title": _clean_string(feed_object.get("title")),
        "slug": slug,
        "url": _feed_url(feed_object, base_url),
        "type": classify_media(media),
        "media_count": len(media),
        "video_count": len(videos),
        "image_count": len(images),
        "media": media,
        "embed_url": first_video.get("embed_url") if first_video else None,
        "video_id": first_video.get("video_id") if first_video else None,
        "video_url": first_video.get("video_url") if first_video else None,
        "thumbnail": first_video.get("thumbnail") if first_video else None,
        "duration": first_video.get("duration") if first_video else None,
    }

    # Metadata fields are useful on individual post responses and are safe to
    # expose on feed items too.  They are kept null-free for cleaner JSON.
    optional_metadata = {
        "id": _clean_string(_first_non_empty(feed_object.get("_id"), feed_object.get("id"))),
        "channel_id": _clean_string(
            _first_non_empty(feed_object.get("channelId"), feed_object.get("channel_id"))
        ),
        "channel_name": _clean_string(
            _first_non_empty(feed_object.get("channelName"), feed_object.get("channel_name"))
        ),
        "username": _clean_string(feed_object.get("username")),
        "avatar": absolute_url(feed_object.get("avatar"), base_url),
        "created_at": _clean_string(
            _first_non_empty(feed_object.get("createdAt"), feed_object.get("created_at"))
        ),
    }

    item.update(optional_metadata)

    return item


# -----------------------------------------------------------------------------
# DOM extraction helpers
# -----------------------------------------------------------------------------


def _soup(html: str) -> BeautifulSoup:
    """Create a BeautifulSoup document."""
    return BeautifulSoup(html or "", "html.parser")


def _anchor_label(anchor: Any) -> str:
    """Return useful text/ARIA label for an anchor."""
    aria = _clean_string(anchor.get("aria-label"))
    text = _clean_string(anchor.get_text(" ", strip=True))
    title = _clean_string(anchor.get("title"))
    return _first_non_empty(aria, text, title) or ""


def _is_pagination_anchor(anchor: Any) -> bool:
    """Return True for anchors that look like pagination controls."""
    href = _clean_string(anchor.get("href")) or ""
    label = _anchor_label(anchor).lower()

    if extract_page_number_from_url(href) is not None:
        return True

    if any(word in label for word in ("next", "previous", "prev", "pagination")):
        return True

    return False


def _collect_dom_pagination(html: str, current_page: Optional[int] = None) -> Dict[str, Optional[str]]:
    """
    Read pagination from rendered HTML.

    This remains a fallback because the site also stores pagination in RSC.
    """
    soup = _soup(html)
    navs = soup.find_all("nav")

    candidates = []
    for nav in navs:
        aria = (_clean_string(nav.get("aria-label")) or "").lower()
        if "pagination" in aria:
            candidates.append(nav)

    if not candidates:
        candidates = navs

    previous: Optional[str] = None
    next_url: Optional[str] = None

    for nav in candidates:
        for anchor in nav.find_all("a", href=True):
            if not _is_pagination_anchor(anchor):
                continue

            href = _clean_string(anchor.get("href"))
            if not href:
                continue

            label = _anchor_label(anchor).lower()
            target_page = extract_page_number_from_url(href)

            if any(word in label for word in ("previous", "prev")):
                previous = href
                continue

            if "next" in label:
                next_url = href
                continue

            # If labels are icons only, infer direction from page number.
            if current_page is not None and target_page is not None:
                if target_page < current_page:
                    previous = href
                elif target_page > current_page:
                    next_url = href

    return {"next": next_url, "previous": previous}


# -----------------------------------------------------------------------------
# RSC pagination extraction
# -----------------------------------------------------------------------------


def _collect_rsc_pagination_from_stream(
    stream: str,
    current_page: Optional[int] = None,
) -> Dict[str, Optional[str]]:
    """
    Extract pagination hrefs from the reconstructed Flight stream.

    The important part is that this receives the *combined* stream, so a
    pagination row split across multiple 2 KB Flight chunks is reconstructed
    before searching it.
    """
    if not stream:
        return {"next": None, "previous": None}

    next_url: Optional[str] = None
    previous_url: Optional[str] = None

    # Look around explicit aria-label + href pairs first.
    pair_pattern = re.compile(
        r'"href"\s*:\s*"(?P<href>[^"\\]*(?:\\.[^"\\]*)*)"[^\n\r]{0,700}?"aria-label"\s*:\s*"(?P<label>[^"\\]*(?:\\.[^"\\]*)*)"',
        re.DOTALL,
    )

    for match in pair_pattern.finditer(stream):
        href = _decode_json_fragment(match.group("href"))
        label = _decode_json_fragment(match.group("label"))
        if not href or not label:
            continue

        label_lower = label.lower()
        if "previous" in label_lower or "prev" in label_lower:
            previous_url = href
        elif "next" in label_lower:
            next_url = href

    # Finally, collect every feed/page/N path from RSC and infer direction from
    # the requested/current page. This is the most tolerant fallback.
    page_paths: List[Tuple[int, str]] = []
    href_pattern = re.compile(r'"href"\s*:\s*"(?P<href>[^"\\]*(?:\\.[^"\\]*)*)"')

    for match in href_pattern.finditer(stream):
        href = _decode_json_fragment(match.group("href"))
        if not href:
            continue

        page_number = extract_page_number_from_url(href)
        if page_number is None:
            continue

        page_paths.append((page_number, href))

    if current_page is not None:
        lower_candidates = [item for item in page_paths if item[0] < current_page]
        higher_candidates = [item for item in page_paths if item[0] > current_page]

        if previous_url is None and lower_candidates:
            previous_url = max(lower_candidates, key=lambda item: item[0])[1]

        if next_url is None and higher_candidates:
            next_url = min(higher_candidates, key=lambda item: item[0])[1]

    return {"next": next_url, "previous": previous_url}


def _decode_json_fragment(value: str) -> Optional[str]:
    """Decode an escaped JSON string fragment captured by regex."""
    try:
        decoded = json.loads(f'"{value}"')
        return decoded if isinstance(decoded, str) else None
    except Exception:
        return value.replace('\\"', '"').replace("\\\\", "\\")


def _collect_rsc_pagination(html: str, current_page: Optional[int] = None) -> Dict[str, Optional[str]]:
    """Extract pagination from the reconstructed RSC stream."""
    return _collect_rsc_pagination_from_stream(
        combined_rsc_payload(html),
        current_page=current_page,
    )


def extract_pagination(html: str, current_page: Optional[int] = None) -> Dict[str, Optional[str]]:
    """
    Extract upstream pagination.

    RSC is preferred because it survives chunk splitting and icon-only controls.
    DOM is used as a fallback when RSC has no usable result.
    """
    rsc = _collect_rsc_pagination(html, current_page=current_page)
    dom = _collect_dom_pagination(html, current_page=current_page)

    return {
        "next": rsc.get("next") or dom.get("next"),
        "previous": rsc.get("previous") or dom.get("previous"),
    }


# -----------------------------------------------------------------------------
# Legacy media/DOM helpers retained for compatibility
# -----------------------------------------------------------------------------


def extract_iframe_urls(html: str, base_url: str) -> List[str]:
    """Extract iframe URLs from rendered HTML as a legacy fallback."""
    soup = _soup(html)
    urls: List[str] = []

    for iframe in soup.find_all("iframe"):
        src = absolute_url(iframe.get("src"), base_url)
        if src:
            urls.append(src)

    return _dedupe_preserve_order(urls)


def extract_media_urls_from_html(html: str, base_url: str) -> List[str]:
    """Extract common media-like URLs from rendered HTML."""
    soup = _soup(html)
    urls: List[str] = []

    for tag_name, attribute in (
        ("video", "src"),
        ("source", "src"),
        ("img", "src"),
        ("iframe", "src"),
    ):
        for tag in soup.find_all(tag_name):
            value = absolute_url(tag.get(attribute), base_url)
            if value:
                urls.append(value)

    return _dedupe_preserve_order(urls)


def extract_title_from_dom(html: str) -> Optional[str]:
    """Extract a best-effort title from HTML."""
    soup = _soup(html)

    h1 = soup.find("h1")
    if h1:
        value = _clean_string(h1.get_text(" ", strip=True))
        if value:
            return value

    h2 = soup.find("h2")
    if h2:
        value = _clean_string(h2.get_text(" ", strip=True))
        if value:
            return value

    if soup.title:
        return _clean_string(soup.title.get_text(" ", strip=True))

    return None


def extract_slug_from_url(url: str) -> Optional[str]:
    """Extract a post slug from a /feed/<slug> URL."""
    value = _clean_string(url)
    if not value:
        return None

    parsed = urlparse(value)
    path = parsed.path.strip("/")

    if not path.startswith("feed/"):
        return None

    remainder = path[len("feed/") :]
    if remainder.startswith("page/"):
        return None

    return normalize_slug(remainder)


def extract_slug_from_dom(html: str) -> Optional[str]:
    """Extract the first post slug from a feed link in rendered HTML."""
    soup = _soup(html)

    for anchor in soup.find_all("a", href=True):
        href = _clean_string(anchor.get("href"))
        if not href:
            continue

        slug = extract_slug_from_url(href)
        if slug:
            return slug

    return None


def extract_dom_feed_items(html: str, base_url: str) -> List[Dict[str, Any]]:
    """
    Legacy DOM-only feed parser.

    It is intentionally secondary to RSC extraction because rendered HTML can
    hide media behind UI components or only show the first item in a gallery.
    """
    soup = _soup(html)
    result: List[Dict[str, Any]] = []

    for anchor in soup.find_all("a", href=True):
        href = _clean_string(anchor.get("href"))
        if not href:
            continue

        slug = extract_slug_from_url(href)
        if not slug:
            continue

        heading = anchor.find(["h1", "h2", "h3", "h4"])
        title = _clean_string(heading.get_text(" ", strip=True)) if heading else None

        media: List[Dict[str, Any]] = []

        for iframe in anchor.find_all("iframe"):
            src = absolute_url(iframe.get("src"), base_url)
            if src:
                media.append(
                    {
                        "type": "video",
                        "embed_url": src,
                    }
                )

        for image in anchor.find_all("img"):
            src = absolute_url(image.get("src"), base_url)
            if src:
                media.append(
                    {
                        "type": "image",
                        "image_url": src,
                    }
                )

        if not title and not media:
            continue

        item = {
            "title": title,
            "slug": slug,
            "url": absolute_url(href, base_url),
            "type": classify_media(media),
            "media_count": len(media),
            "video_count": len(get_video_media(media)),
            "image_count": len(get_image_media(media)),
            "media": media,
            "embed_url": media[0].get("embed_url") if media and media[0].get("type") == "video" else None,
            "video_id": None,
            "video_url": None,
            "thumbnail": None,
            "duration": None,
        }

        result.append(item)

    return result


# -----------------------------------------------------------------------------
# Feed page parser
# -----------------------------------------------------------------------------


def parse_page(
    html: str,
    base_url: str,
    current_page: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Parse a feed page into the public Phase 2/3 response shape.

    No network calls happen here and nothing is persisted.
    """
    if current_page is None:
        current_page = extract_page_number_from_url(base_url)
        if current_page is None:
            current_page = 1

    feed_objects = extract_rsc_feed_objects(html)

    items: List[Dict[str, Any]] = []
    seen_slugs = set()

    for feed_object in feed_objects:
        item = build_public_item(feed_object, base_url)
        slug = normalize_slug(item.get("slug"))
        if not slug:
            continue

        key = slug_key(slug)
        if key in seen_slugs:
            continue

        seen_slugs.add(key)
        items.append(item)

    # If RSC extraction somehow fails completely, retain a DOM fallback rather
    # than returning a false empty feed.
    if not items:
        dom_items = extract_dom_feed_items(html, base_url)
        for item in dom_items:
            slug = normalize_slug(item.get("slug"))
            if not slug:
                continue
            key = slug_key(slug)
            if key in seen_slugs:
                continue
            seen_slugs.add(key)
            items.append(item)

    pagination = extract_pagination(html, current_page=current_page)

    return {
        "items": items,
        "count": len(items),
        "pagination": pagination,
        "page": current_page,
    }


# -----------------------------------------------------------------------------
# Individual post parser
# -----------------------------------------------------------------------------


def _decode_next_image_url(value: Any) -> Optional[str]:
    """
    Recover the original image URL from a Next.js optimized image URL.

    Direct post pages render `/_next/image?url=<original>&...` in normal HTML.
    Feed RSC objects already contain the original URL, so this helper is only
    used by the direct-post DOM parser.
    """
    raw = _clean_string(value)
    if not raw:
        return None

    parsed = urlparse(raw)
    if parsed.path.rstrip("/") == "/_next/image":
        from urllib.parse import parse_qs

        query = parse_qs(parsed.query)
        original = query.get("url", [None])[0]
        if original:
            return unquote(original)

    return raw


def _dom_image_source(image: Any) -> Optional[str]:
    """Return the best original source URL for a rendered <img>."""
    candidates: List[str] = []

    for attribute in ("src", "data-src", "data-lazy-src"):
        value = _decode_next_image_url(image.get(attribute))
        if value:
            candidates.append(value)

    srcset = _clean_string(image.get("srcset")) or _clean_string(image.get("srcSet"))
    if srcset:
        # Prefer the largest source from srcset.  Each entry is `url width`.
        for entry in srcset.split(","):
            entry = entry.strip()
            if not entry:
                continue
            candidate = entry.split()[0]
            decoded = _decode_next_image_url(candidate)
            if decoded:
                candidates.append(decoded)

    return candidates[-1] if candidates else None


def _is_main_post_media_container(node: Any) -> bool:
    """Return True for the exact media wrapper used by direct post pages."""
    classes = node.get("class", []) if hasattr(node, "get") else []
    if isinstance(classes, str):
        classes = classes.split()
    classes = set(classes or [])
    return "mb-4" in classes and "space-y-3" in classes


def _find_main_post_media_container(h1: Any) -> Optional[Any]:
    """
    Find the media wrapper belonging to the main post H1.

    The direct post page structure is:
        channel header -> h1 -> div.mb-4.space-y-3 -> media blocks

    We deliberately anchor this to the H1 instead of scanning every iframe/img
    on the page, because the page also contains channel/recommendation images.
    """
    if h1 is None:
        return None

    for sibling in h1.find_all_next("div"):
        if _is_main_post_media_container(sibling):
            return sibling

    return None


def _extract_direct_post_media(container: Any, base_url: str) -> List[Dict[str, Any]]:
    """
    Extract the main post's media in exact DOM order.

    Each direct child of the media wrapper is one media block.  Video blocks
    contain an iframe; image blocks contain a blurred background image plus the
    real image.  The blurred image is marked aria-hidden/empty-alt and is
    intentionally ignored.
    """
    if container is None:
        return []

    media: List[Dict[str, Any]] = []

    for block in container.find_all(recursive=False):
        # BeautifulSoup may expose whitespace as NavigableString; those do not
        # have find()/get() and are skipped naturally here.
        if not hasattr(block, "find"):
            continue

        iframe = block.find("iframe")
        if iframe is not None:
            src = absolute_url(iframe.get("src"), base_url)
            if src:
                video_id = None
                parsed = urlparse(src)
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) >= 2 and parts[-2].lower() == "embed":
                    video_id = _clean_string(parts[-1])

                media.append(
                    {
                        "type": "video",
                        "id": video_id,
                        "videoId": video_id,
                        "url": src,
                    }
                )
            continue

        images = block.find_all("img")
        if images:
            chosen = None
            # Prefer the actual displayed image over the blurred background.
            for image in images:
                alt = _clean_string(image.get("alt"))
                aria_hidden = (_clean_string(image.get("aria-hidden")) or "").lower()
                classes = image.get("class", [])
                if isinstance(classes, str):
                    classes = classes.split()
                class_text = " ".join(classes or []).lower()

                if aria_hidden == "true" or not alt or "blur-2xl" in class_text:
                    continue

                chosen = image
                break

            if chosen is None:
                chosen = images[-1]

            image_url = _dom_image_source(chosen)
            if image_url:
                media.append(
                    {
                        "type": "image",
                        "id": None,
                        "url": image_url,
                    }
                )

    return media


def _extract_direct_post_metadata(h1: Any, base_url: str) -> Dict[str, Any]:
    """Extract channel metadata located immediately before the main H1."""
    metadata: Dict[str, Any] = {}
    if h1 is None:
        return metadata

    channel_anchor = None
    for anchor in h1.find_all_previous("a", href=True):
        href = _clean_string(anchor.get("href")) or ""
        if href.startswith("/channels/"):
            channel_anchor = anchor
            break

    if channel_anchor is None:
        return metadata

    channel_href = _clean_string(channel_anchor.get("href"))
    if channel_href:
        channel_path = urlparse(channel_href).path.strip("/")
        parts = channel_path.split("/")
        if len(parts) >= 2 and parts[0] == "channels":
            metadata["channel_id"] = parts[1]

    channel_name = channel_anchor.find("h3")
    if channel_name:
        metadata["channel_name"] = _clean_string(channel_name.get_text(" ", strip=True))

    username_node = channel_anchor.find("p")
    if username_node:
        username_text = _clean_string(username_node.get_text(" ", strip=True))
        if username_text:
            metadata["username"] = username_text.lstrip("@").strip()

    avatar = channel_anchor.find("img")
    if avatar is not None:
        avatar_url = _dom_image_source(avatar)
        if avatar_url:
            metadata["avatar"] = absolute_url(avatar_url, base_url)

    return {key: value for key, value in metadata.items() if value is not None}


def _extract_direct_post_description(h1: Any) -> Optional[str]:
    """Extract the main post description paragraph, if present."""
    if h1 is None:
        return None

    container = _find_main_post_media_container(h1)
    if container is None:
        return None

    # The prose description is the next sibling after the media wrapper in the
    # current direct-post layout.  Restrict the search to a nearby div so the
    # recommendation section cannot be mistaken for the description.
    for sibling in container.find_all_next("div", limit=5):
        classes = sibling.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        if "prose" not in set(classes or []):
            continue
        paragraph = sibling.find("p")
        if paragraph:
            return _clean_string(paragraph.get_text(" ", strip=True))

    return None


def _extract_direct_post_tags(h1: Any) -> List[str]:
    """Extract tag labels from the main post before the recommendation area."""
    if h1 is None:
        return []

    container = _find_main_post_media_container(h1)
    if container is None:
        return []

    description_div = None
    for sibling in container.find_all_next("div", limit=8):
        classes = sibling.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        if "prose" in set(classes or []):
            description_div = sibling
            break

    if description_div is None:
        return []

    tags: List[str] = []
    for sibling in description_div.find_all_next("div", limit=4):
        classes = sibling.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        if "flex-wrap" not in set(classes or []):
            continue
        for anchor in sibling.find_all("a", href=True):
            label = _clean_string(anchor.get_text(" ", strip=True))
            if label:
                tags.append(label)
        if tags:
            break

    return _dedupe_preserve_order(tags)


def _build_dom_post_object(
    h1: Any,
    requested_slug: str,
    html: str,
    base_url: str,
) -> Optional[Dict[str, Any]]:
    """Build a feed-shaped object from the direct post DOM."""
    title = _clean_string(h1.get_text(" ", strip=True)) if h1 is not None else None
    if not title:
        return None

    media_container = _find_main_post_media_container(h1)
    media = _extract_direct_post_media(media_container, base_url)

    # A valid direct post can technically have no media, but the page still
    # needs to be identified correctly.  We therefore accept the object as long
    # as its H1 exists and the requested route slug is valid.
    feed_object: Dict[str, Any] = {
        "_id": None,
        "slug": normalize_slug(requested_slug),
        "title": title,
        "mediaItems": media,
    }
    feed_object.update(_extract_direct_post_metadata(h1, base_url))

    description = _extract_direct_post_description(h1)
    if description:
        feed_object["description"] = description

    tags = _extract_direct_post_tags(h1)
    if tags:
        feed_object["tags"] = tags

    return feed_object


def _find_dom_post_match(html: str, requested_slug: str, base_url: str) -> Optional[Dict[str, Any]]:
    """
    Parse the actual direct `/feed/<slug>` page.

    This is intentionally NOT a feed-card parser.  Individual post pages do
    not render the main post as an `<a href="/feed/<slug>">` card.  Instead the
    page has a channel header, one `<h1>`, then the main media wrapper.
    """
    wanted = normalize_slug(requested_slug)
    if not wanted:
        return None

    soup = _soup(html)
    h1 = soup.find("h1")
    if h1 is None:
        return None

    # The direct post route is already the requested slug.  We still verify any
    # explicit canonical/path evidence when it exists, but we do not require a
    # `/feed/<slug>` anchor because there isn't one on the main post itself.
    canonical_slug = None
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical is not None:
        canonical_slug = extract_slug_from_url(_clean_string(canonical.get("href")) or "")

    if canonical_slug and slug_key(canonical_slug) != slug_key(wanted):
        return None

    feed_object = _build_dom_post_object(h1, wanted, html, base_url)
    if feed_object is None:
        return None

    return build_public_item(feed_object, base_url)


def extract_post_recommendations(
    html: str,
    requested_slug: str,
    base_url: str,
) -> List[Dict[str, Any]]:
    """
    Extract the recommendation cards rendered in the direct post page's
    ``You might like:`` section.

    On Desihub's direct post pages the main post is rendered directly in the
    page component, while recommendation cards are serialized as normal RSC
    ``feed`` objects.  Therefore the existing RSC feed-object extractor is the
    correct source for recommendations here.

    The requested post is excluded defensively in case the upstream response
    ever includes it in its own recommendation list.  Order is preserved.
    """
    wanted = normalize_slug(requested_slug)
    if not html or not wanted:
        return []

    recommendations: List[Dict[str, Any]] = []
    seen: set = set()

    for feed_object in extract_rsc_feed_objects(html):
        slug = normalize_slug(feed_object.get("slug"))
        if not slug:
            continue

        if slug_key(slug) == slug_key(wanted):
            continue

        identity = _feed_object_identity(feed_object)
        if identity in seen:
            continue

        seen.add(identity)
        recommendations.append(build_public_item(feed_object, base_url))

    return recommendations


def _attach_post_recommendations(
    item: Dict[str, Any],
    html: str,
    requested_slug: str,
    base_url: str,
) -> Dict[str, Any]:
    """Attach normalized recommendation cards to an individual post item."""
    recommendations = extract_post_recommendations(
        html,
        requested_slug,
        base_url,
    )
    item["recommendation_count"] = len(recommendations)
    item["recommendations"] = recommendations
    return item


def parse_post_page(
    html: str,
    requested_slug: str,
    base_url: str,
) -> Optional[Dict[str, Any]]:
    """
    Parse one individual `/feed/<slug>` page.

    Important architecture:
        * Feed pages (`/feed`, `/feed/page/N`) use RSC feed objects.
        * Individual post pages (`/feed/<slug>`) are parsed from their direct
          rendered HTML first.  The main post is not a normal `feed` RSC object;
          the RSC feed objects on that page are recommendation cards.

    RSC remains a secondary fallback for unusual page variants, never the
    primary lookup for the direct post.
    """
    wanted = normalize_slug(requested_slug)
    if not wanted:
        return None

    # PRIMARY: direct post DOM.
    dom_match = _find_dom_post_match(html, wanted, base_url)
    if dom_match is not None:
        return _attach_post_recommendations(dom_match, html, wanted, base_url)

    # SECONDARY: some upstream revisions may serialize the main post as a
    # normal feed object.  Preserve the old robust RSC recovery for those pages.
    stream = combined_rsc_payload(html)
    feed_objects = extract_rsc_feed_objects(html)
    match = find_feed_object_by_slug(feed_objects, wanted)

    if match is not None:
        return _attach_post_recommendations(
            build_public_item(match, base_url),
            html,
            wanted,
            base_url,
        )

    near_slug_match = _extract_feed_object_near_slug(stream, wanted)
    if near_slug_match is not None:
        return _attach_post_recommendations(
            build_public_item(near_slug_match, base_url),
            html,
            wanted,
            base_url,
        )

    partial_match = _extract_post_object_from_stream_by_slug(stream, wanted)
    if partial_match is not None:
        return _attach_post_recommendations(
            build_public_item(partial_match, base_url),
            html,
            wanted,
            base_url,
        )

    return None


# -----------------------------------------------------------------------------
# Debug/introspection helpers
# -----------------------------------------------------------------------------


def parser_debug_info(html: str) -> Dict[str, Any]:
    """Return non-sensitive parser diagnostics for local testing."""
    segments = extract_next_f_segments(html)
    stream = _flight_stream_from_segments(segments)
    feeds = _extract_feed_objects_direct(stream)

    return {
        "html_length": len(html or ""),
        "next_f_segment_count": len(segments),
        "next_f_type1_count": sum(1 for tag, _ in segments if tag == 1),
        "rsc_stream_length": len(stream),
        "feed_object_count": len(feeds),
        "feed_slugs": [normalize_slug(item.get("slug")) for item in feeds],
    }


__all__ = [
    "absolute_url",
    "build_public_item",
    "classify_media",
    "combined_rsc_payload",
    "extract_dom_feed_items",
    "extract_feed_objects",
    "extract_iframe_urls",
    "extract_media_urls_from_html",
    "extract_next_f_payloads",
    "extract_next_f_segments",
    "extract_page_number_from_url",
    "extract_pagination",
    "extract_rsc_feed_objects",
    "extract_rsc_media",
    "extract_slug_from_dom",
    "extract_slug_from_url",
    "find_feed_object_by_slug",
    "get_feed_media_items",
    "get_image_media",
    "get_video_media",
    "normalize_media_item",
    "normalize_slug",
    "page_path_for_number",
    "parse_page",
    "parse_post_page",
    "extract_post_recommendations",
    "parser_debug_info",
    "slug_key",
]


# Backwards-compatible alias used by a few earlier parser revisions.
extract_feed_objects = extract_rsc_feed_objects
