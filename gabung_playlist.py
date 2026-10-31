#!/usr/bin/env python3
"""
Gabung banyak playlist M3U/M3U8 jadi satu, buang channel yang mati (offline),
buang duplikat, dan simpan sebagai satu fail .m3u.

Cara guna:
    python gabung_playlist.py                      # baca sumber dari sources.txt
    python gabung_playlist.py -s sources.txt -o live.m3u --workers 50 --timeout 8
    python gabung_playlist.py --no-check           # gabung sahaja, tak semak live
    python gabung_playlist.py --deep               # buka setiap stream dengan ffprobe + kesan subtitle/CC
    python gabung_playlist.py --deep --subs-only   # simpan channel bersubtitle MS/ID/EN (atau CC) sahaja

sources.txt: satu URL atau path fail .m3u setiap baris. Baris bermula '#' diabaikan.
Tiada library luar diperlukan (Python 3.8+).
"""
import argparse
import csv
import json
import re
import shutil
import ssl
import subprocess
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
        self.subs = set()             # label subtitle yang dikesan: EN, MS, ID, CC, ...

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

    def wanted_subs(self, langs):
        """Subtitle dalam bahasa pilihan, dan closed captions (CC) yang biasanya English."""
        return sorted(s for s in self.subs if s in langs or s == "CC" or s[3:] in langs and s.startswith("CC-"))

    def tag_subs(self, langs):
        """Tambah tanda subtitle pada nama, contoh 'HBO [SUB EN/MS]', supaya nampak dalam player."""
        found = self.wanted_subs(langs)
        if found:
            self.extinf = f"{self.extinf} [SUB {'/'.join(found)}]"

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
    """Set group-title ikut kategori, dan susun: kategori utama dulu, negara keutamaan dulu,
    kemudian channel yang ada subtitle."""
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
                                0 if ch.subs else 1,
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


# Kod bahasa subtitle -> label pendek (ISO 639-1/2 dan variasi biasa)
LANG_LABELS = {
    "en": "EN", "eng": "EN", "english": "EN",
    "ms": "MS", "msa": "MS", "may": "MS", "zsm": "MS", "malay": "MS", "melayu": "MS",
    "id": "ID", "ind": "ID", "indonesian": "ID", "bahasa": "ID",
}
MEDIA_TAG = re.compile(r"#EXT-X-MEDIA:(.*)")


def lang_label(lang):
    lang = (lang or "").strip().lower()
    return LANG_LABELS.get(lang) or LANG_LABELS.get(lang.split("-")[0]) or (lang.upper() or "?")


def http_get(url, headers, timeout, size):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return (r.headers.get("Content-Type") or "").lower(), r.read(size), r.geturl()


def probe(ch, timeout):
    """Semak stream boleh dimuat: ikut HLS sampai ke segmen video sebenar dan muat turun sedikit.

    Pulangkan (ok, url_media, set_subtitle). Subtitle dibaca dari tag #EXT-X-MEDIA dalam master playlist.
    """
    url, subs = ch.url, set()
    if not url.lower().startswith(("http://", "https://")):
        return True, url, subs  # rtmp/udp dll. tak boleh disemak dengan HTTP; simpan saja
    headers = ch.headers()
    try:
        for _ in range(4):  # master -> variant -> segmen
            ctype, body, final_url = http_get(url, headers, timeout, 256 * 1024)
            text = body.decode("utf-8", errors="ignore")
            if not text.lstrip().startswith("#EXTM3U"):
                if any(t in ctype for t in ("text/html", "json", "xml")):
                    return False, url, subs  # halaman error / login / API, bukan video
                return len(body) > 0, url, subs
            for m in MEDIA_TAG.finditer(text):
                attrs = dict(ATTR_RE.findall(m.group(1)))
                kind = re.search(r"TYPE=([A-Z-]+)", m.group(1))
                if kind and kind.group(1) == "SUBTITLES":
                    subs.add(lang_label(attrs.get("LANGUAGE") or attrs.get("NAME")))
                elif kind and kind.group(1) == "CLOSED-CAPTIONS":
                    subs.add("CC-" + lang_label(attrs.get("LANGUAGE")) if attrs.get("LANGUAGE") else "CC")
            uris = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
            if not uris:
                return False, url, subs
            if "#EXT-X-STREAM-INF" in text:
                url = urljoin(final_url, uris[0])
                continue
            # Playlist media: muat turun segmen terkini (yang lama mungkin dah dibuang server)
            ctype, seg, _ = http_get(urljoin(final_url, uris[-1]), headers, timeout, 2048)
            ok = len(seg) > 0 and not any(t in ctype for t in ("text/html", "json"))
            return ok, url, subs
        return False, url, subs
    except Exception:
        return False, url, subs


