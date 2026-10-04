from __future__ import annotations

import json
import re
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
)
from urllib.parse import (
    unquote,
    urljoin,
    urlparse,
)

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Next.js / React Server Components
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


FEED_PATH_PATTERN = re.compile(
    r"^/feed(?:/page/(?P<page>\d+))?(?:/.*)?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _safe_int(
    value: Any,
) -> Optional[int]:
    if value is None:
        return None

    if isinstance(
        value,
        bool,
    ):
        return None

    try:
        return int(value)
    except (
        TypeError,
        ValueError,
    ):
        return None


def _clean_string(
    value: Any,
) -> Optional[str]:
    if value is None:
        return None

    if not isinstance(
        value,
        str,
    ):
        value = str(value)

    value = value.strip()

    if not value:
        return None

    return value


def _normalize_slug(
    value: Any,
) -> Optional[str]:
    value = _clean_string(
        value
    )

    if not value:
        return None

    value = unquote(
        value
    ).strip()

    value = value.strip(
        "/"
    )

    if value.startswith(
        "feed/"
    ):
        value = value[5:]

    if "/page/" in value:
        parts = value.split(
            "/page/",
            1,
        )

        if (
            len(parts) == 2
            and parts[1].isdigit()
        ):
            return None

    if "/" in value:
        value = (
            value
            .rstrip("/")
            .split("/")[-1]
        )

    return value or None


def _absolute_or_original(
    url: Any,
    base_url: str,
) -> Optional[str]:
    url = _clean_string(
        url
    )

    if not url:
        return None

    return urljoin(
        base_url.rstrip("/") + "/",
        url,
    )


def _is_http_url(
    value: Any,
) -> bool:
    value = _clean_string(
        value
    )

    if not value:
        return False

    parsed = urlparse(
        value
    )

    return (
        parsed.scheme
        in {
            "http",
            "https",
        }
        and bool(
            parsed.netloc
        )
    )


def _extract_balanced_json(
    text: str,
    start_index: int,
) -> Optional[str]:
    """
    Extract a balanced JSON object/array beginning at start_index.
    """

    if not text:
        return None

    if (
        start_index < 0
        or start_index >= len(text)
    ):
        return None

    opening = text[
        start_index
    ]

    if opening not in "{[":
        return None

    closing = (
        "}"
        if opening == "{"
        else "]"
    )

    depth = 0
    in_string = False
    escaped = False

    for index in range(
        start_index,
        len(text),
    ):
        char = text[
            index
        ]

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
                return text[
                    start_index:index + 1
                ]

    return None


# ---------------------------------------------------------------------------
# Next.js Flight payload extraction
# ---------------------------------------------------------------------------

def extract_next_f_payloads(
    html: str,
) -> List[str]:
    """
    Extract and JSON-decode every self.__next_f.push([1, "..."]) payload.

    Next.js can split one Flight row across multiple script tags, so every
    decoded chunk is preserved and joined later.
    """

    if not html:
        return []

    payloads: List[str] = []

    for match in NEXT_F_PUSH_PATTERN.finditer(
        html
    ):
        raw_payload = match.group(
            "payload"
        )

        if not raw_payload:
            continue

        try:
            payload = json.loads(
                raw_payload
            )
        except (
            TypeError,
            json.JSONDecodeError,
        ):
            continue

        if isinstance(
            payload,
            str,
        ):
            payloads.append(
                payload
            )

    return payloads


def _combined_rsc_payload(
    html: str,
) -> str:
    return "".join(
        extract_next_f_payloads(
            html
        )
    )


def _parse_rsc_row_value(
    value: str,
) -> Optional[Any]:
    value = value.strip()

    if not value:
        return None

    if value[0] not in "[{":
        return None

    try:
        return json.loads(
            value
        )
    except (
        TypeError,
        json.JSONDecodeError,
    ):
        return None


def _iter_rsc_rows(
    html: str,
) -> Iterable[Any]:
    """
    Parse JSON-valued Flight rows after all inline chunks have been joined.

    Example:

        24:["$","$L21","id",{"feed":{...}}]

    becomes:

        ["$","$L21","id",{"feed":{...}}]
    """

    combined = _combined_rsc_payload(
        html
    )

    if not combined:
        return

    for raw_line in combined.splitlines():
        line = raw_line.strip()

        if (
            not line
            or ":" not in line
        ):
            continue

        _row_id, row_value = (
            line.split(
                ":",
                1,
            )
        )

        parsed = _parse_rsc_row_value(
            row_value
        )

        if parsed is None:
            continue

        yield parsed


