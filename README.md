# Pinchana Coub

Direct Coub scraper service for public single-Coub URLs. It reads metadata from
`https://coub.com/api/v2/coubs/{permalink}.json`; it does not use a browser or
`yt-dlp`.

## Media behavior

- When separate HTML5 video and audio sources are present, the highest known
  quality (`higher`, `high`, `med`, `low`) is selected. ffmpeg repeats the video
  through the full audio track and muxes the result into one MP4. Stream copy is
  attempted first; only the audio is converted to AAC if direct muxing fails.
- `file_versions.share.default` is a ready-to-use MP4 fallback when source
  download or muxing fails, or when no separate audio track is available.
- A direct video download is the final fallback when there is no share MP4.
- Results are stored in the shared `MediaStorage` cache.

The separate-source path is the default because live inspection on 2026-09-03
found that current share MP4s used the same frame size but a lower video bitrate
and contained only about two visual loops, while the high-quality audio source
was substantially longer. Run `research/inspect_live.py` to repeat that check.

## API

- `POST /scrape` with `{"url":"https://coub.com/view/PERMALINK"}`
- `POST /inspect` for metadata without downloading media
- `GET /media/coub/{permalink}/{filename}` inside the trusted gateway network
- `GET /health` verifies ffmpeg plus shared VPN and DNS readiness

`/embed/{permalink}` URLs are also accepted because they identify the same
public single-Coub resource.

## Development

```sh
uv sync --frozen
uv run pytest
uv run uvicorn pinchana_coub.main:app --host 0.0.0.0 --port 8090 --reload
```

Build the image from the parent `pinchana-api` checkout so the Docker build can
copy `pinchana-core`:

```sh
docker build --file pinchana-coub/Dockerfile --tag pinchana-coub:local .
```
