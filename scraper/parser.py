from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup


# ============================================================================
# Next.js / RSC extraction constants
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

# The site currently renders feed pagination inside a React Server Component
# chunk. These labels are part of the RSC tree, not necessarily visible HTML
# in every response mode.
RSC_NEXT_LABEL_PATTERN = re.compile(
    r'"href"\s*:\s*"(?P<href>[^"]+)"'
    r'.{0,1600}?'
    r'"aria-label"\s*:\s*"Next Page"',
    re.DOTALL,
)

RSC_PREVIOUS_LABEL_PATTERN = re.compile(
    r'"href"\s*:\s*"(?P<href>[^"]+)"'
    r'.{0,1600}?'
    r'"aria-label"\s*:\s*"Previous Page"',
    re.DOTALL,
)

# A second pattern handles the common case where the aria-label occurs first
# in the serialized RSC object and the href occurs shortly before it.
RSC_NEXT_REVERSE_PATTERN = re.compile(
    r'"aria-label"\s*:\s*"Next Page"'
    r'.{0,1600}?'
    r'"href"\s*:\s*"(?P<href>[^"]+)"',
    re.DOTALL,
)

RSC_PREVIOUS_REVERSE_PATTERN = re.compile(
    r'"aria-label"\s*:\s*"Previous Page"'
    r'.{0,1600}?'
    r'"href"\s*:\s*"(?P<href>[^"]+)"',
    re.DOTALL,
)

SLUG_SAFE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$", re.IGNORECASE)


# ============================================================================
# Generic helpers
# ============================================================================

