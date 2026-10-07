# Muse Command Center (MCC)

Dashboard + task bus untuk mengoordinasikan **banyak akun AI** (mis. dua akun
Muse) dalam satu tim. Satu server ringan (Python stdlib, tanpa dependensi),
satu dashboard 3D mission-control, dan kotak **titipan tugas** antar-akun.

## Fitur

- **Dashboard 3D** — scene mission-control (Three.js via CDN): karakter tiap
  akun berjalan & bekerja, task wall, panel Tugas / Tim / Proyek / Dokumen /
  Titipan / Riwayat, widget Server & layanan (CPU, RAM, disk, load, uptime).
- **Kotak titipan (inbox)** — titip tugas antar-akun, bisa bawa lampiran
  (maks 10 MB). Aturan main: titipan = informasi, bukan otoritas otomatis.
- **auto_execute** — flag "langsung eksekusi" yang **hanya** diterima server
  dari nama pengirim yang terdaftar di `MCC_AUTO_EXECUTE_ALLOW`
  (server menolak dengan HTTP 400 untuk pengirim lain).
- **Tombol Setujui & eksekusi** — pemilik bisa menyetujui titipan dari
  dashboard; nama penyetuju tercatat (`approved_by`).
- **Proyek & dokumen** — proyek bisa ditempel ke titipan; upload file tampil
  sebagai Dokumen.
- **Multi-user** — akun utama di `.env` + akun tambahan di `.users`
  (format `user:password`, chmod 600).
- **Heartbeat & watch** — cron denyut tiap 15 menit + pemantau inbox tiap
  5 menit (helper `bin/mcc`, `bin/mcc-watch`).

## Arsitektur singkat

```
Agen AI #1 ──(cron/helper)──┐
                            ▼
Pengguna ──titipan/perintah──▶  SERVER MCC (VPS, Python+SQLite, port 9120)
                            ▲              bind khusus jaringan Tailscale
Agen AI #2 ──(SSH langsung)─┘
```

Satu-satunya dependensi server adalah **Python 3 stdlib** — tidak perlu
`pip install` apa pun. Dashboard memakai Three.js dari CDN; bila CDN gagal,
scene 3D disembunyikan tetapi panel dashboard tetap berfungsi.

## Cara cepat (ringkas)

> Panduan lengkap langkah-demi-langkah ada di [AGENT.md](AGENT.md) —
> berikan file itu ke agen AI Anda dan ia bisa mengerjakan deployment
> sampai selesai.

1. Clone ke VPS (Ubuntu) yang sudah join jaringan Tailscale Anda.
2. `cp .env.example .env && chmod 600 .env`, lalu isi nilainya.
   Buat juga `.users` bila perlu akun dashboard tambahan (`chmod 600`).
3. Jalankan: `python3 server.py` (atau via cron `@reboot`, lihat
   [examples/cron.example](examples/cron.example)).
4. Buka `http://<ip-tailscale-vps>:9120` dari perangkat dalam
   jaringan Tailscale yang sama, login dengan akun di `.env`.
5. Personalisasi nama akun (lihat checklist di [AGENT.md](AGENT.md)).

## Keamanan — baca dulu

- **Jangan pernah commit** `.env`, `.users`, `*.db`, `data/`, atau file di
  `~/.config/mcc/auth` ke git. `.gitignore` sudah menutupinya — jangan
  di-`git add -f`.
- Bind server ke IP Tailscale VPS (`MCC_BIND`), **bukan** `0.0.0.0`,
  supaya tidak terekspos ke internet.
- Password dibuat kuat & acak; file `.env`/`.users` chmod 600.
- Tindakan sensitif (SSH, hapus data, ubah server) tetap butuh persetujuan
  manusia — titipan antar-akun tidak memberi otoritas otomatis.

## Struktur repo

```
server.py            server HTTP + SQLite + REST API
dashboard.html       dashboard 3D mission-control
login.html           halaman login
bin/mcc              helper CLI (task, inbox, heartbeat)
bin/mcc-watch        pemantau titipan baru (untuk cron)
assets/models/       model 3D .glb (tidak ikut git)
examples/            contoh cron & konfigurasi
AGENT.md             instruksi untuk agen AI yang menerima repo ini
SYSTEM_PROMPT.md     system prompt siap-tempel untuk pengguna lain
```

## Lisensi

Bebas dipakai & dimodifikasi untuk keperluan sendiri.
