# Playlist OTT

Playlist M3U gabungan siaran percuma (free-to-air dan FAST) yang disemak live.
Disusun ikut kategori **MOVIES â†’ KIDS â†’ ENTERTAINMENT â†’ SPORTS â†’ lain-lain**, dengan channel dari
Malaysia, Indonesia, Singapura, Australia dan Brunei didahulukan dalam setiap kategori.

## Guna dalam OTT / player

Masukkan URL raw ini dalam player (TiviMate, IPTV Smarters, VLC, Kodi dan lain-lain):

```
https://raw.githubusercontent.com/dexxia/mkbdk/main/playlist_live.m3u
```

## Kemas kini playlist

```bash
python gabung_playlist.py --workers 120 --timeout 8 --retries 1
git add playlist_live.m3u
git commit -m "Kemas kini playlist"
git push
```

- `sources.txt`: senarai sumber playlist (satu URL setiap baris)
- `--main-only`: simpan MOVIES, KIDS, ENTERTAINMENT, SPORTS sahaja
- `--priority my,id,sg,au,bn`: susunan negara keutamaan

Status live disemak dari rangkaian tempat skrip dijalankan; channel geo-block mungkin tak boleh
dimainkan dari negara lain.

Sumber: [iptv-org](https://github.com/iptv-org/iptv), [Free-TV](https://github.com/Free-TV/IPTV), [i.mjh.nz](https://i.mjh.nz).
