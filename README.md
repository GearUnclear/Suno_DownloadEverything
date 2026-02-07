# Suno DownloadEverything

Reliable tools to mirror your Suno library locally, keep it synced as new songs appear, and recover missing files.

## What this repo contains

- `progress_check.py`
  - Fetches your feed (with cache and retries), compares API vs local MP3s, and writes missing/extra reports.
- `targeted_update.py`
  - Downloads only files currently identified as missing from cached API pages.
  - Designed to run while `progress_check.py` is updating cache.
- `Suno_downloader.py`
  - Legacy full-library downloader with optional cover-art embedding.

## Filename rules

All scripts now use the same filename convention:

- Normal titled song: `Song Name.mp3`
- Duplicate title: `Song Name v2.mp3`, `Song Name v3.mp3`, etc.
- Untitled song: `Untitled YYYY-MM-DD <clipid8>.mp3`
- Liked song (`is_liked=true`): prefixed with `(Liked) `
  - Example: `(Liked) Song Name.mp3`
  - Example: `(Liked) Untitled 2026-02-07 ab12cd34.mp3`

## Requirements

- Python 3.8+
- `pip`
- A valid Suno auth token

Install dependencies:

```bash
pip install -r requirements.txt
```

## Get your Suno token

1. Open `https://suno.com` and sign in.
2. Open browser DevTools -> Network.
3. Refresh the page and find a request to feed API (`/api/feed/v2?...`).
4. In request headers, copy `Authorization: Bearer ...`.
5. Save only the token string (without `Bearer `) in `token.txt`.

`token.txt` is read automatically by `progress_check.py` and `targeted_update.py` if `--token` is not passed.

## Recommended workflow (reliable + fast)

### 1) Refresh status with smart cache head sync

```bash
python3 progress_check.py --head-sync-pages 8 --max-retries 8 --sleep 0.05
```

What this does:

- Checks newest live pages first and pushes cache forward when new songs are found.
- Uses cached pages for the full scan to stay fast.
- Writes:
  - `out/progress_summary.json`
  - `out/progress_missing.txt`
  - `out/progress_extra.txt`
  - `out/progress_check.log`

### 2) Download all currently-missing files

```bash
python3 targeted_update.py --once --max-retries 8 --download-sleep 0.05
```

Important behavior:

- `--once` is drain mode: it runs immediate cycles until missing files are cleared (or no eligible downloads remain).
- `--max-downloads` now defaults to `0` (unlimited per cycle), so it attempts all identified missing files each cycle.
- It does **not** block re-downloads using stale downloaded-id state, so previously removed files can be recovered.

### 3) Verify clean

```bash
python3 progress_check.py --head-sync-pages 8 --max-retries 8 --sleep 0.05
```

Clean state means in `out/progress_summary.json`:

- `missing_titles = 0`
- `extra_titles = 0`

## Expected workflow (daily operation)

Use this every time you want the local folder fully synchronized:

1. Run a status + cache-head sync pass.
2. Run targeted drain download to pull every missing file currently identified.
3. Run a final status check and confirm clean summary.

Commands:

```bash
python3 progress_check.py --head-sync-pages 8 --max-retries 8 --sleep 0.05
python3 targeted_update.py --once --max-retries 8 --download-sleep 0.05
python3 progress_check.py --head-sync-pages 8 --max-retries 8 --sleep 0.05
```

Expected final result in `out/progress_summary.json`:

- `complete_api_fetch: true`
- `missing_titles: 0`
- `extra_titles: 0`

If new songs are created while this is running, run the same 3 commands again.

## Continuous sync mode

Terminal A:

```bash
python3 progress_check.py --head-sync-pages 8 --max-retries 8 --sleep 0.05
```

Terminal B:

```bash
python3 targeted_update.py --poll-interval 5 --stop-when-clean --max-retries 8 --download-sleep 0.05
```

This keeps downloading missing tracks while cache is being updated.

## Script reference

### `progress_check.py`

Key options:

- `--refresh`: ignore cache and refetch everything
- `--head-sync-pages N`: number of live head pages to probe before using cache
- `--max-retries N`: retries per page (`0` = infinite)
- `--fail-on-partial`: exits non-zero if feed did not complete

### `targeted_update.py`

Key options:

- `--once`: drain cycles then exit
- `--max-downloads N`: per-cycle cap (`0` = all missing, default)
- `--max-retries N`: retries per clip (`0` = infinite)
- `--stop-when-clean`: exit when no missing files and `progress_check` is complete
- `--dry-run`: show planned downloads without writing files

### `Suno_downloader.py`

Example:

```bash
python3 Suno_downloader.py --token "YOUR_TOKEN" --directory "suno-downloads" --with-thumbnail
```

Options:

- `--token` (required)
- `--directory`
- `--with-thumbnail`
- `--proxy`

## Output files

All operational artifacts are in `out/`:

- `out/api_cache/page_XXXX.json`
- `out/progress_check.log`
- `out/progress_summary.json`
- `out/progress_missing.txt`
- `out/progress_extra.txt`
- `out/targeted_update.log`
- `out/targeted_update_state.json`

## Troubleshooting

- `401` / `403`
  - Token expired/invalid. Re-export token from browser and retry.
- `429`
  - Rate limiting. Scripts auto-retry with backoff; reduce aggressiveness if needed (`--sleep`, `--download-sleep`).
- DNS / reachability errors
  - Network/VPN/DNS issue; scripts log warnings and retry.
- New songs not appearing
  - Run `progress_check.py` with head sync enabled (default behavior) or use `--refresh` for full recache.

## Notes

- This is an unofficial toolset and not affiliated with Suno.
- Use it for your own content and in compliance with Suno terms.
