#!/usr/bin/env python3
"""Inspect current Coub media candidates without downloading full assets."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from typing import Any

import httpx


async def probe(url: str) -> dict[str, Any] | None:
    if shutil.which("ffprobe") is None:
        return None
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,size,bit_rate:stream=codec_name,codec_type,width,height,bit_rate",
        "-of",
        "json",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        return {"error": stderr.decode("utf-8", errors="replace")[-300:]}
    return json.loads(stdout)


async def inspect(permalinks: list[str]) -> list[dict[str, Any]]:
    results = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for permalink in permalinks:
            response = await client.get(f"https://coub.com/api/v2/coubs/{permalink}.json")
            response.raise_for_status()
            payload = response.json()
            versions = payload.get("file_versions") or {}
            html5 = versions.get("html5") or {}
            video = next(
                (html5.get("video", {}).get(key) for key in ("higher", "high", "med", "low") if html5.get("video", {}).get(key)),
                None,
            )
            audio = next(
                (html5.get("audio", {}).get(key) for key in ("higher", "high", "med", "low") if html5.get("audio", {}).get(key)),
                None,
            )
            share = (versions.get("share") or {}).get("default")
            video_url = video.get("url") if isinstance(video, dict) else video
            audio_url = audio.get("url") if isinstance(audio, dict) else audio
            results.append(
                {
                    "requested": permalink,
                    "canonical": payload.get("permalink"),
                    "duration": payload.get("duration"),
                    "dimensions": payload.get("dimensions"),
                    "candidate_sizes": {
                        "video": video.get("size") if isinstance(video, dict) else None,
                        "audio": audio.get("size") if isinstance(audio, dict) else None,
                    },
                    "probes": {
                        "share": await probe(share) if share else None,
                        "video": await probe(video_url) if video_url else None,
                        "audio": await probe(audio_url) if audio_url else None,
                    },
                }
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("permalink", nargs="+", help="One or more public Coub permalinks")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(inspect(args.permalink)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

