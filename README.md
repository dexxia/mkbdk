# Playlist OTT

![NxmTV](assets/logo.png)

Playlist M3U gabungan siaran percuma (free-to-air dan FAST) yang disemak live.
Disusun ikut kategori **MOVIES → KIDS → ENTERTAINMENT → SPORTS → lain-lain → NOSUB**, dengan channel
dari Malaysia, Indonesia, Singapura, Australia dan Brunei didahulukan dalam setiap kategori.

- Kategori biasa hanya mengandungi channel bersubtitle English, Melayu atau Indonesia (atau closed
  captions), ditanda `[SUB ...]` pada nama.
- Channel tanpa subtitle dikumpul dalam kategori **NOSUB** di bawah sekali.
- Setiap kategori dimulakan dengan entri berlogo `★ NxmTV • KATEGORI ★`.

## Guna dalam OTT / player

Masukkan URL raw ini dalam player (TiviMate, IPTV Smarters, VLC, Kodi dan lain-lain):

```
https://raw.githubusercontent.com/dexxia/mkbdk/main/playlist_live.m3u
```

## Kemas kini automatik

GitHub Actions ([update-playlist.yml](.github/workflows/update-playlist.yml)) menjana semula playlist
setiap hari jam 04:00 waktu Malaysia dan push jika ada perubahan. Untuk jalankan sekarang: tab
**Actions → Kemas kini playlist → Run workflow**.

Nota: pelayan GitHub Actions berada di luar negara (biasanya AS), jadi channel yang geo-block untuk
Malaysia/Australia mungkin tercicir manakala channel AS lebih banyak lulus.

## Kemas kini manual

```bash
python gabung_playlist.py --deep --nosub-group --workers 120 --timeout 8 --retries 1
git pull
git add playlist_live.m3u laporan_channel.csv
git commit -m "Kemas kini playlist"
git push
```

- `sources.txt`: senarai sumber playlist (satu URL setiap baris)
- `--deep`: buka setiap stream dengan ffprobe (perlu FFmpeg) untuk pastikan boleh dimainkan dan kesan subtitle/CC
- `--nosub-group`: channel tanpa subtitle MS/ID/EN (atau CC) masuk kategori NOSUB; `--sub-langs` untuk tukar bahasa
- `--subs-only`: buang terus channel tanpa subtitle (ganti NOSUB)
- `--brand`, `--brand-logo`, `--brand-video`, `--no-brand`: entri tajuk berlogo setiap kategori
- `--main-only`: simpan MOVIES, KIDS, ENTERTAINMENT, SPORTS sahaja
- `--priority my,id,sg,au,bn`: susunan negara keutamaan

Status live disemak dari rangkaian tempat skrip dijalankan; channel geo-block mungkin tak boleh
dimainkan dari negara lain.

Sumber: [iptv-org](https://github.com/iptv-org/iptv), [Free-TV](https://github.com/Free-TV/IPTV), [i.mjh.nz](https://i.mjh.nz).