def probe_retry(ch, timeout, retries):
    """Server IPTV selalu putus sekejap; cuba beberapa kali sebelum anggap mati."""
    for _ in range(retries + 1):
        result = probe(ch, timeout)
        if result[0]:
            return result
    return result


def ffprobe_check(ch, url, timeout, ffprobe):
    """Buka stream dengan ffprobe. Pulangkan (ok, set_subtitle): ok jika ada trek video/audio yang boleh dibaca."""
    h = ch.headers()
    cmd = [ffprobe, "-v", "error", "-rw_timeout", str(int(timeout * 1_000_000)),
           "-user_agent", h["User-Agent"], "-probesize", "3000000", "-analyzeduration", "4000000"]
    if "Referer" in h:
        cmd += ["-referer", h["Referer"]]
    if "Origin" in h:
        cmd += ["-headers", f"Origin: {h['Origin']}\r\n"]
    cmd += ["-show_streams", "-of", "json", url]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout * 3).stdout
        streams = json.loads(out or b"{}").get("streams", [])
    except Exception:
        return False, set()
    subs = set()
    for s in streams:
        if s.get("codec_type") == "subtitle":
            subs.add(lang_label(s.get("tags", {}).get("language")))
        if s.get("codec_type") == "video" and s.get("closed_captions"):
            subs.add("CC")
    return any(s.get("codec_type") in ("video", "audio") for s in streams), subs


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
    ap.add_argument("--deep", action="store_true",
                    help="buka setiap stream dengan ffprobe: pastikan video/audio boleh dibaca dan kesan subtitle/CC")
    ap.add_argument("--sub-langs", default="ms,id,en",
                    help="bahasa subtitle yang dicari (default: ms,id,en)")
    ap.add_argument("--subs-only", action="store_true",
                    help="simpan channel yang ada subtitle bahasa pilihan (atau CC) sahaja")
    ap.add_argument("--report", default="laporan_channel.csv",
                    help="fail laporan CSV: kategori, negara, subtitle setiap channel (default: laporan_channel.csv)")
    args = ap.parse_args()
    sub_langs = {lang_label(l) for l in args.sub_langs.split(",") if l.strip()}

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
        print(f"Menyemak status live hingga ke segmen video ({args.workers} serentak, timeout {args.timeout}s)...")
        media_url, live = {}, []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(probe_retry, ch, args.timeout, args.retries): ch for ch in all_channels}
            for n, fut in enumerate(as_completed(futs), 1):
                ch = futs[fut]
                ok, media_url[id(ch)], ch.subs = fut.result()
                if ok:
                    live.append(ch)
                if n % 100 == 0 or n == len(futs):
                    print(f"  {n}/{len(futs)} disemak, {len(live)} live")

        ffprobe = shutil.which("ffprobe") if args.deep else None
        if args.deep and not ffprobe:
            print("[AMARAN] ffprobe tak dijumpai; semakan --deep dilangkau. Pasang FFmpeg untuk guna pilihan ini.")
        if ffprobe:
            # ffprobe lebih berat (satu proses setiap stream), jadi hadkan bilangan serentak
            workers = min(args.workers, 32)
            print(f"\nMembuka setiap stream dengan ffprobe ({workers} serentak)...")
            playable = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(ffprobe_check, ch, media_url[id(ch)], args.timeout, ffprobe): ch for ch in live}
                for n, fut in enumerate(as_completed(futs), 1):
                    ch = futs[fut]
                    ok, subs = fut.result()
                    if ok:
                        ch.subs |= subs
                        playable.append(ch)
                    if n % 100 == 0 or n == len(futs):
                        print(f"  {n}/{len(futs)} dibuka, {len(playable)} boleh dimainkan")
            live = playable

    with_subs = [ch for ch in live if ch.wanted_subs(sub_langs)]
    print(f"\nChannel dengan subtitle {'/'.join(sorted(sub_langs))} atau CC: {len(with_subs)}")
    for label in sorted({s for ch in live for s in ch.subs}):
        print(f"  {label:<6} {sum(label in ch.subs for ch in live)}")
    if args.subs_only:
        live = with_subs
    for ch in live:
        ch.tag_subs(sub_langs)

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
    if args.report:
        with open(args.report, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["kategori", "negara", "nama", "subtitle", "url"])
            for ch in live:
                w.writerow([ch.attrs.get("group-title", ""), ch.country().upper(), ch.name,
                            " ".join(sorted(ch.subs)), ch.url])
        print(f"Laporan disimpan ke {args.report}")
    print(f"\nSiap: {len(live)} channel live disimpan ke {args.output}")


if __name__ == "__main__":
    main()
