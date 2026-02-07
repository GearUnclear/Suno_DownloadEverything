# Suno Bulk Downloader

A simple command-line Python toolkit to download and keep your private songs from [Suno AI](https://suno.com/) synchronized locally.

This repo supports both one-shot bulk download and a reliable sync workflow that checks API progress, detects missing files, and downloads only what is missing.

## Features

- **Bulk Download:** Download all songs from your private library.
- **Reliable Progress Check:** Compare full API feed vs local MP3 files and generate missing/extra reports.
- **Targeted Missing Recovery:** Download only files identified as missing.
- **Smart Cache Sync:** Reuse API cache for speed while pulling new head-page songs.
- **Liked Filename Prefixing:** Songs with `is_liked=true` are saved with `(Liked) ` prefix.
- **Duplicate Handling:** Saves duplicate titles as versioned files (for example, `My Song v2.mp3`).
- **Metadata Embedding:** Legacy downloader can embed title/artist/cover art.
- **Proxy Support:** Legacy downloader supports HTTP/S proxies.

https://imgur.com/a/Ox9goh7

## Requirements

- [Python 3.8+](https://www.python.org/downloads/)
- `pip` (Python package installer)

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/Suno_DownloadEverything.git
   cd Suno_DownloadEverything
   ```

2. **Install required packages:**
   ```bash
   pip install -r requirements.txt
   ```

## How to Use

The scripts require a **Suno Authorization Token** to access your private library.

### Step 1: Find Your Authorization Token

1. Open [suno.com](https://suno.com/) and sign in.
2. Open browser **Developer Tools** (`F12` / `Ctrl+Shift+I` / `Cmd+Option+I`).
3. Go to the **Network** tab.
4. Filter by `feed`.
5. Refresh the page and click a feed request (`/api/feed/v2?...`).
6. In **Headers**, find `Authorization: Bearer ...`.
7. Copy only the token string (without `Bearer `) and save it to `token.txt`.

Example:
https://i.imgur.com/PQtOIM5.jpeg

**Important:** Treat your token like a password. Do not share it.

### Step 2: Run the Recommended Sync Workflow

Run these three commands in order:

```bash
python3 progress_check.py --head-sync-pages 8 --max-retries 8 --sleep 0.05
python3 targeted_update.py --once --max-retries 8 --download-sleep 0.05
python3 progress_check.py --head-sync-pages 8 --max-retries 8 --sleep 0.05
```

This is the expected operational workflow:

1. Build/update API view and missing report.
2. Download all currently identified missing files.
3. Verify clean status.

Expected clean result in `out/progress_summary.json`:

- `complete_api_fetch: true`
- `missing_titles: 0`
- `extra_titles: 0`

### Important Targeted Update Behavior

- `targeted_update.py --once` runs in drain mode and continues until missing files are cleared (or no eligible clips remain).
- `--max-downloads` defaults to `0`, which means unlimited per cycle (download all currently identified missing files each cycle).

### Optional: Continuous Two-Terminal Mode

Terminal A:

```bash
python3 progress_check.py --head-sync-pages 8 --max-retries 8 --sleep 0.05
```

Terminal B:

```bash
python3 targeted_update.py --poll-interval 5 --stop-when-clean --max-retries 8 --download-sleep 0.05
```

## Basic Legacy Downloader Usage

**Basic Usage:**
```bash
python3 Suno_downloader.py --token "your_token_here"
```

**With Thumbnail + Custom Directory:**
```bash
python3 Suno_downloader.py --token "your_token_here" --directory "My Suno Music" --with-thumbnail
```

## Command-Line Arguments

### `progress_check.py`

- `--token` / `--token-file`: auth token input.
- `--out-dir`: output folder (default: `out`).
- `--refresh`: ignore cache and refetch all pages.
- `--head-sync-pages`: live head pages to probe before using cache.
- `--max-retries`: retries per page (`0` means infinite).
- `--fail-on-partial`: non-zero exit if feed did not complete.

### `targeted_update.py`

- `--once`: run drain cycles and exit.
- `--max-downloads`: cap per cycle (`0` means all missing, default).
- `--max-retries`: retries per clip (`0` means infinite).
- `--stop-when-clean`: exit when missing is zero and progress fetch is complete.
- `--dry-run`: plan only, no downloads.

### `Suno_downloader.py`

- `--token` **(Required)**
- `--directory`
- `--with-thumbnail`
- `--proxy`

## Output Files

All runtime artifacts are written under `out/`:

- `out/api_cache/page_XXXX.json`
- `out/progress_check.log`
- `out/progress_summary.json`
- `out/progress_missing.txt`
- `out/progress_extra.txt`
- `out/targeted_update.log`
- `out/targeted_update_state.json`

## Troubleshooting

- **401 / 403:** Token is expired or invalid. Re-export token and retry.
- **429:** Rate limiting. Scripts auto-retry with backoff.
- **DNS / reachability failures:** Check network/VPN/DNS; retries are automatic.
- **New songs not seen yet:** Rerun `progress_check.py` (or use `--refresh` for full recache).

## Disclaimer

This is an unofficial tool and is not affiliated with Suno, Inc. It is intended for personal use to back up your own creations. Please follow Suno's Terms of Service.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
