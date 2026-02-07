#!/usr/bin/env python3
import argparse
import json
import random
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

FILENAME_BAD_CHARS = r'[<>:"/\\|?*\x00-\x1F]'
VERSIONED_NAME_RE = re.compile(r"^(.*) v(\d+)$")
UNTITLED_PREFIX = "Untitled"
LIKED_PREFIX = "(Liked) "


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


def display_title(clip):
    raw_title = clip.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    if title:
        return apply_liked_prefix(title, clip_is_liked(clip))
    return clip_base_name(clip)


def load_state(path):
    if not path.exists():
        return {"failed_attempts": {}}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("state must be an object")
        failed_attempts = data.get("failed_attempts", {})
        if not isinstance(failed_attempts, dict):
            failed_attempts = {}
        return {
            "failed_attempts": {str(k): int(v) for k, v in failed_attempts.items() if isinstance(v, int)},
        }
    except Exception:
        return {"failed_attempts": {}}


def save_state(path, failed_attempts):
    payload = {
        "updated_at": utc_ts(),
        "failed_attempts": failed_attempts,
    }
    path.write_text(json.dumps(payload, indent=2))


def reserve_unique_path(out_dir, base_name):
    first = out_dir / f"{base_name}.mp3"
    if not first.exists():
        return first
    n = 2
    while True:
        candidate = out_dir / f"{base_name} v{n}.mp3"
        if not candidate.exists():
            return candidate
        n += 1


def count_local_mp3_by_base(out_dir):
    counts = Counter()
    for path in out_dir.glob("*.mp3"):
        stem = path.stem
        m = VERSIONED_NAME_RE.match(stem)
        base = m.group(1) if m else stem
        counts[base] += 1
    return counts


def load_cache_clips(cache_dir):
    expected = Counter()
    clips_by_base = defaultdict(list)
    seen_ids = set()
    parsed_pages = 0
    unreadable_pages = 0

    for page_path in sorted(cache_dir.glob("page_*.json")):
        try:
            data = json.loads(page_path.read_text())
        except Exception:
            unreadable_pages += 1
            continue
        parsed_pages += 1

        batch = data if isinstance(data, list) else data.get("clips", [])
        if not isinstance(batch, list):
            continue

        for clip in batch:
            clip_id = clip.get("id")
            audio_url = clip.get("audio_url")
            if not clip_id or not audio_url:
                continue
            if clip_id in seen_ids:
                continue
            seen_ids.add(clip_id)

            title = display_title(clip)
            base = clip_base_name(clip)

            expected[base] += 1
            clips_by_base[base].append(
                {
                    "id": clip_id,
                    "title": title,
                    "base": base,
                    "audio_url": audio_url,
                    "created_at": clip.get("created_at") or "",
                }
            )

    for base in clips_by_base:
        clips_by_base[base].sort(key=lambda c: (c["created_at"], c["id"]))

    return expected, clips_by_base, parsed_pages, unreadable_pages


def load_missing_hints(missing_file):
    if not missing_file.exists():
        return []
    hinted = []
    for line in missing_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        base = line.split("\t", 1)[0].strip()
        if base:
            hinted.append(base)
    return hinted


def progress_fetch_complete(summary_path):
    if not summary_path.exists():
        return False
    try:
        data = json.loads(summary_path.read_text())
    except Exception:
        return False
    return bool(data.get("complete_api_fetch"))


def build_plan(missing_counts, clips_by_base, failed_attempts, hinted_bases, max_downloads, per_clip_max_failures):
    plan = []
    hinted_set = set(hinted_bases)

    def sort_key(base_name):
        return (0 if base_name in hinted_set else 1, base_name)

    for base in sorted(missing_counts.keys(), key=sort_key):
        need = missing_counts[base]
        if need <= 0:
            continue
        for clip in clips_by_base.get(base, []):
            clip_id = clip["id"]
            if failed_attempts.get(clip_id, 0) >= per_clip_max_failures:
                continue
            plan.append(clip)
            need -= 1
            if need <= 0 or len(plan) >= max_downloads:
                break
        if len(plan) >= max_downloads:
            break
    return plan


