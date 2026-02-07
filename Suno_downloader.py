#!/usr/bin/env python3
import argparse
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests
from colorama import Fore, init
from mutagen.id3 import ID3, APIC, TIT2, TPE1, error
from mutagen.mp3 import MP3

init(autoreset=True)

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


def clip_filename_base(clip):
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
    return clip_filename_base(clip)


def pick_proxy_dict(proxies_list):
    if not proxies_list:
        return None
    proxy = random.choice(proxies_list)
    return {"http": proxy, "https": proxy}


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


def embed_metadata(mp3_path, image_url=None, title=None, artist=None, proxies_list=None, token=None, timeout=15):
    if not image_url:
        return

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    proxy_dict = pick_proxy_dict(proxies_list)
    r = requests.get(image_url, proxies=proxy_dict, headers=headers, timeout=timeout)
    r.raise_for_status()
    image_bytes = r.content
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]

    audio = MP3(mp3_path, ID3=ID3)
    try:
        audio.add_tags()
    except error:
        pass

    if title:
        audio.tags["TIT2"] = TIT2(encoding=3, text=title)
    if artist:
        audio.tags["TPE1"] = TPE1(encoding=3, text=artist)

    for key in list(audio.tags.keys()):
        if key.startswith("APIC"):
            del audio.tags[key]

    audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
    audio.save(v2_version=3)


def load_token(arg_token, token_file):
    if arg_token:
        return arg_token.strip()
    path = Path(token_file)
    if not path.exists():
        return None
    return path.read_text().strip()


class AuthFailure(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"authorization failed with status {status_code}")


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


def fetch_feed_page(session, page, token, proxies_list, timeout, max_retries, max_backoff, jitter, base_sleep):
    url = (
        "https://studio-api.prod.suno.com/api/feed/v2"
        "?hide_disliked=true&hide_gen_stems=true&hide_studio_clips=true&page="
        f"{page}"
    )
    headers = {"Authorization": f"Bearer {token}"}
    attempt = 0
    while True:
        attempt += 1
        try:
            r = session.get(url, headers=headers, proxies=pick_proxy_dict(proxies_list), timeout=timeout)
            if r.status_code in (401, 403):
                raise AuthFailure(r.status_code)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                raise requests.HTTPError(f"retryable status {r.status_code}")
            if 400 <= r.status_code < 500:
                raise NonRetryableHTTP(page, r.status_code)
            r.raise_for_status()
            data = r.json()
            batch = data if isinstance(data, list) else data.get("clips", [])
            return batch
        except (requests.RequestException, ValueError) as e:
            if max_retries and attempt > max_retries:
                raise RetryExceeded(page, e) from e
            wait = min(max_backoff, (2 ** (attempt - 1)) * base_sleep)
            wait += random.uniform(0, jitter)
            print(f"{Fore.YELLOW}Retrying page {page} in {wait:.1f}s (attempt {attempt}): {e}")
            time.sleep(wait)


def dedupe_clips_by_id(clips):
    seen = set()
    deduped = []
    for clip in clips:
        clip_id = clip.get("id")
        if not clip_id:
            continue
        if clip_id in seen:
            continue
        seen.add(clip_id)
        deduped.append(clip)
    return deduped


