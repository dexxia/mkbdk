#!/usr/bin/env python3
"""
Gabung banyak playlist M3U/M3U8 jadi satu, buang channel yang mati (offline),
buang duplikat, dan simpan sebagai satu fail .m3u.

Cara guna:
    python gabung_playlist.py                      # baca sumber dari sources.txt
    python gabung_playlist.py -s sources.txt -o live.m3u --workers 50 --timeout 8
    python gabung_playlist.py --no-check           # gabung sahaja, tak semak live

sources.txt: satu URL atau path fail .m3u setiap baris. Baris bermula '#' diabaikan.
Tiada library luar diperlukan (Python 3.8+).
"""
import argparse
import re
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')

# Banyak server IPTV guna sijil SSL yang tak sah; kita cuma semak sama ada stream hidup.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


class Channel:
    def __init__(self, extinf, options, url, source):
        self.extinf = extinf          # baris #EXTINF penuh
        self.options = options        # baris #EXTVLCOPT / #KODIPROP / #EXTGRP dll.
        self.url = url
        self.source = source
        self.attrs = dict(ATTR_RE.findall(extinf))
        self.name = extinf.rsplit(",", 1)[-1].strip() if "," in extinf else url

    def headers(self):
        h = {"User-Agent": self.attrs.get("user-agent") or DEFAULT_UA}
        for opt in self.options:
            low = opt.lower()
            if low.startswith("#extvlcopt:http-user-agent="):
                h["User-Agent"] = opt.split("=", 1)[1].strip()
            elif low.startswith("#extvlcopt:http-referrer="):
                h["Referer"] = opt.split("=", 1)[1].strip()
            elif low.startswith("#extvlcopt:http-origin="):
                h["Origin"] = opt.split("=", 1)[1].strip()
        if "Referer" not in h and self.attrs.get("http-referrer"):
            h["Referer"] = self.attrs["http-referrer"]
        return h

    def db_info(self):
        return CHANNEL_DB.get(self.attrs.get("tvg-id", "").split("@")[0], {})

    def country(self):
        c = self.attrs.get("tvg-country", "").strip().lower()
        if len(c) == 2:
            return c
        m = re.search(r"\.([a-z]{2})(?:@|$)", self.attrs.get("tvg-id", "").lower())
        if m:
            return m.group(1)
        if self.db_info().get("country"):
            return self.db_info()["country"].lower()
        m = SOURCE_COUNTRY.search(self.source)
        return m.group(1) if m else ""

    def set_group(self, group):
        if 'group-title="' in self.extinf:
            self.extinf = re.sub(r'group-title="[^"]*"', f'group-title="{group}"', self.extinf, count=1)
        else:
            head, sep, name = self.extinf.rpartition(",")
            self.extinf = f'{head} group-title="{group}"{sep}{name}' if sep else self.extinf
        self.attrs["group-title"] = group

    def to_m3u(self):
        return "\n".join([self.extinf, *self.options, self.url])


# Kategori output mengikut susunan keutamaan. Tag pertama yang padan menang,
# jadi "Kids;Movies" -> KIDS dan "Entertainment;Sports" -> SPORTS.
CATEGORY_RULES = [
    ("KIDS", {"kids", "animation", "children", "cartoon"}),
    ("SPORTS", {"sports", "sport", "outdoor", "auto"}),
    ("MOVIES", {"movies", "movie", "film", "cinema"}),
    ("ENTERTAINMENT", {"entertainment", "series", "comedy", "general", "family", "music",
                       "lifestyle", "classic", "culture", "cooking", "travel", "relax"}),
    ("NEWS", {"news", "weather", "business"}),
    ("DOCUMENTARY", {"documentary", "science", "education"}),
    ("RELIGIOUS", {"religious"}),
]
CATEGORY_ORDER = ["MOVIES", "KIDS", "ENTERTAINMENT", "SPORTS", "NEWS", "DOCUMENTARY", "RELIGIOUS", "LAIN-LAIN"]
MAIN_CATEGORIES = set(CATEGORY_ORDER[:4])
CATEGORY_FROM_SOURCE = re.compile(r"/categories/([a-z]+)\.m3u")
# Negara dari URL sumber: countries/my.m3u, streams/au_samsung.m3u, i.mjh.nz/au/...
SOURCE_COUNTRY = re.compile(r"(?:/countries/|/streams/|i\.mjh\.nz/)([a-z]{2})[/_.]")
NAME_WORDS = re.compile(r"[a-z]+")