def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """
    Convert a value to int without allowing malformed scraper data to crash
    the whole response.
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return int(value)

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value)

    try:
        text = str(value).strip()
    except Exception:
        return default

    if not text:
        return default

    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _clean_string(value: Any) -> Optional[str]:
    """
    Normalize strings while keeping meaningful content unchanged.
    """
    if value is None:
        return None

    if isinstance(value, str):
        value = value.strip()
        return value or None

    try:
        value = str(value).strip()
    except Exception:
        return None

    return value or None


def _safe_lower(value: Any) -> str:
    """
    Lowercase a value safely for matching.
    """
    text = _clean_string(value)
    return text.lower() if text else ""


def _dedupe_preserve_order(values: Iterable[Any]) -> List[Any]:
    """
    De-duplicate values while preserving their first-seen order.
    """
    output: List[Any] = []
    seen = set()

    for value in values:
        try:
            marker = json.dumps(value, sort_keys=True, ensure_ascii=False)
        except Exception:
            marker = repr(value)

        if marker in seen:
            continue

        seen.add(marker)
        output.append(value)

    return output


def _absolute_or_original(url: Any, base_url: str) -> Optional[str]:
    """
    Convert a relative URL to an absolute URL.
    """
    value = _clean_string(url)
    if not value:
        return None

    if value.startswith(("http://", "https://")):
        return value

    try:
        return urljoin(base_url, value)
    except Exception:
        return value


def _same_host(url: str, base_url: str) -> bool:
    """
    Check whether two URLs point at the same hostname.
    """
    try:
        left = urlparse(url).netloc.lower()
        right = urlparse(base_url).netloc.lower()
        return bool(left and right and left == right)
    except Exception:
        return False


def _extract_balanced_json(text: str, start: int) -> Optional[str]:
    """
    Extract a balanced JSON object/array beginning at `start`.

    Strings are handled correctly so braces inside titles, URLs, or escaped
    text do not terminate the object early.
    """
    if not text or start < 0 or start >= len(text):
        return None

    opening = text[start]

    if opening not in "{[":
        return None

    closing = "}" if opening == "{" else "]"

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
                return text[start:index + 1]

    return None


def _decode_json_string(value: str) -> Optional[str]:
    """
    Decode a JSON-encoded string used as the second element of
    self.__next_f.push([1, "..."]).
    """
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, str) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


# ============================================================================
# Next.js Flight / RSC extraction
# ============================================================================

def extract_next_f_payloads(html: str) -> List[str]:
    """
    Extract every decoded payload from self.__next_f.push([1, "..."]).

    The same page can contain many Flight chunks. We intentionally preserve
    chunk order because feed objects and pagination can be split across them.
    """
    if not html:
        return []

    payloads: List[str] = []

    for match in NEXT_F_PUSH_PATTERN.finditer(html):
        encoded_payload = match.group("payload")
        decoded_payload = _decode_json_string(encoded_payload)

        if decoded_payload is None:
            continue

        payloads.append(decoded_payload)

    return payloads


def _payload_contains_feed_object(payload: str) -> bool:
    """
    Cheap pre-filter before running deeper RSC parsing.
    """
    if not payload:
        return False

    return (
        '"feed"' in payload
        and '"slug"' in payload
        and '"mediaItems"' in payload
    )


def _find_feed_objects_in_payload(payload: str) -> List[Dict[str, Any]]:
    """
    Find serialized RSC objects containing a `feed` object.

    We do not assume that a whole Flight chunk is one JSON document. A single
    payload can contain multiple RSC records separated by newlines and can
    contain references such as $L23.
    """
    if not _payload_contains_feed_object(payload):
        return []

    objects: List[Dict[str, Any]] = []

    # Fast path: locate every `"feed":{` candidate and parse the enclosing
    # object from the opening brace.
    cursor = 0

    while True:
        marker = payload.find('"feed"', cursor)

        if marker == -1:
            break

        colon = payload.find(":", marker + len('"feed"'))
        if colon == -1:
            break

        feed_start = payload.find("{", colon + 1)

        if feed_start == -1:
            break

        feed_json = _extract_balanced_json(payload, feed_start)

        if not feed_json:
            cursor = marker + len('"feed"')
            continue

        try:
            feed_object = json.loads(feed_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            cursor = feed_start + 1
            continue

        if not isinstance(feed_object, dict):
            cursor = feed_start + len(feed_json)
            continue

        if not _clean_string(feed_object.get("slug")):
            cursor = feed_start + len(feed_json)
            continue

        if "mediaItems" not in feed_object:
            cursor = feed_start + len(feed_json)
            continue

        objects.append(feed_object)
        cursor = feed_start + len(feed_json)

    return objects


def _dedupe_feed_objects(
    feed_objects: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    De-duplicate feed objects by slug first, then _id when a slug is absent.
    """
    output: List[Dict[str, Any]] = []
    seen_slugs = set()
    seen_ids = set()

    for feed in feed_objects:
        if not isinstance(feed, dict):
            continue

        slug = _clean_string(feed.get("slug"))
        feed_id = _clean_string(feed.get("_id"))

        if slug:
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)

        elif feed_id:
            if feed_id in seen_ids:
                continue
            seen_ids.add(feed_id)

        else:
            continue

        output.append(feed)

    return output


def extract_rsc_feed_objects(html: str) -> List[Dict[str, Any]]:
    """
    Extract feed records from all Next.js RSC chunks.

    Important:
    The individual post page contains a main feed record followed by
    recommendation records ("You might like"). This function deliberately
    returns all records; callers decide which one is primary.
    """
    payloads = extract_next_f_payloads(html)

    all_objects: List[Dict[str, Any]] = []

    for payload in payloads:
        all_objects.extend(_find_feed_objects_in_payload(payload))

    return _dedupe_feed_objects(all_objects)


# ============================================================================
# Media normalization
# ============================================================================

