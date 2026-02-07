#!/usr/bin/env python3
import argparse
import json
import random
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

FILENAME_BAD_CHARS = r'[<>:"/\\|?*\x00-\x1F]'
UNTITLED_PREFIX = "Untitled"
LIKED_PREFIX = "(Liked) "
CACHE_PAGE_SIZE = 20


def sanitize_filename(name, maxlen=200):
    safe = re.sub(FILENAME_BAD_CHARS, "_", name)
    safe = safe.strip(" .")
    return safe[:maxlen] if len(safe) > maxlen else safe


def clip_is_liked(clip):
    return bool(clip.get("is_liked"))


def apply_liked_prefix(name, liked):
    if not liked:
        return name
    if name.startswith(LIKED_PREFIX):
        return name
    return sanitize_filename(f"{LIKED_PREFIX}{name}")


def utc_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def is_dns_error(err):
    text = str(err)
    return "NameResolutionError" in text or "Failed to resolve" in text


class AuthFailure(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"auth failed with status {status_code}")


class NonRetryableHTTP(Exception):
    def __init__(self, page, status_code):
        self.page = page
        self.status_code = status_code
        super().__init__(f"non-retryable HTTP status {status_code} on page {page}")


class RetryExceeded(Exception):
    def __init__(self, page, last_error):
        self.page = page
        self.last_error = last_error
        super().__init__(f"exceeded max retries on page {page}: {last_error}")


def clip_base_name(clip):
    raw_title = clip.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    if title:
        return apply_liked_prefix(sanitize_filename(title), clip_is_liked(clip))

    clip_id = clip.get("id") or "unknown"
    created_at = clip.get("created_at") or ""
    date_part = created_at[:10] if isinstance(created_at, str) and len(created_at) >= 10 else "unknown-date"
    untitled = sanitize_filename(f"{UNTITLED_PREFIX} {date_part} {clip_id[:8]}")
    return apply_liked_prefix(untitled, clip_is_liked(clip))


def clip_id(clip):
    value = clip.get("id")
    return str(value) if value else None


def dedupe_clips_by_id(clips):
    seen_ids = set()
    deduped = []
    for clip in clips:
        cid = clip_id(clip)
        if cid and cid in seen_ids:
            continue
        if cid:
            seen_ids.add(cid)
        deduped.append(clip)
    return deduped


def load_cached_clips(cache_dir):
    clips = []
    for cache_file in sorted(cache_dir.glob("page_*.json")):
        try:
            data = json.loads(cache_file.read_text())
        except Exception:
            continue
        batch = data if isinstance(data, list) else data.get("clips", [])
        if not isinstance(batch, list):
            continue
        if not batch:
            break
        clips.extend(batch)
    return dedupe_clips_by_id(clips)


def rewrite_cache_clips(cache_dir, clips):
    for old in cache_dir.glob("page_*.json"):
        old.unlink()

    if not clips:
        (cache_dir / "page_0000.json").write_text(json.dumps({"clips": []}))
        return

    page = 0
    for i in range(0, len(clips), CACHE_PAGE_SIZE):
        chunk = clips[i:i + CACHE_PAGE_SIZE]
        (cache_dir / f"page_{page:04d}.json").write_text(json.dumps({"clips": chunk}))
        page += 1

    (cache_dir / f"page_{page:04d}.json").write_text(json.dumps({"clips": []}))


def fetch_live_page(session, base_api_url, headers, page, args, log):
    attempt = 0
    while True:
        try:
            r = session.get(base_api_url + str(page), headers=headers, timeout=args.timeout)
            if r.status_code in (401, 403):
                raise AuthFailure(r.status_code)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                raise requests.HTTPError(f"retryable status {r.status_code}")
            if 400 <= r.status_code < 500:
                raise NonRetryableHTTP(page, r.status_code)
            r.raise_for_status()
            data = r.json()
            batch = data if isinstance(data, list) else data.get("clips", [])
            return data, batch
        except (requests.RequestException, ValueError) as e:
            attempt += 1
            if args.max_retries and attempt > args.max_retries:
                raise RetryExceeded(page, e) from e
            if attempt == 1 and is_dns_error(e):
                log("WARN: DNS resolution failed; check network/VPN/DNS settings.")
            backoff = min(args.max_backoff, (2 ** (attempt - 1)) * args.sleep)
            backoff += random.uniform(0, args.jitter)
            log(f"Retrying page {page} in {backoff:.1f}s (attempt {attempt}): {e}")
            time.sleep(backoff)