def fetch_all_clips(token, proxies_list, args):
    print(f"{Fore.CYAN}Extracting private songs using Authorization Token...")

    session = requests.Session()
    clips = []
    page = 0
    complete = False
    stop_reason = ""

    while True:
        if args.max_pages and page >= args.max_pages:
            stop_reason = f"max_pages:{args.max_pages}"
            break

        print(f"{Fore.MAGENTA}Fetching songs (Page {page})...")
        try:
            batch = fetch_feed_page(
                session=session,
                page=page,
                token=token,
                proxies_list=proxies_list,
                timeout=args.timeout,
                max_retries=args.max_retries,
                max_backoff=args.max_backoff,
                jitter=args.jitter,
                base_sleep=args.sleep,
            )
        except AuthFailure as e:
            print(f"{Fore.RED}Authorization failed (status {e.status_code}). Token is likely expired/invalid.")
            return [], False, f"auth_failed:{e.status_code}"
        except NonRetryableHTTP as e:
            print(f"{Fore.RED}{e}")
            stop_reason = f"http_{e.status_code}_page:{e.page}"
            break
        except RetryExceeded as e:
            print(f"{Fore.RED}{e}")
            stop_reason = f"retry_exceeded_page:{e.page}"
            break

        if not batch:
            print(f"{Fore.YELLOW}No more clips found on page {page}.")
            complete = True
            stop_reason = f"end_of_feed_page:{page}"
            break

        clips.extend(batch)
        print(f"{Fore.GREEN}Found {len(batch)} clips on page {page}. Total so far: {len(clips)}")
        page += 1
        time.sleep(args.sleep)

    deduped = dedupe_clips_by_id(clips)
    songs = []
    for clip in deduped:
        clip_id = clip.get("id")
        audio_url = clip.get("audio_url")
        if not clip_id or not audio_url:
            continue
        songs.append(
            {
                "id": str(clip_id),
                "title": clip.get("title") or "",
                "display_title": display_title(clip),
                "filename_base": clip_filename_base(clip),
                "audio_url": audio_url,
                "image_url": clip.get("image_url"),
                "display_name": clip.get("display_name"),
                "created_at": clip.get("created_at") or "",
            }
        )

    songs.sort(key=lambda c: (c["created_at"], c["id"]))
    return songs, complete, stop_reason


def plan_first_pass_downloads(songs, local_counts):
    seen_expected = Counter()
    planned = []
    skipped_as_existing = 0

    for song in songs:
        base = song["filename_base"]
        seen_expected[base] += 1
        if seen_expected[base] <= local_counts.get(base, 0):
            skipped_as_existing += 1
            continue
        planned.append(song)

    return planned, skipped_as_existing


def download_song(session, song, out_dir, token, proxies_list, args):
    headers = {"Authorization": f"Bearer {token}"}
    attempt = 0
    while True:
        attempt += 1
        tmp_path = None
        try:
            with session.get(
                song["audio_url"],
                stream=True,
                headers=headers,
                proxies=pick_proxy_dict(proxies_list),
                timeout=args.timeout,
            ) as r:
                if r.status_code in (401, 403):
                    return {"ok": False, "fatal_auth": True, "error": f"auth_failed:{r.status_code}"}
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    raise requests.HTTPError(f"retryable status {r.status_code}")
                if 400 <= r.status_code < 500:
                    return {"ok": False, "fatal_auth": False, "error": f"http_{r.status_code}"}
                r.raise_for_status()

                out_path = reserve_unique_path(out_dir, song["filename_base"])
                tmp_path = out_path.with_suffix(out_path.suffix + ".part")
                with tmp_path.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                tmp_path.replace(out_path)
                return {"ok": True, "path": out_path}
        except (requests.RequestException, OSError) as e:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            if args.max_retries and attempt > args.max_retries:
                return {"ok": False, "fatal_auth": False, "error": str(e)}
            wait = min(args.max_backoff, (2 ** (attempt - 1)) * args.sleep)
            wait += random.uniform(0, args.jitter)
            print(f"{Fore.YELLOW}Retrying clip {song['id']} in {wait:.1f}s (attempt {attempt}): {e}")
            time.sleep(wait)