# Pangkalan data channel iptv-org (id -> kategori, negara, nsfw); diisi oleh load_channel_db()
CHANNEL_DB = {}
CHANNEL_DB_URL = "https://iptv-org.github.io/api/channels.json"


def load_channel_db(timeout):
    import json
    try:
        for c in json.loads(fetch_text(CHANNEL_DB_URL, timeout)):
            CHANNEL_DB[c["id"]] = c
        print(f"[OK] pangkalan data channel: {len(CHANNEL_DB)} rekod")
    except Exception as e:
        print(f"[AMARAN] pangkalan data channel tak dapat dimuat ({e}); kategori ikut tag playlist sahaja")


def categorize(ch, priority):
    tags = {t.strip().lower() for t in ch.attrs.get("group-title", "").split(";")}
    m = CATEGORY_FROM_SOURCE.search(ch.source)
    if m:
        tags.add(m.group(1))
    tags.update(ch.db_info().get("categories", []))
    for cat, words in CATEGORY_RULES:
        if tags & words:
            return cat
    # Tiada tag langsung: cuba teka dari nama channel ("Action Hollywood Movies" -> MOVIES)
    name_words = set(NAME_WORDS.findall(ch.name.lower()))
    for cat, words in CATEGORY_RULES:
        if name_words & words:
            return cat
    # Channel TV am dari negara keutamaan (biasanya "Undefined") masuk ENTERTAINMENT
    return "ENTERTAINMENT" if ch.country() in priority else "LAIN-LAIN"


def organize(channels, priority, main_only=False):
    """Set group-title ikut kategori, dan susun: kategori utama dulu, negara keutamaan dulu."""
    rank = {c: i for i, c in enumerate(priority)}
    result = []
    for ch in channels:
        cat = categorize(ch, priority)
        if main_only and cat not in MAIN_CATEGORIES:
            continue
        ch.set_group(cat)
        result.append(ch)
    result.sort(key=lambda ch: (CATEGORY_ORDER.index(ch.attrs["group-title"]),
                                rank.get(ch.country(), len(priority)),
                                ch.name.lower()))
    return result


