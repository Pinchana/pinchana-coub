"""Coub scraper service backed by the public v2 metadata endpoint."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pinchana_core.models import ScrapeRequest, ScrapeResponse
from pinchana_core.plugins import ScraperPlugin, registry
from pinchana_core.storage import MediaStorage

from .scraper import (
    CoubClient,
    CoubMetadata,
    CoubNotFoundError,
    CoubRestrictedError,
    CoubSchemaError,
    CoubUpstreamError,
    extract_permalink,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
client = CoubClient()
storage = MediaStorage(
    base_path=os.getenv("CACHE_PATH", "./cache"),
    max_size_gb=float(os.getenv("CACHE_MAX_SIZE_GB", "10.0")),
)
COUB_CACHE_VERSION = 1
FFMPEG_TIMEOUT_SECONDS = float(os.getenv("COUB_FFMPEG_TIMEOUT_SECONDS", "300"))


class CoubScrapeResponse(ScrapeResponse):
    username: str | None = None
    author_name: str | None = None
    created_at: str | None = None


class MediaDownloadError(RuntimeError):
    """Required Coub media could not be persisted."""


class MuxError(RuntimeError):
    """ffmpeg could not build a user-ready looped video."""


def _media_path_from_url(value: str | None) -> Path | None:
    if not value or not value.startswith("/media/coub/"):
        return None
    relative = value.split("?", 1)[0].removeprefix("/media/coub/")
    parts = relative.split("/", 1)
    if len(parts) != 2 or not all(parts):
        return None
    return storage.base_path / parts[0] / parts[1]


def _cached_media_ready(metadata: dict) -> bool:
    if not isinstance(metadata, dict) or metadata.get("_cache_version") != COUB_CACHE_VERSION:
        return False
    video = _media_path_from_url(metadata.get("video_url"))
    if video is None or not video.is_file() or video.stat().st_size == 0:
        return False
    thumbnail_url = metadata.get("thumbnail_url")
    if thumbnail_url:
        thumbnail = _media_path_from_url(thumbnail_url)
        if thumbnail is None or not thumbnail.is_file() or thumbnail.stat().st_size == 0:
            return False
    return True


def _response(
    metadata: CoubMetadata,
    *,
    thumbnail: bool,
    downloaded: bool,
    duration: float | None = None,
) -> CoubScrapeResponse:
    duration = metadata.duration if duration is None else duration
    return CoubScrapeResponse(
        shortcode=metadata.permalink,
        caption=metadata.title,
        title=metadata.title,
        author=metadata.author,
        username=metadata.username,
        author_name=metadata.author,
        media_type="video",
        thumbnail_url=(
            f"/media/coub/{metadata.permalink}/thumbnail.jpg" if thumbnail else (metadata.picture or "")
        ),
        video_url=f"/media/coub/{metadata.permalink}/video.mp4" if downloaded else None,
        duration=max(0, round(duration)) if duration is not None else None,
        created_at=metadata.created_at,
    )


async def _run_ffmpeg(arguments: list[str], output: Path) -> None:
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.part.mp4")
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *arguments,
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "-y",
        str(temporary),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise MuxError("Coub mux timed out") from exc
    except FileNotFoundError as exc:
        raise MuxError("ffmpeg is not installed") from exc

    if process.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        detail = stderr.decode("utf-8", errors="replace")[-500:]
        raise MuxError(f"ffmpeg failed to mux Coub media: {detail}")
    temporary.replace(output)


async def _loop_and_mux(video: Path, audio: Path, output: Path) -> str:
    base = [
        "-stream_loop",
        "-1",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-shortest",
    ]
    try:
        await _run_ffmpeg([*base, "-c", "copy"], output)
        return "stream_copy"
    except MuxError as copy_error:
        logger.warning("coub_mux_copy_failed error=%s; retrying with AAC audio", copy_error)
        await _run_ffmpeg([*base, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"], output)
        return "audio_transcode"


async def _probe_duration(path: Path) -> float | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except (FileNotFoundError, TimeoutError):
        logger.warning("coub_duration_probe_unavailable path=%s", path)
        return None
    if process.returncode != 0:
        logger.warning(
            "coub_duration_probe_failed path=%s error=%s",
            path,
            stderr.decode("utf-8", errors="replace")[-300:],
        )
        return None
    try:
        duration = float(stdout.strip())
    except ValueError:
        return None
    return duration if duration >= 0 else None


async def _download(metadata: CoubMetadata) -> CoubScrapeResponse:
    storage.prepare_post_dir(metadata.permalink)
    post_dir = storage._post_dir(metadata.permalink)
    output = storage.video_path(metadata.permalink)
    thumbnail = storage.thumbnail_path(metadata.permalink)

    thumbnail_ok = False
    if metadata.picture:
        thumbnail_ok = await storage.download(metadata.picture, thumbnail)

    strategy = ""
    if metadata.video and metadata.audio:
        source_video = post_dir / "source-video.mp4"
        source_audio = post_dir / "source-audio.mp3"
        downloaded = await asyncio.gather(
            storage.download(metadata.video.url, source_video),
            storage.download(metadata.audio.url, source_audio),
        )
        if all(downloaded):
            try:
                mux_mode = await _loop_and_mux(source_video, source_audio, output)
                strategy = f"assembled_{mux_mode}"
            except MuxError as exc:
                logger.error("coub_best_quality_mux_failed id=%s error=%s", metadata.permalink, exc)
            finally:
                source_video.unlink(missing_ok=True)
                source_audio.unlink(missing_ok=True)
        else:
            logger.error("coub_best_quality_download_failed id=%s results=%s", metadata.permalink, downloaded)

    if not output.is_file() and metadata.share_url:
        if not await storage.download(metadata.share_url, output):
            raise MediaDownloadError("Coub share video download failed")
        strategy = "share_fallback"

    if not output.is_file() and metadata.video:
        if not await storage.download(metadata.video.url, output):
            raise MediaDownloadError("Coub video download failed")
        strategy = "video_only"

    if not output.is_file() or output.stat().st_size == 0:
        raise MediaDownloadError("Coub metadata contained no downloadable video")

    logger.info(
        "coub_media_ready id=%s strategy=%s bytes=%d thumbnail=%s",
        metadata.permalink,
        strategy,
        output.stat().st_size,
        thumbnail_ok,
    )
    output_duration = await _probe_duration(output)
    response = _response(
        metadata,
        thumbnail=thumbnail_ok,
        downloaded=True,
        duration=output_duration,
    )
    payload = response.model_dump()
    payload["_cache_version"] = COUB_CACHE_VERSION
    payload["_delivery_strategy"] = strategy
    storage.save_metadata(metadata.permalink, payload)
    return response


def _map_error(error: Exception) -> HTTPException:
    if isinstance(error, CoubNotFoundError):
        return HTTPException(status_code=404, detail={"code": "not_found", "message": str(error)})
    if isinstance(error, CoubRestrictedError):
        return HTTPException(status_code=403, detail={"code": "restricted_media", "message": str(error)})
    if isinstance(error, CoubSchemaError):
        logger.error("coub_schema_error error=%s", error)
        return HTTPException(status_code=502, detail={"code": "extraction_failed", "message": str(error)})
    if isinstance(error, CoubUpstreamError):
        logger.warning("coub_upstream_error error=%s", error)
        return HTTPException(status_code=503, detail={"code": "service_unavailable", "message": str(error)})
    if isinstance(error, MediaDownloadError):
        return HTTPException(status_code=503, detail={"code": "media_download_failed", "message": str(error)})
    logger.exception("coub_unexpected_error", exc_info=error)
    return HTTPException(status_code=500, detail={"code": "extraction_failed", "message": "Coub extraction failed"})


async def _metadata_for_url(url: str) -> CoubMetadata:
    try:
        permalink = extract_permalink(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return await client.fetch(permalink)
    except (CoubNotFoundError, CoubRestrictedError, CoubSchemaError, CoubUpstreamError) as exc:
        raise _map_error(exc) from exc


async def _scrape(url: str, permalink: str) -> CoubScrapeResponse:
    cached = storage.load_metadata(permalink) if storage.is_cached(permalink) else None
    if cached and _cached_media_ready(cached):
        logger.info("coub_cache_hit id=%s", permalink)
        return CoubScrapeResponse(**cached)

    metadata = await _metadata_for_url(url)
    try:
        return await _download(metadata)
    except MediaDownloadError as exc:
        raise _map_error(exc) from exc
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/scrape", response_model=CoubScrapeResponse)
async def process_scrape_request(request: ScrapeRequest):
    url = str(request.url)
    try:
        permalink = extract_permalink(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await storage.singleflight(permalink, lambda: _scrape(url, permalink))


@router.post("/inspect", response_model=CoubScrapeResponse)
async def inspect_coub(request: ScrapeRequest):
    metadata = await _metadata_for_url(str(request.url))
    return _response(metadata, thumbnail=False, downloaded=False)


@router.get("/media/coub/{permalink}/{filename}")
async def serve_media(permalink: str, filename: str):
    if filename not in {"video.mp4", "thumbnail.jpg"}:
        raise HTTPException(status_code=404, detail="File not found")
    path = storage._post_dir(permalink) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@router.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "coub",
        "metadata_source": "coub_api_v2",
        "ffmpeg": shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None,
    }


registry.register(
    ScraperPlugin(
        name="coub",
        router=router,
        route_patterns=["coub.com/view/", "coub.com/embed/"],
    )
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        await client.close()
        await storage.close()


app = FastAPI(title="Pinchana Coub", version="26.9.1", lifespan=lifespan)
app.include_router(router)