def normalize_media_item(
    media_item: Any,
    position: int = 0,
) -> Optional[Dict[str, Any]]:
    """
    Normalize one raw mediaItems element into the public API media schema.

    Supported source types:
      - video
      - image

    Unknown types are ignored instead of being guessed.
    """
    if not isinstance(media_item, dict):
        return None

    media_type = _safe_lower(media_item.get("type"))

    if media_type == "video":
        item_id = _clean_string(media_item.get("id"))
        embed_url = _clean_string(media_item.get("url"))
        video_id = _clean_string(media_item.get("videoId"))
        video_url = _clean_string(media_item.get("videoUrl"))
        thumbnail = _clean_string(media_item.get("thumbnailUrl"))
        duration = _safe_int(media_item.get("duration"))

        return {
            "position": position,
            "type": "video",
            "id": item_id,
            "embed_url": embed_url,
            "video_id": video_id,
            "video_url": video_url,
            "thumbnail": thumbnail,
            "duration": duration,
        }

    if media_type == "image":
        item_id = _clean_string(media_item.get("id"))
        image_url = _clean_string(media_item.get("url"))

        return {
            "position": position,
            "type": "image",
            "id": item_id,
            "url": image_url,
        }

    return None


def normalize_media_items(
    media_items: Any,
) -> List[Dict[str, Any]]:
    """
    Normalize the complete mediaItems array while preserving original order.
    """
    if not isinstance(media_items, list):
        return []

    normalized: List[Dict[str, Any]] = []

    for position, raw_item in enumerate(media_items):
        item = normalize_media_item(raw_item, position)

        if item is None:
            continue

        normalized.append(item)

    # Re-number after unsupported entries are removed so public positions are
    # contiguous and deterministic.
    for position, item in enumerate(normalized):
        item["position"] = position

    return normalized


