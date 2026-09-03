"""Direct client and schema parser for Coub's public v2 endpoint."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

logger = logging.getLogger(__name__)

COUB_API_BASE = "https://coub.com/api/v2/coubs"
COUB_HOSTS = {"coub.com", "www.coub.com", "m.coub.com"}
COUB_PATH_RE = re.compile(r"^/(?:view|embed)/([A-Za-z0-9_-]+)/?$", re.IGNORECASE)
QUALITY_ORDER = ("higher", "high", "med", "low")


class CoubError(RuntimeError):
    """Base class for expected Coub extraction failures."""


class CoubNotFoundError(CoubError):
    """The requested Coub does not exist or was removed."""


class CoubRestrictedError(CoubError):
    """The requested Coub is private or access-restricted."""


class CoubUpstreamError(CoubError):
    """Coub could not be reached or returned a temporary error."""


class CoubSchemaError(CoubError):
    """Coub returned JSON that no longer matches the supported schema."""


@dataclass(frozen=True)
class MediaSource:
    quality: str
    url: str
    size: int | None = None


@dataclass(frozen=True)
class CoubMetadata:
    permalink: str
    title: str
    author: str
    username: str | None
    picture: str | None
    duration: float | None
    created_at: str | None
    video: MediaSource | None
    audio: MediaSource | None
    share_url: str | None


def extract_permalink(url: str) -> str:
    """Return a permalink from a public single-Coub view or embed URL."""
    parsed = urlsplit(str(url))
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or host not in COUB_HOSTS:
        raise ValueError("Only public coub.com URLs are supported")
    match = COUB_PATH_RE.fullmatch(unquote(parsed.path))
    if not match:
        raise ValueError("Expected a coub.com/view/{permalink} URL")
    return match.group(1)


def _source(value: Any, quality: str) -> MediaSource | None:
    if isinstance(value, str):
        url = value.strip()
        return MediaSource(quality=quality, url=url) if url else None
    if not isinstance(value, dict):
        return None
    url = value.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    size = value.get("size")
    try:
        normalized_size = int(size) if size is not None else None
    except (TypeError, ValueError):
        normalized_size = None
    return MediaSource(quality=quality, url=url, size=normalized_size)


def _best_source(versions: Any) -> MediaSource | None:
    if not isinstance(versions, dict):
        return None
    for quality in QUALITY_ORDER:
        source = _source(versions.get(quality), quality)
        if source:
            return source
    return None


def parse_metadata(payload: Any, requested_permalink: str) -> CoubMetadata:
    """Validate the fields required to produce a user-ready video."""
    if not isinstance(payload, dict):
        raise CoubSchemaError("Coub metadata response is not a JSON object")

    if payload.get("banned") is True:
        raise CoubNotFoundError("This Coub was removed")
    if payload.get("visibility_type") not in {None, "public"}:
        raise CoubRestrictedError("This Coub is private or restricted")
    if payload.get("age_restricted") is True or payload.get("age_restricted_by_admin") is True:
        raise CoubRestrictedError("This Coub is restricted")

    permalink = payload.get("permalink")
    if not isinstance(permalink, str) or not permalink:
        raise CoubSchemaError("Coub metadata is missing its permalink")

    file_versions = payload.get("file_versions")
    if not isinstance(file_versions, dict):
        raise CoubSchemaError("Coub metadata is missing file_versions")
    html5 = file_versions.get("html5")
    if not isinstance(html5, dict):
        raise CoubSchemaError("Coub metadata is missing file_versions.html5")

    video = _best_source(html5.get("video"))
    audio = _best_source(html5.get("audio"))
    share = file_versions.get("share")
    share_url = share.get("default") if isinstance(share, dict) else None
    if not isinstance(share_url, str) or not share_url.strip():
        share_url = None
    if video is None and share_url is None:
        raise CoubSchemaError("Coub metadata contains no supported video URL")

    channel = payload.get("channel")
    channel = channel if isinstance(channel, dict) else {}
    title = payload.get("title")
    duration = payload.get("duration")
    try:
        normalized_duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        normalized_duration = None

    logger.info(
        "coub_media_candidates requested=%s canonical=%s video_quality=%s "
        "video_size=%s audio_quality=%s audio_size=%s share_available=%s",
        requested_permalink,
        permalink,
        video.quality if video else None,
        video.size if video else None,
        audio.quality if audio else None,
        audio.size if audio else None,
        bool(share_url),
    )

    return CoubMetadata(
        permalink=permalink,
        title=title.strip() if isinstance(title, str) and title.strip() else permalink,
        author=str(channel.get("title") or channel.get("permalink") or ""),
        username=str(channel["permalink"]) if channel.get("permalink") else None,
        picture=str(payload["picture"]) if payload.get("picture") else None,
        duration=normalized_duration,
        created_at=str(payload.get("published_at") or payload.get("created_at") or "") or None,
        video=video,
        audio=audio,
        share_url=share_url,
    )


class CoubClient:
    """Fetch public Coub metadata without browser automation."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owns_client = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Pinchana-Coub/1.0",
                },
            )
        return self._client

    async def fetch(self, permalink: str) -> CoubMetadata:
        url = f"{COUB_API_BASE}/{permalink}.json"
        try:
            response = await (await self._http()).get(url)
        except httpx.TimeoutException as exc:
            raise CoubUpstreamError("Coub metadata request timed out") from exc
        except httpx.RequestError as exc:
            raise CoubUpstreamError("Coub metadata request failed") from exc

        if response.status_code in {404, 410}:
            raise CoubNotFoundError("This Coub was not found or has been removed")
        if response.status_code in {401, 403, 451}:
            raise CoubRestrictedError("This Coub is private or restricted")
        if response.status_code == 429:
            raise CoubUpstreamError("Coub is rate limiting metadata requests")
        if response.status_code >= 500:
            raise CoubUpstreamError(f"Coub metadata API returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise CoubUpstreamError(f"Coub metadata API returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise CoubSchemaError("Coub metadata API returned invalid JSON") from exc
        try:
            return parse_metadata(payload, permalink)
        except CoubSchemaError:
            logger.exception(
                "coub_schema_change permalink=%s top_level_keys=%s",
                permalink,
                sorted(str(key) for key in payload)[:40] if isinstance(payload, dict) else None,
            )
            raise

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