def main():
    base_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="First-pass bulk downloader for Suno (workflow step 1): fetch feed and download currently-missing files."
    )
    parser.add_argument("--token", type=str, default=None, help="Suno Bearer token. If omitted, uses --token-file.")
    parser.add_argument(
        "--token-file",
        type=str,
        default=str(base_dir / "token.txt"),
        help="Path to token file used when --token is omitted.",
    )
    parser.add_argument(
        "--directory",
        type=str,
        default=str(base_dir / "out"),
        help="Local directory for saving files (default: out).",
    )
    parser.add_argument("--proxy", type=str, help="Proxy with protocol (comma-separated).")
    parser.add_argument("--with-thumbnail", action="store_true", help="Embed each song thumbnail into ID3 metadata.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Base sleep/backoff unit in seconds.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument("--max-pages", type=int, default=0, help="Optional max pages to fetch (0 = no limit).")
    parser.add_argument("--max-retries", type=int, default=8, help="Retries per page/download (0 = infinite).")
    parser.add_argument("--max-backoff", type=float, default=60.0, help="Maximum backoff in seconds.")
    parser.add_argument("--jitter", type=float, default=0.3, help="Random jitter added to backoff sleep.")
    parser.add_argument("--dry-run", action="store_true", help="Build plan only; do not download files.")
    parser.add_argument("--fail-on-partial", action="store_true", help="Exit non-zero if API fetch did not complete.")
    parser.add_argument("--fail-on-download-errors", action="store_true", help="Exit non-zero if any downloads fail.")
    args = parser.parse_args()

    token = load_token(args.token, args.token_file)
    if not token:
        print(
            f"{Fore.RED}No token provided. Pass --token or create token file at {args.token_file}",
            file=sys.stderr,
        )
        sys.exit(1)

    proxies_list = args.proxy.split(",") if args.proxy else None
    out_dir = Path(args.directory)
    out_dir.mkdir(parents=True, exist_ok=True)

    songs, complete_api_fetch, stop_reason = fetch_all_clips(token, proxies_list, args)
    if stop_reason.startswith("auth_failed"):
        sys.exit(1)
    if not songs:
        print(f"{Fore.YELLOW}No clips discovered from API.")
        if args.fail_on_partial and not complete_api_fetch:
            sys.exit(2)
        sys.exit(0)

    local_counts = count_local_mp3_by_base(out_dir)
    plan, skipped_as_existing = plan_first_pass_downloads(songs, local_counts)

    print(f"\n{Fore.CYAN}--- First-Pass Download Plan ---")
    print(f"{Fore.CYAN}API unique clips: {len(songs)}")
    print(f"{Fore.CYAN}Local files detected: {sum(local_counts.values())}")
    print(f"{Fore.CYAN}Assumed already present by title count: {skipped_as_existing}")
    print(f"{Fore.CYAN}Planned downloads: {len(plan)}")
    print(f"{Fore.CYAN}API fetch complete: {complete_api_fetch} ({stop_reason})")

    if args.dry_run:
        for song in plan[:25]:
            print(f"{Fore.YELLOW}DRY RUN: {song['display_title']} -> {song['filename_base']}.mp3")
        if len(plan) > 25:
            print(f"{Fore.YELLOW}... and {len(plan) - 25} more")
        sys.exit(0)

    session = requests.Session()
    downloaded = 0
    failed = 0
    fatal_auth = False

    print(f"\n{Fore.CYAN}--- Starting Download Process ({len(plan)} planned) ---")
    for song in plan:
        print(f"Processing: {Fore.GREEN}{song['display_title']}")
        result = download_song(session, song, out_dir, token, proxies_list, args)
        if not result.get("ok"):
            failed += 1
            if result.get("fatal_auth"):
                fatal_auth = True
            print(f"{Fore.RED}  -> Failed: {result.get('error')}")
            continue

        downloaded += 1
        saved_path = result["path"]
        print(f"{Fore.GREEN}  -> Downloaded: {saved_path.name}")

        if args.with_thumbnail and song.get("image_url"):
            try:
                embed_metadata(
                    saved_path,
                    image_url=song["image_url"],
                    token=token,
                    artist=song.get("display_name"),
                    title=song.get("title") or song["filename_base"],
                    proxies_list=proxies_list,
                    timeout=args.timeout,
                )
                print(f"{Fore.GREEN}  -> Embedded thumbnail")
            except Exception as e:
                print(f"{Fore.YELLOW}  -> Thumbnail embed skipped: {e}")

    print(f"\n{Fore.BLUE}--- Summary ---")
    print(f"{Fore.BLUE}Downloaded: {downloaded}")
    print(f"{Fore.BLUE}Failed: {failed}")
    print(f"{Fore.BLUE}Output directory: {out_dir}")
    print(f"{Fore.BLUE}API fetch complete: {complete_api_fetch} ({stop_reason})")

    if fatal_auth:
        sys.exit(1)
    if args.fail_on_partial and not complete_api_fetch:
        sys.exit(2)
    if args.fail_on_download_errors and failed > 0:
        sys.exit(3)
    sys.exit(0)


if __name__ == "__main__":
    main()