def fetch_text(src, timeout):
    if Path(src).is_file():
        return Path(src).read_text(encoding="utf-8", errors="ignore")
    req = urllib.request.Request(src, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read().decode("utf-8", errors="ignore")


def parse_m3u(text, source):
    channels, extinf, options = [], None, []
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.upper().startswith("#EXTM3U"):
            continue
        if line.upper().startswith("#EXTINF"):
            extinf, options = line, []
        elif line.startswith("#"):
            if extinf is not None:
                options.append(line)
        else:
            if extinf is None:
                extinf = f"#EXTINF:-1,{line}"
            channels.append(Channel(extinf, options, line, source))
            extinf, options = None, []
    return channels


def is_live(ch, timeout):
    """True jika stream boleh dibuka dan nampak macam video/HLS sebenar."""
    url = ch.url
    if not url.lower().startswith(("http://", "https://")):
        return True  # rtmp/udp dll. tak boleh disemak dengan HTTP; simpan saja
    headers = ch.headers()
    try:
        for _ in range(3):  # ikut master playlist -> variant playlist
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                if r.status >= 400:
                    return False
                ctype = (r.headers.get("Content-Type") or "").lower()
                head = r.read(4096)
                final_url = r.geturl()
            text = head.decode("utf-8", errors="ignore")
            if text.lstrip().startswith("#EXTM3U"):
                # Playlist HLS: semak ada segmen/variant, dan ikut satu tahap ke bawah
                nxt = next((l.strip() for l in text.splitlines()
                            if l.strip() and not l.startswith("#")), None)
                if nxt is None:
                    return False
                if "#EXT-X-STREAM-INF" in text:
                    url = urljoin(final_url, nxt)
                    continue
                return True
            if any(t in ctype for t in ("text/html", "json", "xml")):
                return False  # biasanya halaman error / login / API, bukan video
            return len(head) > 0
        return True
    except Exception:
        return False


def is_live_retry(ch, timeout, retries):
    """Server IPTV selalu putus sekejap; cuba beberapa kali sebelum anggap mati."""
    return any(is_live(ch, timeout) for _ in range(retries + 1))


def main():
    ap = argparse.ArgumentParser(description="Gabung playlist M3U dan simpan channel yang live sahaja.")
    ap.add_argument("-s", "--sources", default="sources.txt", help="fail senarai sumber (default: sources.txt)")
    ap.add_argument("-o", "--output", default="playlist_live.m3u", help="fail output (default: playlist_live.m3u)")
    ap.add_argument("--workers", type=int, default=40, help="bilangan semakan serentak (default: 40)")
    ap.add_argument("--timeout", type=float, default=10, help="timeout setiap stream dalam saat (default: 10)")
    ap.add_argument("--retries", type=int, default=2, help="cuba semula jika gagal (default: 2)")
    ap.add_argument("--no-check", action="store_true", help="jangan semak live, gabung sahaja")
    ap.add_argument("--priority", default="my,id,sg,au,bn",
                    help="kod negara keutamaan, ikut susunan (default: my,id,sg,au,bn)")
    ap.add_argument("--main-only", action="store_true",
                    help="simpan MOVIES, KIDS, ENTERTAINMENT, SPORTS sahaja")
    ap.add_argument("--no-categories", action="store_true",
                    help="kekalkan group-title asal, jangan susun ikut kategori")
    args = ap.parse_args()

    src_file = Path(args.sources)
    if not src_file.is_file():
        sys.exit(f"Fail sumber '{src_file}' tak dijumpai. Buat fail itu dan letak URL playlist, satu setiap baris.")
    sources = [l.strip() for l in src_file.read_text(encoding="utf-8").splitlines()
               if l.strip() and not l.strip().startswith("#")]

    if not args.no_categories:
        load_channel_db(args.timeout * 6)

    all_channels, seen = [], set()
    for src in sources:
        try:
            chans = parse_m3u(fetch_text(src, args.timeout * 3), src)
        except Exception as e:
            print(f"[GAGAL] {src}: {e}")
            continue
        added = 0
        for ch in chans:
            key = ch.url.strip().rstrip("/")
            if key in seen or ch.db_info().get("is_nsfw"):
                continue
            seen.add(key)
            all_channels.append(ch)
            added += 1
        print(f"[OK] {src}: {len(chans)} channel ({added} baru)")

    print(f"\nJumlah channel unik: {len(all_channels)}")

    if args.no_check:
        live = all_channels
    else:
        print(f"Menyemak status live ({args.workers} serentak, timeout {args.timeout}s)...")
        results = {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(is_live_retry, ch, args.timeout, args.retries): i for i, ch in enumerate(all_channels)}
            for n, fut in enumerate(as_completed(futs), 1):
                results[futs[fut]] = fut.result()
                if n % 50 == 0 or n == len(futs):
                    print(f"  {n}/{len(futs)} disemak, {sum(results.values())} live")
        live = [ch for i, ch in enumerate(all_channels) if results.get(i)]

    if not args.no_categories:
        priority = [c.strip().lower() for c in args.priority.split(",") if c.strip()]
        live = organize(live, priority, args.main_only)
        print("\nRingkasan kategori (jumlah / dari negara keutamaan):")
        for cat in CATEGORY_ORDER:
            in_cat = [ch for ch in live if ch.attrs["group-title"] == cat]
            if in_cat:
                pri = sum(ch.country() in priority for ch in in_cat)
                print(f"  {cat:<14} {len(in_cat):>5} / {pri}")

    out = ["#EXTM3U"] + [ch.to_m3u() for ch in live]
    Path(args.output).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nSiap: {len(live)} channel live disimpan ke {args.output}")


if __name__ == "__main__":
    main()
