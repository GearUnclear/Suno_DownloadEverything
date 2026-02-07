# progress_check.py notes

This script is a durable, resumable checker for Suno downloads. It fetches the full feed from the Suno API, compares it to the local `out/` folder, and writes all logs and reports inside the repo.

## What it writes
- `out/progress_check.log`
- `out/progress_summary.json`
- `out/progress_missing.txt`
- `out/progress_extra.txt`
- `out/api_cache/` (one JSON file per page so it can resume without re-fetching)

## How it avoids timeouts
- Each page is cached on disk in `out/api_cache/`.
- If a request fails or hits `429`, the script retries with exponential backoff plus jitter.
- The script can run indefinitely (default), so it keeps retrying until it finishes.

## How to run
```bash
/root/mobile-dev/Suno_DownloadEverything/.venv/bin/python /root/mobile-dev/Suno_DownloadEverything/progress_check.py --out-dir /root/mobile-dev/Suno_DownloadEverything/out
```

## Resetting the cache
If you want a clean re-fetch, delete the cache folder or pass `--refresh`:
```bash
rm -rf /root/mobile-dev/Suno_DownloadEverything/out/api_cache
# or
/root/mobile-dev/Suno_DownloadEverything/.venv/bin/python /root/mobile-dev/Suno_DownloadEverything/progress_check.py --refresh
```

## Notes on counts
The script compares by sanitized title, the same way the downloader builds filenames. If multiple clips share the same title, the script expects multiple files with the same base name, using the downloader's `v2`, `v3` naming scheme.
If a clip has `is_liked=true`, filenames are prefixed with `(Liked) ` (for example, `(Liked) Song Name.mp3`).

## Targeted missing downloader (while progress_check runs)
Use `targeted_update.py` in a second terminal to continuously download only files currently missing from `out/` based on `out/api_cache/` pages.

```bash
/root/mobile-dev/Suno_DownloadEverything/.venv/bin/python /root/mobile-dev/Suno_DownloadEverything/targeted_update.py --out-dir /root/mobile-dev/Suno_DownloadEverything/out --poll-interval 5 --stop-when-clean
```

`targeted_update.py` now treats `--max-downloads 0` as unlimited (default), and `--once` runs in drain mode until missing files are cleared or no eligible downloads remain.

Useful safety flags:

```bash
# one planning pass, no downloads
/root/mobile-dev/Suno_DownloadEverything/.venv/bin/python /root/mobile-dev/Suno_DownloadEverything/targeted_update.py --out-dir /root/mobile-dev/Suno_DownloadEverything/out --once --dry-run

# stop if no progress for N cycles
/root/mobile-dev/Suno_DownloadEverything/.venv/bin/python /root/mobile-dev/Suno_DownloadEverything/targeted_update.py --out-dir /root/mobile-dev/Suno_DownloadEverything/out --max-idle-cycles 20
```
