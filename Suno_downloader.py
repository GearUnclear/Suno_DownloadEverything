import argparse
import os
import random
import re
import sys
import time

import requests
from colorama import Fore, init
from mutagen.id3 import ID3, APIC, TIT2, TPE1, error
from mutagen.mp3 import MP3

init(autoreset=True)

FILENAME_BAD_CHARS = r'[<>:"/\\|?*\x00-\x1F]'
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
    return f"{LIKED_PREFIX}{name}"

def clip_filename_base(clip):
    raw_title = clip.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    if title:
        base = apply_liked_prefix(title, clip_is_liked(clip))
    else:
        clip_id = clip.get("id") or "unknown"
        created_at = clip.get("created_at") or ""
        date_part = created_at[:10] if isinstance(created_at, str) and len(created_at) >= 10 else "unknown-date"
        untitled = f"{UNTITLED_PREFIX} {date_part} {clip_id[:8]}"
        base = apply_liked_prefix(untitled, clip_is_liked(clip))
    return sanitize_filename(base)

def pick_proxy_dict(proxies_list):
    if not proxies_list: return None
    proxy = random.choice(proxies_list)
    return {"http": proxy, "https": proxy}

def embed_metadata(mp3_path, image_url=None, title=None, artist=None, proxies_list=None, token=None, timeout=15):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    proxy_dict = pick_proxy_dict(proxies_list)
    r = requests.get(image_url, proxies=proxy_dict, headers=headers, timeout=timeout)
    r.raise_for_status()
    image_bytes = r.content
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    
    audio = MP3(mp3_path, ID3=ID3)
    try: audio.add_tags()
    except error: pass

    if title: audio.tags["TIT2"] = TIT2(encoding=3, text=title)
    if artist: audio.tags["TPE1"] = TPE1(encoding=3, text=artist)

    for key in list(audio.tags.keys()):
        if key.startswith("APIC"): del audio.tags[key]

    audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
    audio.save(v2_version=3)

def extract_private_song_info(token_string, proxies_list=None):
    print(f"{Fore.CYAN}Extracting private songs using Authorization Token...")
    base_api_url = "https://studio-api.prod.suno.com/api/feed/v2?hide_disliked=true&hide_gen_stems=true&hide_studio_clips=true&page="
    headers = {"Authorization": f"Bearer {token_string}"}

    song_info = {}
    page = 1
    
    while True:
        api_url = f"{base_api_url}{page}"
        try:
            print(f"{Fore.MAGENTA}Fetching songs (Page {page})...")
            response = requests.get(api_url, headers=headers, proxies=pick_proxy_dict(proxies_list), timeout=15)
            if response.status_code in [401, 403]:
                print(f"{Fore.RED}Authorization failed (status {response.status_code}). Your token is likely expired or incorrect.")
                return {}
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            print(f"{Fore.RED}Request failed on page {page}: {e}")
            return {}

        clips = data if isinstance(data, list) else data.get("clips", [])
        if not clips:
            print(f"{Fore.YELLOW}No more clips found on page {page}.")
            break

        print(f"{Fore.GREEN}Found {len(clips)} clips on page {page}.")
        for clip in clips:
            uuid, audio_url, image_url = clip.get("id"), clip.get("audio_url"), clip.get("image_url")
            if (uuid and audio_url) and uuid not in song_info:
                raw_title = clip.get("title")
                title = raw_title.strip() if isinstance(raw_title, str) else ""
                display_title = apply_liked_prefix(title, clip_is_liked(clip)) if title else clip_filename_base(clip)
                song_info[uuid] = {
                    "title": title,
                    "audio_url": audio_url,
                    "image_url": image_url,
                    "display_name": clip.get("display_name"),
                    "filename_base": clip_filename_base(clip),
                    "display_title": display_title,
                }
        page += 1
        time.sleep(5)
    return song_info

def get_unique_filename(filename):
    if not os.path.exists(filename): return filename
    name, extn = os.path.splitext(filename)
    counter = 2
    while True:
        new_filename = f"{name} v{counter}{extn}"
        if not os.path.exists(new_filename): return new_filename
        counter += 1

def download_file(url, filename, proxies_list=None, token=None, timeout=30):
    # This function now correctly handles finding a unique filename before saving
    unique_filename = get_unique_filename(filename)
    
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with requests.get(url, stream=True, proxies=pick_proxy_dict(proxies_list), headers=headers, timeout=timeout) as r:
        r.raise_for_status()
        with open(unique_filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk: f.write(chunk)
    return unique_filename

def main():
    parser = argparse.ArgumentParser(description="Bulk download your private suno songs")
    parser.add_argument("--token", type=str, required=True, help="Your Suno session Bearer Token.")
    parser.add_argument("--proxy", type=str, help="Proxy with protocol (comma-separated).")
    parser.add_argument("--directory", type=str, default="suno-downloads", help="Local directory for saving files.")
    parser.add_argument("--with-thumbnail", action="store_true", help="Embed the song's thumbnail.")
    args = parser.parse_args()

    songs = extract_private_song_info(args.token, args.proxy.split(",") if args.proxy else None)

    if not songs:
        print(f"{Fore.RED}No songs found. Please check your token.")
        sys.exit(1)

    if not os.path.exists(args.directory):
        os.makedirs(args.directory)

    print(f"\n{Fore.CYAN}--- Starting Download Process ({len(songs)} songs to check) ---")
    for uuid, obj in songs.items():
        title = obj.get("display_title") or obj.get("title") or uuid
        metadata_title = obj.get("title") or obj.get("filename_base") or uuid
        fname = obj.get("filename_base", sanitize_filename(title)) + ".mp3"
        out_path = os.path.join(args.directory, fname)

        print(f"Processing: {Fore.GREEN}🎵 {title}")
        try:
            # FIX: The old 'if os.path.exists' check was removed from here.
            # We now call download_file directly and let it handle unique filenames.
            
            print(f"  -> Downloading...")
            saved_path = download_file(obj["audio_url"], out_path, token=args.token)
            
            if args.with_thumbnail and obj.get("image_url"):
                print(f"  -> Embedding thumbnail...")
                embed_metadata(saved_path, image_url=obj["image_url"], token=args.token, artist=obj.get("display_name"), title=metadata_title)
            
            # Let the user know if a new version was created
            if os.path.basename(saved_path) != os.path.basename(out_path):
                print(f"{Fore.YELLOW}  -> Saved as new version: {os.path.basename(saved_path)}")

        except Exception as e:
            print(f"{Fore.RED}Failed on {title}: {e}")

    print(f"\n{Fore.BLUE}Download process complete. Files are in '{args.directory}'.")
    sys.exit(0)


if __name__ == "__main__":
    main()