def sync_cache_head(session, base_api_url, headers, cache_dir, args, log):
    cached_clips = load_cached_clips(cache_dir)
    if not cached_clips:
        return {"status": "empty_cache", "shifted_clips": 0}

    cached_ids = {clip_id(c) for c in cached_clips if clip_id(c)}
    live_prefix = []
    anchor_found = False

    for page in range(0, args.head_sync_pages):
        _, batch = fetch_live_page(session, base_api_url, headers, page, args, log)
        if not batch:
            rewrite_cache_clips(cache_dir, [])
            return {"status": "feed_empty", "shifted_clips": len(cached_clips)}

        for clip in batch:
            cid = clip_id(clip)
            if cid and cid in cached_ids:
                anchor_found = True
                break
            live_prefix.append(clip)
        if anchor_found:
            break

    if not anchor_found:
        return {"status": "no_overlap_refresh", "shifted_clips": 0}

    merged = dedupe_clips_by_id(live_prefix + cached_clips)
    shifted_clips = max(0, len(merged) - len(cached_clips))
    if shifted_clips > 0:
        rewrite_cache_clips(cache_dir, merged)
        return {"status": "shifted", "shifted_clips": shifted_clips}
    return {"status": "up_to_date", "shifted_clips": 0}


def main():
    base_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Progressively check Suno downloads vs API with retry backoff and on-disk cache."
    )
    parser.add_argument("--token", type=str, default=None, help="Bearer token. If omitted, uses token.txt.")
    parser.add_argument("--token-file", type=str, default=str(base_dir / "token.txt"), help="Path to token file.")
    parser.add_argument("--out-dir", type=str, default=str(base_dir / "out"), help="Download/output directory.")
    parser.add_argument("--cache-dir", type=str, default=None, help="Cache directory for API pages.")
    parser.add_argument("--refresh", action="store_true", help="Ignore cache and refetch all pages.")
    parser.add_argument("--sleep", type=float, default=1.0, help="Base sleep between successful page requests.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds.")
    parser.add_argument("--max-pages", type=int, default=0, help="Optional max pages to fetch (0 = no limit).")
    parser.add_argument("--max-retries", type=int, default=12, help="Max retries per page (0 = infinite).")
    parser.add_argument("--max-backoff", type=float, default=120.0, help="Maximum backoff sleep in seconds.")
    parser.add_argument("--jitter", type=float, default=0.5, help="Random jitter added to backoff sleep.")
    parser.add_argument(
        "--head-sync-pages",
        type=int,
        default=5,
        help="When using cache, probe this many live head pages and push cache forward (0 disables).",
    )
    parser.add_argument(
        "--fail-on-partial",
        action="store_true",
        help="Exit with code 2 if API fetch did not complete to end-of-feed.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path(args.cache_dir) if args.cache_dir else (out_dir / "api_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / "progress_check.log"

    def log(msg):
        line = f"[{utc_ts()}] {msg}"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line)

    token = args.token
    if not token:
        token_file = Path(args.token_file)
        if not token_file.exists():
            log(f"ERROR: token file not found at {token_file}")
            raise SystemExit(1)
        token = token_file.read_text().strip()

    base_api_url = (
        "https://studio-api.prod.suno.com/api/feed/v2"
        "?hide_disliked=true&hide_gen_stems=true&hide_studio_clips=true&page="
    )
    headers = {"Authorization": f"Bearer {token}"}

    session = requests.Session()

    log("Starting API fetch...")

    cache_head_sync = "disabled_by_flag" if args.head_sync_pages <= 0 else "skipped"
    cache_head_shifted_clips = 0

    if not args.refresh and args.head_sync_pages > 0 and any(cache_dir.glob("page_*.json")):
        try:
            sync_result = sync_cache_head(session, base_api_url, headers, cache_dir, args, log)
            cache_head_sync = sync_result["status"]
            cache_head_shifted_clips = sync_result.get("shifted_clips", 0)
            if cache_head_sync == "shifted":
                log(f"Cache head sync inserted {cache_head_shifted_clips} new clip(s) at the front.")
            if cache_head_sync == "no_overlap_refresh":
                log(
                    f"No cache overlap found in first {args.head_sync_pages} live pages; "
                    "falling back to full refresh."
                )
                for old in cache_dir.glob("page_*.json"):
                    old.unlink()
                args.refresh = True
        except AuthFailure as e:
            log(f"ERROR: auth failed with status {e.status_code}")
            raise SystemExit(1)
        except NonRetryableHTTP as e:
            log(f"ERROR: non-retryable HTTP status {e.status_code} on page {e.page} during head sync")
            raise SystemExit(1)
        except RetryExceeded as e:
            log(f"ERROR: exceeded max retries on page {e.page} during head sync: {e.last_error}")
            raise SystemExit(1)
    elif args.refresh:
        cache_head_sync = "skipped_refresh_mode"

    page = 0
    clips = []
    complete = True
    stop_reason = "end_of_feed"

    while True:
        if args.max_pages and page >= args.max_pages:
            log(f"Reached max-pages limit: {args.max_pages}")
            complete = False
            stop_reason = f"max_pages_reached:{args.max_pages}"
            break

        cache_file = cache_dir / f"page_{page:04d}.json"

        if cache_file.exists() and not args.refresh:
            try:
                data = json.loads(cache_file.read_text())
                batch = data if isinstance(data, list) else data.get("clips", [])
                if not batch:
                    log(f"No more clips at page {page}.")
                    stop_reason = f"end_of_feed_page:{page}"
                    break
                clips.extend(batch)
                log(f"Loaded page {page} from cache: {len(batch)} clips (total {len(clips)})")
                page += 1
                time.sleep(args.sleep)
                continue
            except Exception as e:
                log(f"WARN: failed to read cache for page {page}: {e}. Refetching...")

        batch = None
        while True:
            try:
                data, batch = fetch_live_page(session, base_api_url, headers, page, args, log)
                cache_file.write_text(json.dumps(data))

                if not batch:
                    log(f"No more clips at page {page}.")
                    stop_reason = f"end_of_feed_page:{page}"
                    break

                clips.extend(batch)
                log(f"Fetched page {page}: {len(batch)} clips (total {len(clips)})")
                page += 1
                time.sleep(args.sleep)
                break
            except AuthFailure as e:
                log(f"ERROR: auth failed with status {e.status_code}")
                complete = False
                stop_reason = f"auth_failed:{e.status_code}"
                break
            except NonRetryableHTTP as e:
                log(f"ERROR: non-retryable HTTP status {e.status_code} on page {e.page}")
                complete = False
                stop_reason = f"http_{e.status_code}_page:{e.page}"
                break
            except RetryExceeded as e:
                log(f"ERROR: exceeded max retries on page {e.page}: {e.last_error}")
                complete = False
                stop_reason = f"max_retries_exceeded_page:{e.page}"
                break

        # end while for attempts

        if batch == []:
            break
        if stop_reason.startswith("auth_failed") or stop_reason.startswith("http_") or stop_reason.startswith("max_retries_exceeded"):
            break

    # summarize expected; dedupe by clip id because feed can contain repeats
    deduped_clips = []
    seen_ids = set()
    for c in clips:
        clip_id = c.get("id")
        if clip_id and clip_id in seen_ids:
            continue
        if clip_id:
            seen_ids.add(clip_id)
        deduped_clips.append(c)

    expected = Counter()
    for c in deduped_clips:
        base = clip_base_name(c)
        expected[base] += 1

    # actual counts by base filename (strip ' vN' suffix)
    actual = Counter()
    for p in out_dir.glob("*.mp3"):
        stem = p.stem
        m = re.match(r"^(.*) v(\d+)$", stem)
        base = m.group(1) if m else stem
        actual[base] += 1

    missing = {base: (need, actual.get(base, 0)) for base, need in expected.items() if actual.get(base, 0) < need}
    extra = {base: (actual.get(base, 0), expected.get(base, 0)) for base in actual.keys() if actual.get(base, 0) > expected.get(base, 0)}

    missing_path = out_dir / "progress_missing.txt"
    extra_path = out_dir / "progress_extra.txt"
    summary_path = out_dir / "progress_summary.json"

    with missing_path.open("w", encoding="utf-8") as f:
        for base, (need, have) in sorted(missing.items()):
            f.write(f"{base}\tneed={need}\thave={have}\n")

    with extra_path.open("w", encoding="utf-8") as f:
        for base, (have, need) in sorted(extra.items()):
            f.write(f"{base}\thave={have}\texpected={need}\n")

    summary = {
        "api_clips_raw": len(clips),
        "api_clips_unique": len(deduped_clips),
        "unique_titles": len(expected),
        "local_mp3_files": sum(actual.values()),
        "missing_titles": len(missing),
        "extra_titles": len(extra),
        "complete_api_fetch": complete,
        "stop_reason": stop_reason,
        "last_page_reached": page,
        "output_dir": str(out_dir),
        "log_file": str(log_path),
        "missing_file": str(missing_path),
        "extra_file": str(extra_path),
        "cache_dir": str(cache_dir),
        "cache_head_sync": cache_head_sync,
        "cache_head_shifted_clips": cache_head_shifted_clips,
        "cache_head_pages_checked": args.head_sync_pages,
    }
    summary_path.write_text(json.dumps(summary, indent=2))

    log("--- Summary ---")
    log(json.dumps(summary))
    if args.fail_on_partial and not complete:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