def download_clip(session, clip, out_dir, token, timeout, max_retries, max_backoff, jitter, base_sleep, log):
    headers = {"Authorization": f"Bearer {token}"}
    clip_id = clip["id"]
    url = clip["audio_url"]
    base_name = clip["base"]

    attempt = 0
    while True:
        attempt += 1
        try:
            with session.get(url, headers=headers, stream=True, timeout=timeout) as r:
                if r.status_code in (401, 403):
                    return {"ok": False, "retryable": False, "error": f"auth_failed:{r.status_code}"}
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    raise requests.HTTPError(f"retryable status {r.status_code}")
                if 400 <= r.status_code < 500:
                    return {"ok": False, "retryable": False, "error": f"http_{r.status_code}"}
                r.raise_for_status()

                out_path = reserve_unique_path(out_dir, base_name)
                with out_path.open("xb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

                return {"ok": True, "path": str(out_path), "clip_id": clip_id}
        except (requests.RequestException, OSError) as e:
            if max_retries and attempt >= max_retries:
                return {"ok": False, "retryable": True, "error": str(e)}
            if attempt == 1 and is_dns_error(e):
                log("WARN: DNS resolution failed during targeted download; check network/VPN/DNS.")
            backoff = min(max_backoff, (2 ** (attempt - 1)) * base_sleep)
            backoff += random.uniform(0, jitter)
            log(f"Retrying clip {clip_id} in {backoff:.1f}s (attempt {attempt}): {e}")
            time.sleep(backoff)


def resolve_cycle_download_limit(max_downloads, missing_files):
    # 0 means unlimited for the current cycle.
    if max_downloads and max_downloads > 0:
        return max_downloads
    return max(1, missing_files)


def main():
    base_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Continuously download only currently-missing files using progress_check cache/output."
    )
    parser.add_argument("--token", type=str, default=None, help="Bearer token. If omitted, uses token.txt.")
    parser.add_argument("--token-file", type=str, default=str(base_dir / "token.txt"), help="Path to token file.")
    parser.add_argument("--out-dir", type=str, default=str(base_dir / "out"), help="Download/output directory.")
    parser.add_argument("--cache-dir", type=str, default=None, help="Cache directory for API pages.")
    parser.add_argument("--state-file", type=str, default=None, help="State file path.")
    parser.add_argument("--log-file", type=str, default=None, help="Log file path.")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between scan cycles.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=0,
        help="Max downloads per cycle (0 = all currently-missing files).",
    )
    parser.add_argument("--download-sleep", type=float, default=0.2, help="Base sleep for retry backoff.")
    parser.add_argument("--max-retries", type=int, default=8, help="Max retries per clip download (0 = infinite).")
    parser.add_argument("--max-backoff", type=float, default=60.0, help="Maximum retry backoff in seconds.")
    parser.add_argument("--jitter", type=float, default=0.3, help="Random jitter added to backoff sleep.")
    parser.add_argument("--max-idle-cycles", type=int, default=0, help="Stop after N idle cycles (0 = infinite).")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run immediate drain cycles until missing files are cleared (or no eligible downloads remain), then exit.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show planned downloads but do not download.")
    parser.add_argument(
        "--stop-when-clean",
        action="store_true",
        help="Exit once no missing files remain and progress_summary says API fetch completed.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir) if args.cache_dir else (out_dir / "api_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    state_path = Path(args.state_file) if args.state_file else (out_dir / "targeted_update_state.json")
    log_path = Path(args.log_file) if args.log_file else (out_dir / "targeted_update.log")
    progress_missing_path = out_dir / "progress_missing.txt"
    progress_summary_path = out_dir / "progress_summary.json"

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

    state = load_state(state_path)
    failed_attempts = state.get("failed_attempts", {})

    session = requests.Session()
    idle_cycles = 0
    cycle = 0

    log("Starting targeted update watcher...")

    while True:
        cycle += 1
        expected, clips_by_base, parsed_pages, unreadable_pages = load_cache_clips(cache_dir)
        actual = count_local_mp3_by_base(out_dir)
        hinted_bases = load_missing_hints(progress_missing_path)

        missing = {base: (need - actual.get(base, 0)) for base, need in expected.items() if need > actual.get(base, 0)}
        missing_titles = len(missing)
        missing_files = sum(missing.values())

        log(
            f"Cycle {cycle}: cache_pages={parsed_pages} unreadable_pages={unreadable_pages} "
            f"expected_files={sum(expected.values())} local_files={sum(actual.values())} "
            f"missing_titles={missing_titles} missing_files={missing_files}"
        )

        cycle_max_downloads = resolve_cycle_download_limit(args.max_downloads, missing_files)

        plan = build_plan(
            missing_counts=missing,
            clips_by_base=clips_by_base,
            failed_attempts=failed_attempts,
            hinted_bases=hinted_bases,
            max_downloads=cycle_max_downloads,
            per_clip_max_failures=max(args.max_retries, 1) if args.max_retries else 9999999,
        )

        downloaded_this_cycle = 0
        if not plan:
            log("No eligible clip downloads in this cycle.")
        else:
            log(f"Planned clip downloads this cycle: {len(plan)}")
            for clip in plan:
                clip_id = clip["id"]
                if args.dry_run:
                    log(f"DRY RUN: would download clip {clip_id} title={clip['title']!r}")
                    continue
                result = download_clip(
                    session=session,
                    clip=clip,
                    out_dir=out_dir,
                    token=token,
                    timeout=args.timeout,
                    max_retries=args.max_retries,
                    max_backoff=args.max_backoff,
                    jitter=args.jitter,
                    base_sleep=args.download_sleep,
                    log=log,
                )
                if result.get("ok"):
                    failed_attempts.pop(clip_id, None)
                    downloaded_this_cycle += 1
                    log(f"Downloaded clip {clip_id} -> {result['path']}")
                else:
                    failed_attempts[clip_id] = int(failed_attempts.get(clip_id, 0)) + 1
                    log(f"Failed clip {clip_id}: {result.get('error')}")
                time.sleep(0.05)

        save_state(state_path, failed_attempts)

        if downloaded_this_cycle == 0:
            idle_cycles += 1
        else:
            idle_cycles = 0

        expected_after, _, _, _ = load_cache_clips(cache_dir)
        actual_after = count_local_mp3_by_base(out_dir)
        remaining_missing = sum(
            need - actual_after.get(base, 0) for base, need in expected_after.items() if need > actual_after.get(base, 0)
        )

        if args.once:
            if remaining_missing == 0:
                log("Exiting after drain run (--once): no missing files remain.")
                break
            if not plan:
                log("Exiting after drain run (--once): no eligible clips remain to satisfy missing files.")
                break
            log(f"Drain run (--once) continuing: remaining_missing_files={remaining_missing}")
            continue

        if args.stop_when_clean and remaining_missing == 0 and progress_fetch_complete(progress_summary_path):
            log("No missing files and progress_check reports complete fetch. Exiting.")
            break

        if args.max_idle_cycles and idle_cycles >= args.max_idle_cycles:
            log(f"Reached max idle cycles: {args.max_idle_cycles}. Exiting.")
            break

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
