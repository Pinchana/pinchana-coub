from __future__ import annotations

from pathlib import Path

import pytest

from pinchana_coub import main as main_module
from pinchana_coub.scraper import CoubMetadata, MediaSource


def metadata(*, audio=True, share=True):
    return CoubMetadata(
        permalink="abc123",
        title="A title",
        author="Channel",
        username="channel",
        picture="https://cdn.example/poster.jpg",
        duration=9.75,
        created_at="2026-09-01T10:00:00Z",
        video=MediaSource("high", "https://cdn.example/video.mp4", 200),
        audio=MediaSource("high", "https://cdn.example/audio.mp3", 100) if audio else None,
        share_url="https://cdn.example/share.mp4" if share else None,
    )


@pytest.mark.asyncio
async def test_best_quality_sources_are_assembled(monkeypatch, tmp_path):
    calls = []

    async def download(url: str, destination: Path):
        calls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"source")
        return True

    async def mux(video: Path, audio: Path, output: Path):
        assert video.name == "source-video.mp4"
        assert audio.name == "source-audio.mp3"
        output.write_bytes(b"ready-video")
        return "stream_copy"

    monkeypatch.setattr(main_module.storage, "base_path", tmp_path)
    monkeypatch.setattr(main_module.storage, "download", download)
    monkeypatch.setattr(main_module, "_loop_and_mux", mux)
    monkeypatch.setattr(main_module, "_probe_duration", lambda _path: async_value(59.0))

    response = await main_module._download(metadata())

    assert response.video_url == "/media/coub/abc123/video.mp4"
    assert response.duration == 59
    assert calls == [
        "https://cdn.example/poster.jpg",
        "https://cdn.example/video.mp4",
        "https://cdn.example/audio.mp3",
    ]
    saved = main_module.storage.load_metadata("abc123")
    assert saved["_delivery_strategy"] == "assembled_stream_copy"
    assert not (tmp_path / "abc123" / "source-video.mp4").exists()


@pytest.mark.asyncio
async def test_share_is_used_when_best_quality_mux_fails(monkeypatch, tmp_path):
    async def download(url: str, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(url.encode())
        return True

    async def mux(*_args):
        raise main_module.MuxError("broken")

    monkeypatch.setattr(main_module.storage, "base_path", tmp_path)
    monkeypatch.setattr(main_module.storage, "download", download)
    monkeypatch.setattr(main_module, "_loop_and_mux", mux)
    monkeypatch.setattr(main_module, "_probe_duration", lambda _path: async_value(20.0))

    await main_module._download(metadata())

    saved = main_module.storage.load_metadata("abc123")
    assert saved["_delivery_strategy"] == "share_fallback"
    assert (tmp_path / "abc123" / "video.mp4").read_bytes() == b"https://cdn.example/share.mp4"


@pytest.mark.asyncio
async def test_video_is_saved_directly_when_audio_and_share_are_absent(monkeypatch, tmp_path):
    async def download(url: str, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"media")
        return True

    monkeypatch.setattr(main_module.storage, "base_path", tmp_path)
    monkeypatch.setattr(main_module.storage, "download", download)
    monkeypatch.setattr(main_module, "_probe_duration", lambda _path: async_value(9.75))

    await main_module._download(metadata(audio=False, share=False))

    assert main_module.storage.load_metadata("abc123")["_delivery_strategy"] == "video_only"


@pytest.mark.asyncio
async def test_cached_response_skips_metadata_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module.storage, "base_path", tmp_path)
    post_dir = tmp_path / "abc123"
    post_dir.mkdir()
    (post_dir / "video.mp4").write_bytes(b"cached")
    main_module.storage.save_metadata(
        "abc123",
        {
            "_cache_version": main_module.COUB_CACHE_VERSION,
            "shortcode": "abc123",
            "caption": "Cached",
            "author": "Channel",
            "media_type": "video",
            "thumbnail_url": "",
            "video_url": "/media/coub/abc123/video.mp4",
        },
    )

    async def unexpected_fetch(_permalink):
        raise AssertionError("metadata should not be fetched on a valid cache hit")

    monkeypatch.setattr(main_module.client, "fetch", unexpected_fetch)
    response = await main_module._scrape("https://coub.com/view/abc123", "abc123")
    assert response.caption == "Cached"


@pytest.mark.asyncio
async def test_health_reports_ffmpeg(monkeypatch):
    async def vpn_status():
        return {"status": "running", "dns": {"status": "running"}}

    monkeypatch.setattr(main_module.gluetun, "enabled", True)
    monkeypatch.setattr(main_module.gluetun, "get_vpn_status", vpn_status)
    monkeypatch.setattr(main_module.shutil, "which", lambda command: "/usr/bin/ffmpeg")
    assert await main_module.health_check() == {
        "status": "healthy",
        "service": "coub",
        "vpn": {"status": "running", "dns": {"status": "running"}},
        "metadata_source": "coub_api_v2",
        "ffmpeg": True,
    }


@pytest.mark.asyncio
async def test_health_fails_when_vpn_dns_is_degraded(monkeypatch):
    async def vpn_status():
        return {"status": "degraded", "vpn_status": "running"}

    monkeypatch.setattr(main_module.gluetun, "enabled", True)
    monkeypatch.setattr(main_module.gluetun, "get_vpn_status", vpn_status)

    with pytest.raises(main_module.HTTPException) as exc_info:
        await main_module.health_check()

    assert exc_info.value.status_code == 503


async def async_value(value):
    return value
