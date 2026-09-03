from __future__ import annotations

import json

import httpx
import pytest

from pinchana_coub.scraper import (
    CoubClient,
    CoubNotFoundError,
    CoubRestrictedError,
    CoubSchemaError,
    CoubUpstreamError,
    extract_permalink,
    parse_metadata,
)


def payload(**overrides):
    base = {
        "permalink": "abc123",
        "title": "A title",
        "duration": 9.75,
        "picture": "https://cdn.example/poster.jpg",
        "published_at": "2026-09-01T10:00:00Z",
        "visibility_type": "public",
        "banned": False,
        "channel": {"title": "Channel Name", "permalink": "channel-name"},
        "file_versions": {
            "html5": {
                "video": {
                    "med": {"url": "https://cdn.example/med.mp4", "size": 100},
                    "higher": {"url": "https://cdn.example/higher.mp4", "size": 300},
                    "high": {"url": "https://cdn.example/high.mp4", "size": 200},
                },
                "audio": {
                    "med": {"url": "https://cdn.example/med.mp3", "size": 20},
                    "high": {"url": "https://cdn.example/high.mp3", "size": 40},
                },
            },
            "share": {"default": "https://cdn.example/share.mp4"},
        },
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://coub.com/view/abc123", "abc123"),
        ("https://www.coub.com/view/ABC_123/?from=share", "ABC_123"),
        ("http://m.coub.com/embed/a-b", "a-b"),
    ],
)
def test_extract_permalink(url, expected):
    assert extract_permalink(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/view/abc123",
        "https://coub.com/random/abc123",
        "https://coub.com/view/abc123/extra",
        "ftp://coub.com/view/abc123",
    ],
)
def test_rejects_non_single_coub_urls(url):
    with pytest.raises(ValueError):
        extract_permalink(url)


def test_selects_best_sources_and_metadata():
    metadata = parse_metadata(payload(), "abc123")

    assert metadata.permalink == "abc123"
    assert metadata.video.quality == "higher"
    assert metadata.video.size == 300
    assert metadata.audio.quality == "high"
    assert metadata.author == "Channel Name"
    assert metadata.username == "channel-name"
    assert metadata.share_url == "https://cdn.example/share.mp4"


def test_share_only_payload_is_supported():
    data = payload()
    data["file_versions"]["html5"] = {"video": {}, "audio": {}}
    metadata = parse_metadata(data, "abc123")
    assert metadata.video is None
    assert metadata.share_url


def test_missing_media_is_schema_error_not_not_found():
    data = payload()
    data["file_versions"] = {"html5": {"video": {}, "audio": {}}}
    with pytest.raises(CoubSchemaError):
        parse_metadata(data, "abc123")


def test_removed_metadata_is_not_found():
    with pytest.raises(CoubNotFoundError):
        parse_metadata(payload(banned=True), "abc123")


@pytest.mark.parametrize("field", ["age_restricted", "age_restricted_by_admin"])
def test_restricted_metadata_is_rejected(field):
    with pytest.raises(CoubRestrictedError):
        parse_metadata(payload(**{field: True}), "abc123")


def client_for(status: int, body=None):
    def handler(request: httpx.Request):
        content = json.dumps(body if body is not None else {}).encode()
        return httpx.Response(status, content=content, headers={"content-type": "application/json"})

    return CoubClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_client_uses_one_public_api_request():
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        return httpx.Response(200, json=payload())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    metadata = await CoubClient(http).fetch("abc123")

    assert metadata.permalink == "abc123"
    assert calls == ["https://coub.com/api/v2/coubs/abc123.json"]
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_client_maps_missing_coub(status):
    with pytest.raises(CoubNotFoundError):
        await client_for(status).fetch("missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 451])
async def test_client_maps_restricted_coub(status):
    with pytest.raises(CoubRestrictedError):
        await client_for(status).fetch("private")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_client_maps_temporary_upstream_failures(status):
    with pytest.raises(CoubUpstreamError):
        await client_for(status).fetch("abc123")