def _walk_json(
    value: Any,
) -> Iterable[Any]:
    """
    Recursively walk dictionaries and lists.
    """

    if isinstance(
        value,
        dict,
    ):
        yield value

        for child in value.values():
            yield from _walk_json(
                child
            )

        return

    if isinstance(
        value,
        list,
    ):
        for child in value:
            yield from _walk_json(
                child
            )


# ---------------------------------------------------------------------------
# Feed object extraction
# ---------------------------------------------------------------------------

def _looks_like_feed_object(
    value: Any,
) -> bool:
    if not isinstance(
        value,
        dict,
    ):
        return False

    slug = _normalize_slug(
        value.get("slug")
    )

    title = _clean_string(
        value.get("title")
    )

    media_items = value.get(
        "mediaItems"
    )

    if not slug:
        return False

    if not title:
        return False

    if not isinstance(
        media_items,
        list,
    ):
        return False

    return True


def _feed_object_key(
    feed_object: Dict[str, Any],
) -> Tuple[Any, str]:
    return (
        feed_object.get(
            "_id"
        ),
        _normalize_slug(
            feed_object.get(
                "slug"
            )
        )
        or "",
    )


def extract_rsc_feed_objects(
    html: str,
) -> List[Dict[str, Any]]:
    """
    Extract every actual feed object from the RSC Flight payload.

    The parser deliberately does NOT depend on a volatile component ID such
    as $L20 or $L21.

    Instead it identifies the stable feed-object shape:

        title
        slug
        mediaItems
    """

    results: List[
        Dict[str, Any]
    ] = []

    seen: Set[
        Tuple[Any, str]
    ] = set()

    for row in _iter_rsc_rows(
        html
    ):
        for candidate in _walk_json(
            row
        ):
            if not _looks_like_feed_object(
                candidate
            ):
                continue

            key = _feed_object_key(
                candidate
            )

            if key in seen:
                continue

            seen.add(
                key
            )

            results.append(
                candidate
            )

    return results


# ---------------------------------------------------------------------------
# Media normalization
# ---------------------------------------------------------------------------