def get_feed_media_items(
    feed_object: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Read and normalize mediaItems from one feed object.
    """
    if not isinstance(feed_object, dict):
        return []

    return normalize_media_items(feed_object.get("mediaItems"))


def get_video_media(
    media: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Return only video media in original order.
    """
    return [
        item
        for item in media
        if isinstance(item, dict)
        and item.get("type") == "video"
    ]


def get_image_media(
    media: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Return only image media in original order.
    """
    return [
        item
        for item in media
        if isinstance(item, dict)
        and item.get("type") == "image"
    ]


def classify_media(
    media: Iterable[Dict[str, Any]],
) -> str:
    """
    Classify a post based on its complete media array.

    Rules:
      0 media                  -> unknown
      1 video                  -> video
      >1 videos only          -> video_collection
      images only              -> image_gallery
      videos + images         -> mixed
    """
    media_list = list(media)

    if not media_list:
        return "unknown"

    video_count = sum(
        1 for item in media_list if item.get("type") == "video"
    )
    image_count = sum(
        1 for item in media_list if item.get("type") == "image"
    )

    if video_count == 1 and image_count == 0:
        return "video"

    if video_count > 1 and image_count == 0:
        return "video_collection"

    if image_count > 0 and video_count == 0:
        return "image_gallery"

    if video_count > 0 and image_count > 0:
        return "mixed"

    return "unknown"


# ============================================================================
# URL / slug helpers
# ============================================================================

def extract_slug_from_url(
    url: Any,
) -> Optional[str]:
    """
    Extract a post slug from URLs such as:

        /feed/my-post
        https://desihub.sh/feed/my-post
        /feed/page/6/my-post

    The last non-empty path component is treated as the slug only when the
    URL is under /feed/.
    """
    value = _clean_string(url)

    if not value:
        return None

    try:
        parsed = urlparse(value)
        path = unquote(parsed.path or "")
    except Exception:
        path = value.split("?", 1)[0].split("#", 1)[0]

    path = path.rstrip("/")

    if not path.startswith("/feed/"):
        return None

    parts = [
        part
        for part in path.split("/")
        if part
    ]

    if len(parts) < 2:
        return None

    # /feed/page/6 is pagination, not a post.
    if len(parts) >= 3 and parts[1].lower() == "page":
        if len(parts) == 3:
            return None
        return parts[-1]

    return parts[-1]


def canonical_post_url(
    slug: Any,
    base_url: str,
) -> Optional[str]:
    """
    Build the site's canonical individual post URL.

    `base_url` may be either the site root or the current page URL, so only
    the scheme + host are used when constructing the canonical URL.
    """
    value = _clean_string(slug)

    if not value:
        return None

    try:
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    except Exception:
        origin = ""

    if not origin:
        origin = base_url.rstrip("/")

    return f"{origin}/feed/{value}"


def is_feed_pagination_url(
    url: Any,
) -> bool:
    """
    Return True for /feed/page and /feed/page/N URLs.
    """
    value = _clean_string(url)

    if not value:
        return False

    try:
        path = urlparse(value).path.rstrip("/")
    except Exception:
        path = value.split("?", 1)[0].rstrip("/")

    if path == "/feed/page":
        return True

    return bool(re.fullmatch(r"/feed/page/\d+", path))


# ============================================================================
# RSC pagination extraction
# ============================================================================

def _nearest_href_before_label(
    payload: str,
    label: str,
) -> Optional[str]:
    """
    Find the href belonging to the nearest pagination button before a given
    aria-label.

    In the site's RSC structure, the href is serialized before the button's
    aria-label. Taking the nearest preceding href avoids accidentally pairing
    the Next Page label with the Previous Page href.
    """
    label_marker = f'"aria-label":"{label}"'
    label_index = payload.find(label_marker)

    if label_index == -1:
        return None

    href_marker = '"href":"'
    href_index = payload.rfind(href_marker, 0, label_index)

    if href_index == -1:
        return None

    value_start = href_index + len(href_marker)
    value_end = payload.find('"', value_start)

    if value_end == -1:
        return None

    return _clean_string(
        payload[value_start:value_end]
    )


def _extract_rsc_pagination_from_payload(
    payload: str,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract Next Page and Previous Page hrefs from one decoded RSC payload.

    The site serializes pagination roughly as:

        href: "/feed/page/5"
        aria-label: "Previous Page"

        href: "/feed/page/7"
        aria-label: "Next Page"

    We locate the nearest href immediately before each label instead of using
    a broad regex. This matters because the same RSC payload contains many
    normal post hrefs before the pagination component.
    """
    if not payload:
        return None, None

    # Restrict matching to a pagination component when possible. This prevents
    # recommendation/post links elsewhere in a large Flight chunk from being
    # considered.
    pagination_index = payload.find('"aria-label":"Pagination navigation"')

    if pagination_index == -1:
        pagination_index = payload.find(
            '"aria-label":"Pagination navigation"'
        )

    search_payload = (
        payload[pagination_index:]
        if pagination_index >= 0
        else payload
    )

    next_url = _nearest_href_before_label(
        search_payload,
        "Next Page",
    )

    previous_url = _nearest_href_before_label(
        search_payload,
        "Previous Page",
    )

    return next_url, previous_url


def extract_rsc_pagination(
    html: str,
) -> Dict[str, Optional[str]]:
    """
    Extract upstream pagination from all RSC chunks.

    Multiple chunks may contain references or unrelated hrefs. We keep the
    first valid next/previous pagination link and ignore normal post links.
    """
    next_url: Optional[str] = None
    previous_url: Optional[str] = None

    # Next.js can split one large nav RSC record across multiple
    # self.__next_f.push() chunks. Joining the decoded payloads first lets the
    # Previous/Next button pair survive that split.
    combined_payload = "\n".join(
        extract_next_f_payloads(html)
    )

    candidate_next, candidate_previous = (
        _extract_rsc_pagination_from_payload(combined_payload)
    )

    if candidate_next and is_feed_pagination_url(candidate_next):
        next_url = candidate_next

    if (
        candidate_previous
        and is_feed_pagination_url(candidate_previous)
    ):
        previous_url = candidate_previous

    return {
        "next": next_url,
        "previous": previous_url,
    }


def extract_dom_pagination(
    html: str,
    base_url: str,
) -> Dict[str, Optional[str]]:
    """
    DOM pagination fallback.

    RSC is the primary source. This fallback exists for resilience if a future
    upstream response changes how Flight data is emitted.
    """
    if not html:
        return {
            "next": None,
            "previous": None,
        }

    soup = BeautifulSoup(html, "html.parser")

    next_url: Optional[str] = None
    previous_url: Optional[str] = None

    pagination_nodes = soup.find_all(
        "nav",
        attrs={"aria-label": re.compile("pagination", re.I)},
    )

    anchors: List[Any] = []

    for node in pagination_nodes:
        anchors.extend(node.find_all("a", href=True))

    if not anchors:
        anchors = soup.find_all("a", href=True)

    for anchor in anchors:
        href = _clean_string(anchor.get("href"))

        if not href:
            continue

        text = _safe_lower(anchor.get_text(" ", strip=True))
        aria_label = _safe_lower(anchor.get("aria-label"))

        button = anchor.find("button")

        if button is not None:
            aria_label = (
                aria_label
                or _safe_lower(button.get("aria-label"))
            )

        absolute_href = _absolute_or_original(href, base_url)

        if not absolute_href:
            continue

        if not is_feed_pagination_url(absolute_href):
            continue

        if "next" in text or "next" in aria_label:
            next_url = absolute_href

        if (
            "previous" in text
            or "prev" in text
            or "previous" in aria_label
            or "prev" in aria_label
        ):
            previous_url = absolute_href

    return {
        "next": next_url,
        "previous": previous_url,
    }


def build_api_pagination(
    upstream_pagination: Dict[str, Optional[str]],
    api_base_url: str,
) -> Dict[str, Optional[str]]:
    """
    Convert upstream /feed/page/N pagination into OUR API pagination.

    Examples:
        upstream /feed/page/6
        -> https://your-api.example/api/feed/6

        upstream /feed
        -> https://your-api.example/api/feed

    `api_base_url` is the public base URL of the deployed scraper.
    """
    result = {
        "next": None,
        "previous": None,
    }

    for key in ("next", "previous"):
        upstream_url = _clean_string(upstream_pagination.get(key))

        if not upstream_url:
            continue

        try:
            path = urlparse(upstream_url).path.rstrip("/")
        except Exception:
            path = upstream_url.split("?", 1)[0].rstrip("/")

        if path == "/feed":
            result[key] = f"{api_base_url.rstrip('/')}/api/feed"
            continue

        page_match = re.fullmatch(r"/feed/page/(\d+)", path)

        if page_match:
            page_number = int(page_match.group(1))
            result[key] = (
                f"{api_base_url.rstrip('/')}/api/feed/{page_number}"
            )

    return result


# ============================================================================
# Public post/item builders
# ============================================================================

def _first_video_legacy_fields(
    media: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Preserve the Phase 1/2 compatibility fields using the first video.
    """
    videos = get_video_media(media)

    if not videos:
        return {
            "embed_url": None,
            "video_id": None,
            "video_url": None,
            "thumbnail": None,
            "duration": None,
        }

    first = videos[0]

    return {
        "embed_url": first.get("embed_url"),
        "video_id": first.get("video_id"),
        "video_url": first.get("video_url"),
        "thumbnail": first.get("thumbnail"),
        "duration": first.get("duration"),
    }


def build_public_item(
    feed_object: Dict[str, Any],
    base_url: str,
) -> Dict[str, Any]:
    """
    Convert one raw RSC feed object into the Phase 3 public item schema.
    """
    media = get_feed_media_items(feed_object)

    video_media = get_video_media(media)
    image_media = get_image_media(media)

    slug = _clean_string(feed_object.get("slug"))

    item: Dict[str, Any] = {
        "title": _clean_string(feed_object.get("title")),
        "slug": slug,
        "url": canonical_post_url(slug, base_url),
        "type": classify_media(media),
        "media_count": len(media),
        "video_count": len(video_media),
        "image_count": len(image_media),
        "media": media,
    }

    item.update(_first_video_legacy_fields(media))

    return item


def build_public_post(
    feed_object: Dict[str, Any],
    base_url: str,
) -> Dict[str, Any]:
    """
    Build the individual-post response from the same normalized media model.

    Additional feed metadata is included when available, while keeping the
    media representation identical to the feed endpoint.
    """
    public_item = build_public_item(feed_object, base_url)

    return {
        **public_item,
        "feed_id": _clean_string(feed_object.get("_id")),
        "channel_id": _clean_string(feed_object.get("channelId")),
        "channel_name": _clean_string(feed_object.get("channelName")),
        "username": _clean_string(feed_object.get("username")),
        "avatar": _clean_string(feed_object.get("avatar")),
        "created_at": _clean_string(feed_object.get("createdAt")),
    }


# ============================================================================
# Feed anchor extraction
# ============================================================================

def extract_feed_post_slugs_from_dom(
    html: str,
) -> List[str]:
    """
    Extract post slugs from actual feed links.

    Only /feed/{slug} and /feed/page/N/{slug} are accepted. Pagination,
    navigation, footer links, and unrelated paths are ignored.
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    slugs: List[str] = []

    for anchor in soup.find_all("a", href=True):
        href = _clean_string(anchor.get("href"))

        if not href:
            continue

        slug = extract_slug_from_url(href)

        if not slug:
            continue

        if slug not in slugs:
            slugs.append(slug)

    return slugs


def _feed_objects_by_slug(
    feed_objects: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """
    Map feed objects by slug.
    """
    output: Dict[str, Dict[str, Any]] = {}

    for feed in feed_objects:
        slug = _clean_string(feed.get("slug"))

        if not slug:
            continue

        if slug not in output:
            output[slug] = feed

    return output


def _select_primary_post_object(
    feed_objects: List[Dict[str, Any]],
    requested_slug: str,
) -> Optional[Dict[str, Any]]:
    """
    Select the requested post from an individual post page.

    This is important because individual post RSC data can also contain
    recommendation objects under "You might like".
    """
    target = _clean_string(requested_slug)

    if not target:
        return None

    for feed in feed_objects:
        if _clean_string(feed.get("slug")) == target:
            return feed

    return None


# ============================================================================
# Page title extraction
# ============================================================================

def extract_page_title(
    html: str,
) -> Optional[str]:
    """
    Extract the rendered HTML title.
    """
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    title = soup.find("title")

    if title is None:
        return None

    value = title.get_text(" ", strip=True)

    return value or None


def extract_canonical_url(
    html: str,
    base_url: str,
) -> Optional[str]:
    """
    Extract canonical URL when available.
    """
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    canonical = soup.find(
        "link",
        attrs={"rel": lambda value: value and "canonical" in value},
    )

    if canonical is None:
        return None

    return _absolute_or_original(
        canonical.get("href"),
        base_url,
    )


# ============================================================================
# Feed page parser
# ============================================================================

def parse_feed_page(
    html: str,
    base_url: str,
    api_base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Parse one /feed or /feed/page/N response.

    Media comes from RSC feed objects.

    Pagination priority:
        1. RSC pagination
        2. DOM pagination fallback

    The returned pagination is converted to OUR API URLs when api_base_url is
    supplied.
    """
    if not html:
        empty_pagination = {
            "next": None,
            "previous": None,
        }

        return {
            "title": None,
            "count": 0,
            "items": [],
            "pagination": empty_pagination,
        }

    title = extract_page_title(html)

    feed_objects = extract_rsc_feed_objects(html)
    feeds_by_slug = _feed_objects_by_slug(feed_objects)

    post_slugs = extract_feed_post_slugs_from_dom(html)

    items: List[Dict[str, Any]] = []
    seen_slugs = set()

    # Preferred order: DOM gives us the actual visual feed order, while RSC
    # gives us the complete media data.
    for slug in post_slugs:
        if slug in seen_slugs:
            continue

        feed_object = feeds_by_slug.get(slug)

        if feed_object is None:
            continue

        seen_slugs.add(slug)
        items.append(build_public_item(feed_object, base_url))

    # Fallback for RSC-only responses where the HTML does not expose normal
    # feed anchors.
    if not items:
        for feed_object in feed_objects:
            slug = _clean_string(feed_object.get("slug"))

            if not slug or slug in seen_slugs:
                continue

            seen_slugs.add(slug)
            items.append(build_public_item(feed_object, base_url))

    upstream_pagination = extract_rsc_pagination(html)

    if not (
        upstream_pagination.get("next")
        or upstream_pagination.get("previous")
    ):
        upstream_pagination = extract_dom_pagination(
            html,
            base_url,
        )

    if api_base_url:
        public_pagination = build_api_pagination(
            upstream_pagination,
            api_base_url,
        )
    else:
        public_pagination = upstream_pagination

    return {
        "title": title,
        "count": len(items),
        "items": items,
        "pagination": public_pagination,
    }


# ============================================================================
# Individual post parser
# ============================================================================

def parse_post_page(
    html: str,
    requested_slug: str,
    base_url: str,
) -> Optional[Dict[str, Any]]:
    """
    Parse one individual /feed/{slug} page.

    Only the feed object whose slug exactly matches requested_slug is returned.
    Recommendation objects are ignored.
    """
    if not html:
        return None

    feed_objects = extract_rsc_feed_objects(html)

    primary = _select_primary_post_object(
        feed_objects,
        requested_slug,
    )

    if primary is None:
        return None

    return build_public_post(
        primary,
        base_url,
    )


# ============================================================================
# Compatibility aliases
# ============================================================================

def extract_rsc_media(
    html: str,
) -> List[Dict[str, Any]]:
    """
    Backward-compatible helper retained for code that imported this name.
    """
    output: List[Dict[str, Any]] = []

    for feed in extract_rsc_feed_objects(html):
        media = get_feed_media_items(feed)

        slug = _clean_string(feed.get("slug"))

        output.append(
            {
                "slug": slug,
                "media": media,
                "media_count": len(media),
                "video_count": len(get_video_media(media)),
                "image_count": len(get_image_media(media)),
                "type": classify_media(media),
            }
        )

    return output


def get_video_media_items(
    feed_object: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Compatibility alias.
    """
    return get_video_media(
        get_feed_media_items(feed_object)
    )


def get_image_media_items(
    feed_object: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Compatibility alias.
    """
    return get_image_media(
        get_feed_media_items(feed_object)
    )


def parse_page(
    html: str,
    base_url: str,
    api_base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Phase 2/3 public entry point.

    Existing callers can continue importing parse_page().
    """
    return parse_feed_page(
        html=html,
        base_url=base_url,
        api_base_url=api_base_url,
    )


# ============================================================================
# Debug / inspection helpers
# ============================================================================

def summarize_feed_objects(
    html: str,
) -> Dict[str, Any]:
    """
    Small internal/debug summary useful during deployment testing.
    """
    feed_objects = extract_rsc_feed_objects(html)

    summaries: List[Dict[str, Any]] = []

    for feed in feed_objects:
        media = get_feed_media_items(feed)

        summaries.append(
            {
                "slug": _clean_string(feed.get("slug")),
                "media_count": len(media),
                "video_count": len(get_video_media(media)),
                "image_count": len(get_image_media(media)),
                "type": classify_media(media),
            }
        )

    return {
        "feed_object_count": len(feed_objects),
        "feeds": summaries,
    }


def validate_media_output(
    media: Any,
) -> bool:
    """
    Validate the normalized public media shape without requiring a model
    library or database.
    """
    if not isinstance(media, list):
        return False

    expected_positions = list(range(len(media)))

    actual_positions = [
        item.get("position")
        for item in media
        if isinstance(item, dict)
    ]

    if actual_positions != expected_positions:
        return False

    for item in media:
        if not isinstance(item, dict):
            return False

        media_type = item.get("type")

        if media_type == "video":
            required_keys = {
                "position",
                "type",
                "id",
                "embed_url",
                "video_id",
                "video_url",
                "thumbnail",
                "duration",
            }

            if not required_keys.issubset(item.keys()):
                return False

        elif media_type == "image":
            required_keys = {
                "position",
                "type",
                "id",
                "url",
            }

            if not required_keys.issubset(item.keys()):
                return False

        else:
            return False

    return True


def validate_public_item(
    item: Any,
) -> bool:
    """
    Validate the Phase 3 feed item structure.
    """
    if not isinstance(item, dict):
        return False

    required = {
        "title",
        "slug",
        "url",
        "type",
        "media_count",
        "video_count",
        "image_count",
        "media",
        "embed_url",
        "video_id",
        "video_url",
        "thumbnail",
        "duration",
    }

    if not required.issubset(item.keys()):
        return False

    if not validate_media_output(item.get("media")):
        return False

    media = item["media"]

    if item["media_count"] != len(media):
        return False

    video_count = sum(
        1 for entry in media
        if entry.get("type") == "video"
    )

    image_count = sum(
        1 for entry in media
        if entry.get("type") == "image"
    )

    if item["video_count"] != video_count:
        return False

    if item["image_count"] != image_count:
        return False

    if item["type"] != classify_media(media):
        return False

    return True


def validate_pagination_output(
    pagination: Any,
) -> bool:
    """
    Validate the public pagination shape.
    """
    if not isinstance(pagination, dict):
        return False

    if "next" not in pagination:
        return False

    if "previous" not in pagination:
        return False

    for key in ("next", "previous"):
        value = pagination.get(key)

        if value is not None and not isinstance(value, str):
            return False

    return True


# ============================================================================
# Safe conversion helpers for future schema extensions
# ============================================================================

def optional_metadata(
    feed_object: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Return common metadata without changing the Phase 2 feed item schema.

    This helper is intentionally separate so future phases can add metadata
    without rewriting media parsing.
    """
    if not isinstance(feed_object, dict):
        return {}

    return {
        "feed_id": _clean_string(feed_object.get("_id")),
        "channel_id": _clean_string(feed_object.get("channelId")),
        "channel_name": _clean_string(feed_object.get("channelName")),
        "username": _clean_string(feed_object.get("username")),
        "avatar": _clean_string(feed_object.get("avatar")),
        "created_at": _clean_string(feed_object.get("createdAt")),
    }


def merge_optional_metadata(
    item: Dict[str, Any],
    feed_object: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Return a copied item with common metadata attached.
    """
    merged = dict(item)
    merged.update(optional_metadata(feed_object))
    return merged


# ============================================================================
# Defensive parsing helpers
# ============================================================================

def _is_valid_slug(
    slug: Any,
) -> bool:
    """
    Basic slug validation used only for route matching.
    """
    value = _clean_string(slug)

    if not value:
        return False

    return bool(SLUG_SAFE_PATTERN.fullmatch(value))


def normalize_requested_slug(
    slug: Any,
) -> Optional[str]:
    """
    Normalize a FastAPI path slug without altering the actual slug text.
    """
    value = _clean_string(slug)

    if not value:
        return None

    value = unquote(value).strip("/")

    if not value:
        return None

    return value


def is_likely_post_slug(
    slug: Any,
) -> bool:
    """
    Reject obvious feed/system paths from post selection.
    """
    value = normalize_requested_slug(slug)

    if not value:
        return False

    if value.lower() in {
        "page",
        "dmca",
        "contact-us",
        "privacy-policy",
    }:
        return False

    return _is_valid_slug(value)


# ============================================================================
# Final public parser contract
# ============================================================================

__all__ = [
    "build_api_pagination",
    "build_public_item",
    "build_public_post",
    "canonical_post_url",
    "classify_media",
    "extract_canonical_url",
    "extract_dom_pagination",
    "extract_feed_post_slugs_from_dom",
    "extract_next_f_payloads",
    "extract_rsc_feed_objects",
    "extract_rsc_media",
    "extract_rsc_pagination",
    "extract_slug_from_url",
    "get_feed_media_items",
    "get_image_media",
    "get_image_media_items",
    "get_video_media",
    "get_video_media_items",
    "normalize_media_item",
    "normalize_media_items",
    "normalize_requested_slug",
    "optional_metadata",
    "parse_feed_page",
    "parse_page",
    "parse_post_page",
    "summarize_feed_objects",
    "validate_media_output",
    "validate_pagination_output",
    "validate_public_item",
]
