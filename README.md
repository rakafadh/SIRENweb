# SIREN

Triase dan pemantauan pasien di dalam ambulans, berjalan offline di Orange Pi 4 Pro dengan layar 7 inci.

Dua ESP32 membaca tanda vital pasien: denyut nadi, SpO₂, laju napas, suhu, dan getaran kendaraan. SIREN menghitung kategori triase **MERAH / KUNING / HIJAU** setiap detik (START + NEWS2), mendeteksi perburukan, lalu menyerahkan ringkasan ke IGD lewat layar, PDF, dan label thermal ber-QR.

**Untuk menjalankan, menanam firmware, dan memasang di Orange Pi, lihat [README_DEPLOY.md](README_DEPLOY.md).**

## Isi folder

```text
SIREN/
├── README.md                 dokumen ini
├── README_DEPLOY.md          panduan uji di laptop, deploy ke Orange Pi, dan checklist UAT
├── siren_app/                aplikasi SIREN (FastAPI + SQLite + tampilan web)
│   ├── app/                  kode server
│   ├── templates/, static/   tampilan
│   ├── models/               model estimasi tekanan darah dan angka akurasinya
│   ├── deploy/               skrip instal Orange Pi (systemd, kiosk, udev)
│   └── tests/                test otomatis
├── siren-sensors/            firmware ESP32 (PlatformIO)
│   ├── src/ppg/              node PPG: MAX30102
│   ├── src/temp/             node suhu: MLX90614 + MPU6050
│   └── tools/                triage.py (algoritma rujukan laporan) dan dashboard uji hardware
└── BloodPressure-Model/      skrip melatih ulang model tekanan darah; hasilnya ditulis ke siren_app/models/
```

## Mulai cepat di laptop

```powershell
cd SIREN\siren_app
py -3.11 -m pip install -r requirements.txt
py -3.11 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Buka http://127.0.0.1:8000. Tanpa ESP32, pilih **Perangkat → Simulasi**.

## Alur di layar

| Halaman | Isi |
|---|---|
| **Pasien baru** | Kesadaran, kriteria START, nyeri, TD manset, identitas (opsional) |
| **Pemantauan** | Kategori live dan alasannya, tanda vital beserta sumbernya, rincian NEWS2, input petugas, catatan tindakan |
| **Serah terima** | Terbuka setelah 7 parameter NEWS2 lengkap (atau ditandai "tidak dapat diukur" dengan alasan). Ringkasan, PDF, label, lalu data dikunci. |
| **Riwayat** | Pasien sebelumnya, ekspor CSV |
| **Perangkat** | Status ESP32 dan printer, pilihan **Sensor sungguhan / Simulasi** |

Tekan `?` di layar mana pun untuk daftar pintasan keyboard.

## Aturan data

- Nilai sensor yang lebih tua dari 5 detik dianggap tidak ada, bukan dibekukan di layar.
- Setiap nilai diberi label asalnya: **SENSOR**, **MANUAL**, atau **SIMULASI**.
- Parameter yang tidak terukur tidak diberi skor 0. NEWS2 menyebut berapa dari 7 parameter yang terukur.
- Nilai manual hanya mengisi parameter yang kosong dari sensor, dan berlaku 15 menit.
- Tekanan darah untuk triase hanya dari manset. Estimasi model dari PPG selalu berlabel eksperimental dan tidak dipakai untuk triase.
- Algoritma triase mengikuti laporan akhir (`siren_app/app/services/triage.py`, identik dengan `siren-sensors/tools/triage.py`). Ada dua penyesuaian yang disepakati tim di `triage_policy.py`: kenaikan NEWS2 hanya dihitung dari parameter yang terukur di kedua waktu, dan kategori turun hanya setelah stabil 30 detik.

## Untuk pengembang

| Keperluan | Perintah atau lokasi |
|---|---|
| Test otomatis | `py -3.11 -m pytest`, dari `siren_app/` |
| Pengaturan | `siren_app/.env`, opsional: semua punya nilai bawaan. Salin dari `siren_app/.env.example`, yang berisi daftar lengkap pengaturan beserta penjelasannya. |
| Mode sensor tersimpan | `siren_app/runtime.json` (diatur dari menu Perangkat) |
| Database | `siren_app/siren.db` (SQLite, WAL) |
| Kode utama | `app/main.py` (route), `app/services/monitor.py` (loop triase 1 detik), `app/services/sensor_service.py` (pembacaan ESP32 dan simulasi) |
| Firmware | `siren-sensors/` dengan PlatformIO: `pio run -e ppg` / `-e temp` |
| Model BP | Dipakai dari `siren_app/models/`. Melatih ulang: `BloodPressure-Model/train_bp_model.py` lalu `evaluate_bp_model.py` (butuh folder `dataset/`); keduanya langsung menimpa file di `siren_app/models/`. |

Konteks lengkap proyek, termasuk keputusan desain, hasil model, dan keterbatasan, ada di `../SIREN_CONTEXT.md`.