def normalize_media_item(
    media_item: Any,
    position: int,
) -> Optional[
    Dict[str, Any]
]:
    if not isinstance(
        media_item,
        dict,
    ):
        return None

    media_type = _clean_string(
        media_item.get(
            "type"
        )
    )

    if media_type:
        media_type = (
            media_type.lower()
        )

    media_id = _clean_string(
        media_item.get(
            "id"
        )
    )

    # ------------------------------------------------------------------
    # Video
    # ------------------------------------------------------------------

    if (
        media_type == "video"
        or any(
            media_item.get(
                key
            )
            for key in (
                "videoId",
                "videoUrl",
                "thumbnailUrl",
                "duration",
            )
        )
    ):
        embed_url = _clean_string(
            media_item.get(
                "url"
            )
            or media_item.get(
                "embedUrl"
            )
            or media_item.get(
                "embed_url"
            )
        )

        video_id = _clean_string(
            media_item.get(
                "videoId"
            )
            or media_item.get(
                "video_id"
            )
        )

        video_url = _clean_string(
            media_item.get(
                "videoUrl"
            )
            or media_item.get(
                "video_url"
            )
        )

        thumbnail = _clean_string(
            media_item.get(
                "thumbnailUrl"
            )
            or media_item.get(
                "thumbnail"
            )
        )

        duration = _safe_int(
            media_item.get(
                "duration"
            )
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

    # ------------------------------------------------------------------
    # Image
    # ------------------------------------------------------------------

    if (
        media_type == "image"
        or media_item.get(
            "url"
        )
    ):
        image_url = _clean_string(
            media_item.get(
                "url"
            )
            or media_item.get(
                "imageUrl"
            )
            or media_item.get(
                "image_url"
            )
        )

        if image_url:
            return {
                "position": position,
                "type": "image",
                "id": media_id,
                "url": image_url,
            }

    return None


def normalize_media_items(
    media_items: Any,
) -> List[
    Dict[str, Any]
]:
    if not isinstance(
        media_items,
        list,
    ):
        return []

    normalized: List[
        Dict[str, Any]
    ] = []

    for position, media_item in enumerate(
        media_items
    ):
        item = normalize_media_item(
            media_item,
            position,
        )

        if item is None:
            continue

        normalized.append(
            item
        )

    return normalized


def get_feed_media_items(
    feed_object: Dict[str, Any],
) -> List[
    Dict[str, Any]
]:
    return normalize_media_items(
        feed_object.get(
            "mediaItems"
        )
    )


def get_video_media(
    feed_object: Dict[str, Any],
) -> List[
    Dict[str, Any]
]:
    return [
        media
        for media in get_feed_media_items(
            feed_object
        )
        if media.get(
            "type"
        ) == "video"
    ]


def get_image_media(
    feed_object: Dict[str, Any],
) -> List[
    Dict[str, Any]
]:
    return [
        media
        for media in get_feed_media_items(
            feed_object
        )
        if media.get(
            "type"
        ) == "image"
    ]


def classify_media(
    media: List[
        Dict[str, Any]
    ],
) -> str:
    if not media:
        return "unknown"

    video_count = sum(
        1
        for item in media
        if item.get(
            "type"
        ) == "video"
    )

    image_count = sum(
        1
        for item in media
        if item.get(
            "type"
        ) == "image"
    )

    if (
        video_count
        and image_count
    ):
        return "mixed"

    if image_count:
        return "image_gallery"

    if video_count > 1:
        return "video_collection"

    if video_count == 1:
        return "video"

    return "unknown"


# ---------------------------------------------------------------------------
# Slug / URL helpers
# ---------------------------------------------------------------------------

def extract_slug_from_url(
    url: Any,
) -> Optional[str]:
    url = _clean_string(
        url
    )

    if not url:
        return None

    parsed = urlparse(
        url
    )

    path = (
        parsed.path
        if parsed.scheme
        else url
    )

    path = unquote(
        path
    )

    path = path.rstrip(
        "/"
    )

    if not path:
        return None

    parts = [
        part
        for part in path.split(
            "/"
        )
        if part
    ]

    if not parts:
        return None

    # /feed/page/6 is pagination, not a post.
    if len(parts) >= 3:
        for index in range(
            len(parts) - 1
        ):
            if (
                parts[index].lower()
                == "page"
                and parts[index + 1].isdigit()
            ):
                return None

    lower_parts = [
        part.lower()
        for part in parts
    ]

    if "feed" in lower_parts:
        feed_index = (
            lower_parts.index(
                "feed"
            )
        )

        if (
            feed_index + 1
            < len(parts)
        ):
            return parts[
                feed_index + 1
            ]

        return None

    return parts[-1]


def canonical_post_url(
    slug: str,
    base_url: str,
) -> str:
    return (
        f"{base_url.rstrip('/')}/feed/"
        f"{slug.lstrip('/')}"
    )


# ---------------------------------------------------------------------------
# Public item builder
# ---------------------------------------------------------------------------

def build_public_item(
    feed_object: Dict[str, Any],
    base_url: str,
) -> Dict[str, Any]:
    media = get_feed_media_items(
        feed_object
    )

    video_media = [
        item
        for item in media
        if item.get(
            "type"
        ) == "video"
    ]

    image_media = [
        item
        for item in media
        if item.get(
            "type"
        ) == "image"
    ]

    first_video = (
        video_media[0]
        if video_media
        else None
    )

    slug = _normalize_slug(
        feed_object.get(
            "slug"
        )
    )

    item_url = (
        canonical_post_url(
            slug,
            base_url,
        )
        if slug
        else None
    )

    return {
        "title": _clean_string(
            feed_object.get(
                "title"
            )
        ),
        "slug": slug,
        "url": item_url,
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
        "media": media,

        # Legacy first-video compatibility fields.
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


# ---------------------------------------------------------------------------
# DOM helpers
# ---------------------------------------------------------------------------

def _find_dom_post_anchor(
    soup: BeautifulSoup,
    requested_slug: str,
):
    requested_slug = _normalize_slug(
        requested_slug
    )

    if not requested_slug:
        return None

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        href = anchor.get(
            "href"
        )

        if not href:
            continue

        slug = extract_slug_from_url(
            href
        )

        if slug == requested_slug:
            return anchor

    return None


def _extract_dom_media_from_post(
    anchor,
) -> List[
    Dict[str, Any]
]:
    """
    Fallback only.

    RSC remains the primary source.
    """

    if anchor is None:
        return []

    root = anchor

    media: List[
        Dict[str, Any]
    ] = []

    iframes = root.find_all(
        "iframe",
        src=True,
    )

    for position, iframe in enumerate(
        iframes
    ):
        src = _clean_string(
            iframe.get(
                "src"
            )
        )

        if not src:
            continue

        media.append(
            {
                "position": position,
                "type": "video",
                "id": None,
                "embed_url": src,
                "video_id": None,
                "video_url": None,
                "thumbnail": None,
                "duration": None,
            }
        )

    image_offset = len(
        media
    )

    for index, image in enumerate(
        root.find_all(
            "img",
            src=True,
        )
    ):
        src = _clean_string(
            image.get(
                "src"
            )
        )

        if not src:
            continue

        if src.startswith(
            "/_next/image"
        ):
            try:
                parsed = urlparse(
                    src
                )

                query = (
                    parsed.query
                )

                for part in query.split(
                    "&"
                ):
                    if part.startswith(
                        "url="
                    ):
                        src = unquote(
                            part.split(
                                "=",
                                1,
                            )[1]
                        )
                        break

            except Exception:
                pass

        if not _is_http_url(
            src
        ):
            continue

        media.append(
            {
                "position": (
                    image_offset
                    + index
                ),
                "type": "image",
                "id": None,
                "url": src,
            }
        )

    return media


def _build_dom_fallback_post(
    soup: BeautifulSoup,
    requested_slug: str,
    base_url: str,
) -> Optional[
    Dict[str, Any]
]:
    anchor = _find_dom_post_anchor(
        soup,
        requested_slug,
    )

    if anchor is None:
        return None

    title = None

    heading = anchor.find(
        [
            "h1",
            "h2",
            "h3",
        ]
    )

    if heading is not None:
        title = _clean_string(
            heading.get_text(
                " ",
                strip=True,
            )
        )

    media = _extract_dom_media_from_post(
        anchor
    )

    if (
        not title
        and not media
    ):
        return None

    normalized_slug = _normalize_slug(
        requested_slug
    )

    if not normalized_slug:
        return None

    video_media = [
        item
        for item in media
        if item.get(
            "type"
        ) == "video"
    ]

    image_media = [
        item
        for item in media
        if item.get(
            "type"
        ) == "image"
    ]

    first_video = (
        video_media[0]
        if video_media
        else None
    )

    return {
        "title": title,
        "slug": normalized_slug,
        "url": canonical_post_url(
            normalized_slug,
            base_url,
        ),
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
        "media": media,
        "embed_url": (
            first_video.get(
                "embed_url"
            )
            if first_video
            else None
        ),
        "video_id": None,
        "video_url": None,
        "thumbnail": None,
        "duration": None,
    }


# ---------------------------------------------------------------------------
# Pagination extraction
# ---------------------------------------------------------------------------

def _contains_aria_label(
    value: Any,
    wanted: str,
) -> bool:
    wanted = wanted.lower()

    for node in _walk_json(
        value
    ):
        if not isinstance(
            node,
            dict,
        ):
            continue

        aria_label = _clean_string(
            node.get(
                "aria-label"
            )
        )

        if (
            aria_label
            and wanted
            in aria_label.lower()
        ):
            return True

    return False


def _collect_rsc_pagination(
    html: str,
) -> Tuple[
    Optional[str],
    Optional[str],
]:
    """
    Read previous/next pagination from RSC.

    We find an href dictionary whose subtree contains the appropriate
    aria-label.
    """

    next_path = None
    previous_path = None

    for row in _iter_rsc_rows(
        html
    ):
        for node in _walk_json(
            row
        ):
            if not isinstance(
                node,
                dict,
            ):
                continue

            href = _clean_string(
                node.get(
                    "href"
                )
            )

            if not href:
                continue

            if not href.startswith(
                "/feed"
            ):
                continue

            if _contains_aria_label(
                node,
                "Next Page",
            ):
                next_path = href

            if _contains_aria_label(
                node,
                "Previous Page",
            ):
                previous_path = href

    return (
        next_path,
        previous_path,
    )


def _collect_dom_pagination(
    soup: BeautifulSoup,
    base_url: str,
) -> Tuple[
    Optional[str],
    Optional[str],
]:
    next_url = None
    previous_url = None

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        href = _clean_string(
            anchor.get(
                "href"
            )
        )

        if not href:
            continue

        text = anchor.get_text(
            " ",
            strip=True,
        ).lower()

        aria_label = _clean_string(
            anchor.get(
                "aria-label"
            )
        )

        aria_label = (
            aria_label.lower()
            if aria_label
            else ""
        )

        if (
            "next page"
            in aria_label
            or text == "next"
        ):
            next_url = href

        if (
            "previous page"
            in aria_label
            or "prev page"
            in aria_label
            or text == "previous"
            or text == "prev"
        ):
            previous_url = href

    return (
        next_url,
        previous_url,
    )


def extract_pagination(
    html: str,
    base_url: str,
) -> Dict[
    str,
    Optional[str],
]:
    """
    Return upstream-relative pagination paths.

    app.py converts these into public API URLs.
    """

    (
        rsc_next,
        rsc_previous,
    ) = _collect_rsc_pagination(
        html
    )

    soup = BeautifulSoup(
        html or "",
        "html.parser",
    )

    (
        dom_next,
        dom_previous,
    ) = _collect_dom_pagination(
        soup,
        base_url,
    )

    next_path = (
        rsc_next
        or dom_next
    )

    previous_path = (
        rsc_previous
        or dom_previous
    )

    return {
        "next": next_path,
        "previous": previous_path,
    }


# ---------------------------------------------------------------------------
# Feed page parser
# ---------------------------------------------------------------------------

def _extract_page_title(
    soup: BeautifulSoup,
) -> Optional[str]:
    title = soup.find(
        "title"
    )

    if title is None:
        return None

    return _clean_string(
        title.get_text(
            " ",
            strip=True,
        )
    )


def parse_page(
    html: str,
    base_url: str,
) -> Dict[str, Any]:
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

    title = _extract_page_title(
        soup
    )

    feed_objects = (
        extract_rsc_feed_objects(
            html
        )
    )

    feeds_by_slug: Dict[
        str,
        Dict[str, Any],
    ] = {}

    for feed_object in feed_objects:
        slug = _normalize_slug(
            feed_object.get(
                "slug"
            )
        )

        if not slug:
            continue

        if slug not in feeds_by_slug:
            feeds_by_slug[
                slug
            ] = feed_object

    items: List[
        Dict[str, Any]
    ] = []

    seen_slugs: Set[
        str
    ] = set()

    # ------------------------------------------------------------------
    # Primary ordering source:
    # rendered feed anchors
    # ------------------------------------------------------------------

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        href = _clean_string(
            anchor.get(
                "href"
            )
        )

        if not href:
            continue

        if (
            href == "/feed"
            or href.startswith(
                "/feed/page/"
            )
        ):
            continue

        slug = extract_slug_from_url(
            href
        )

        if not slug:
            continue

        feed_object = (
            feeds_by_slug.get(
                slug
            )
        )

        if feed_object is None:
            continue

        if slug in seen_slugs:
            continue

        seen_slugs.add(
            slug
        )

        item = build_public_item(
            feed_object,
            base_url,
        )

        item["url"] = (
            _absolute_or_original(
                href,
                base_url,
            )
        )

        items.append(
            item
        )

    # ------------------------------------------------------------------
    # RSC fallback
    # ------------------------------------------------------------------

    if (
        not items
        and feed_objects
    ):
        for feed_object in feed_objects:
            slug = _normalize_slug(
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

    pagination = extract_pagination(
        html,
        base_url,
    )

    return {
        "title": title,
        "count": len(
            items
        ),
        "items": items,
        "pagination": pagination,
    }


# ---------------------------------------------------------------------------
# Individual post parser
# ---------------------------------------------------------------------------

def _find_feed_object_by_slug(
    feed_objects: List[
        Dict[str, Any
        ]
    ],
    requested_slug: str,
) -> Optional[
    Dict[str, Any]
]:
    normalized_requested = (
        _normalize_slug(
            requested_slug
        )
    )

    if not normalized_requested:
        return None

    # Exact match first.
    for feed_object in feed_objects:
        slug = _normalize_slug(
            feed_object.get(
                "slug"
            )
        )

        if (
            slug
            == normalized_requested
        ):
            return feed_object

    # Case-insensitive fallback.
    requested_lower = (
        normalized_requested.lower()
    )

    for feed_object in feed_objects:
        slug = _normalize_slug(
            feed_object.get(
                "slug"
            )
        )

        if (
            slug
            and slug.lower()
            == requested_lower
        ):
            return feed_object

    return None


def parse_post_page(
    html: str,
    requested_slug: str,
    base_url: str,
) -> Optional[
    Dict[str, Any]
]:
    """
    Parse one individual /feed/{slug} page.

    Phase 3 strategy:

        1. Extract every RSC feed object.
        2. Match the requested slug.
        3. Build the exact same public media structure as feed pages.
        4. Ignore recommendation objects.
        5. Fall back to DOM only if RSC extraction fails.
    """

    if not html:
        return None

    normalized_requested = (
        _normalize_slug(
            requested_slug
        )
    )

    if not normalized_requested:
        return None

    feed_objects = (
        extract_rsc_feed_objects(
            html
        )
    )

    primary_feed = (
        _find_feed_object_by_slug(
            feed_objects,
            normalized_requested,
        )
    )

    if primary_feed is not None:
        item = build_public_item(
            primary_feed,
            base_url,
        )

        item["url"] = (
            canonical_post_url(
                normalized_requested,
                base_url,
            )
        )

        return item

    # DOM fallback.
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    return _build_dom_fallback_post(
        soup,
        normalized_requested,
        base_url,
    )
