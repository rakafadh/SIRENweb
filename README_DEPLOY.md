# SIREN · Panduan Uji dan Deploy

Satu dokumen untuk membawa SIREN dari laptop ke ambulans: menjalankan aplikasi, menanam firmware ke ESP32, memasang di Orange Pi 4 Pro, dan memastikan semuanya siap lewat checklist UAT.

```text
 BAGIAN A · LAPTOP (Windows)                      BAGIAN B · ORANGE PI 4 PRO (Ubuntu)
 ─────────────────────────────                    ───────────────────────────────────
 A1 Siapkan laptop                                B1 Siapkan Orange Pi
 A2 Jalankan SIREN (simulasi)                     B2 Salin SIREN ke Orange Pi
 A3 Flash firmware ke kedua ESP32  ──────────►    B3 Instal dan reboot
 A4 Uji SIREN dengan sensor sungguhan             B4 Sambungkan ESP32 dan printer
 A5 Kalau ada masalah                             B5 Pemakaian sehari-hari
                                                  B6 Kalau ada masalah
                         BAGIAN C · CHECKLIST UAT (sebelum dipakai di ambulans)
```

**Isi folder `SIREN/`**

| Folder | Isinya | Dipakai di |
|---|---|---|
| `siren_app/` | Aplikasi SIREN: server, tampilan, database, dan model estimasi tekanan darah (`models/`) | Laptop dan Orange Pi |
| `siren-sensors/` | Firmware kedua ESP32 | Laptop saja, untuk flash |
| `BloodPressure-Model/` | Skrip untuk melatih ulang model; hasilnya otomatis ditulis ke `siren_app/models/` | Laptop saja, bila melatih ulang |

---

## Bagian A · Uji di laptop

### A1. Siapkan laptop (sekali saja)

| Yang dibutuhkan | Cara cek |
|---|---|
| Python 3.11 | `py -3.11 --version` |
| VS Code + ekstensi **PlatformIO IDE** | Ikon semut di sidebar VS Code |
| Driver USB-serial ESP32 (CP210x atau CH340, sesuai chip board) | Board muncul sebagai `COMx` di Device Manager |
| Kabel USB **data** (bukan kabel isi daya saja) | Board terdeteksi saat dicolok |

Pasang paket SIREN, dari folder `SIREN\siren_app`:

```powershell
py -3.11 -m pip install -r requirements.txt
```

> Selalu pakai `py -3.11`, bukan `python`. intinya make sure gunakan python 3.11

### A2. Jalankan SIREN

```powershell
cd "SIREN\siren_app"
py -3.11 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Buka **http://127.0.0.1:8000**. Hentikan dengan `Ctrl+C` di terminal.

- **Tanpa ESP32:** buka menu **Perangkat** → *Sumber data sensor* → **Simulasi**. Pilih *Pasien stabil* atau *Pasien memburuk (5 menit)*.
- Pilihan mode tersimpan di `siren_app/runtime.json`, dan **hanya bisa diganti saat tidak ada pasien aktif**.
- Label printer tanpa printer: di `.env` isi `PRINTER_BACKEND=file` dan `PRINTER_DEVICE=prints`. Label akan tersimpan sebagai PNG di `siren_app/prints/`.
- Test otomatis: `py -3.11 -m pytest`. Semua harus lulus.

### A3. Flash firmware ke kedua ESP32

Firmware cukup ditanam **sekali** dari laptop. Setelah itu ESP32 menjalankannya sendiri setiap kali mendapat daya, termasuk saat dicolok ke Orange Pi. Flash ulang hanya bila kode firmware diubah.

| Board | Sensor | Environment PlatformIO | Pin I²C |
|---|---|---|---|
| ESP32 DevKit (klasik) | MAX30102 | `ppg` | SDA 26 · SCL 14 |
| ESP32-S3 | MLX90614 + MPU6050 | `temp` | SDA 4 · SCL 5 |

1. Di VS Code buka folder **`SIREN\siren-sensors`** (folder yang berisi `platformio.ini`).
2. Colok **satu board saja**, supaya port tidak tertukar.
3. Upload dari terminal PlatformIO (ikon semut → *New Terminal*):
   ```powershell
   pio run -e temp -t upload     # board suhu (ESP32-S3)
   pio run -e ppg  -t upload     # board PPG (ESP32 klasik), setelah board suhu dicabut
   ```
   Bila `pio` tidak dikenali, ganti `pio` dengan `& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe"`.
4. Cek keluaran tiap board dengan `pio device monitor -e ppg` atau `-e temp`. Tutup monitor sebelum menjalankan SIREN, karena satu port hanya bisa dibuka satu program.

**Keluaran yang benar:**

| Board | Contoh baris | Cek |
|---|---|---|
| PPG | `52000,24.8,60,120000,0.010,1.20,76.4,b,…` lalu `W,52000,24.80,119873,119802,…` | Baris `W,` muncul setelah setiap baris angka, berisi 25 nilai. Nilainya naik-turun pelan saat jari diam di sensor. |
| Suhu | `T,8000,35.40,29.50,36.60,35.20,0.0210,0.0700,1.90,7` | Muncul tiap 1 detik. Nilai terakhir `7` berarti semua sensor terdeteksi (`5` = belum stabil, `4` = MLX tidak terdeteksi). |

### A4. Uji SIREN dengan sensor sungguhan

1. Colok kedua ESP32 ke laptop, lalu jalankan SIREN (A2).
2. Menu **Perangkat** → *Sumber data sensor* → **Sensor sungguhan**.
3. Dalam beberapa detik, **Node PPG** dan **Node suhu** harus *Terhubung* dengan port masing-masing. Urutan colok tidak berpengaruh; SIREN mengenali board dari datanya.
4. Tempel jari di MAX30102 dan arahkan MLX90614 ke dahi:

| Nilai | Muncul setelah |
|---|---|
| Nadi, SpO₂ | Beberapa detik |
| Suhu | ±3 detik |
| Laju napas | ±30 detik |
| Estimasi TD (PPG) | ±30 detik gelombang |

### A5. Kalau ada masalah di laptop

| Gejala | Penyebab | Solusi |
|---|---|---|
| `ModuleNotFoundError` atau error numpy | Paket belum terpasang, atau tidak sengaja memakai Python 3.10 | Jalankan ulang A1 dengan `py -3.11` |
| `address already in use` / port 8000 terpakai | SIREN lain masih berjalan | Tutup terminal SIREN sebelumnya, atau pakai `--port 8001` |
| Server menolak start: "skema lama" | `siren.db` dibuat versi lama SIREN | Pindahkan `siren.db` sebagai cadangan, lalu jalankan ulang |
| Upload gagal: *Failed to connect* | Board belum masuk mode upload | Tahan tombol **BOOT** di board saat upload dimulai, lepas setelah tulisan *Connecting* berganti |
| Upload ke board yang salah | Dua board tercolok bersamaan | Colok satu board saja, atau tambahkan `--upload-port COM5` |
| Board tidak muncul di Device Manager | Kabel hanya untuk daya, atau driver belum terpasang | Ganti kabel data; pasang driver CP210x atau CH340 |
| Port *Gagal dibuka* di menu Perangkat | Port dipakai program lain (misalnya `pio device monitor`) | Tutup program itu, lalu **Pindai ulang port** |
| Port terbuka tapi *Belum dikenali* | Firmware lama atau baud salah | Flash ulang (A3); baud harus 115200 |
| Estimasi TD: "Menunggu gelombang PPG…" | Firmware PPG belum mengirim baris `W,` | Flash ulang board PPG |
| Suhu kosong padahal board suhu terhubung | Sensor tidak mengarah ke pasien; nilai di bawah 30 °C diabaikan | Arahkan ke dahi dari jarak dekat |

---

## Bagian B · Deploy ke Orange Pi 4 Pro

### B1. Siapkan Orange Pi (sekali saja)

1. **Pasang Ubuntu versi desktop** dari situs resmi Orange Pi (image khusus Orange Pi 4 Pro) ke kartu microSD atau eMMC, misalnya dengan balenaEtcher. Versi desktop wajib, karena tampilan kiosk butuh layar grafis.
2. **Layar 7 inci**: pastikan tampil di 1024×600 dan orientasinya benar (*Settings → Displays*).
3. **Login otomatis**: SIREN baru muncul setelah user login. Bila Orange Pi berhenti di layar login, aktifkan:
   - GNOME: *Settings → Users → Automatic Login*
   - LightDM: buat file `/etc/lightdm/lightdm.conf.d/50-autologin.conf` berisi
     ```ini
     [Seat:*]
     autologin-user=orangepi
     ```
4. **Internet sekali saja** untuk instalasi (Wi-Fi atau LAN). Setelah instalasi, SIREN berjalan offline penuh.
5. **Jam**: semua jam di linimasa serah terima memakai jam Orange Pi. Tanpa baterai RTC, jam akan salah saat offline. Cek dengan `timedatectl` dan `sudo hwclock -r`. Bila RTC tidak aktif, pasang baterai RTC atau modul DS3231.

### B2. Salin SIREN ke Orange Pi

Orange Pi hanya butuh **satu folder: `siren_app/`**. Model estimasi tekanan darah sudah ada di dalamnya (`siren_app/models/`). Salin ke folder **tanpa spasi**, misalnya:

```text
/home/orangepi/SIREN/
└── siren_app/      seluruh isi, KECUALI data laptop di bawah ini
```

**Jangan ikut disalin:** `siren.db*` (data pasien laptop), `runtime.json` (bisa membuat Orange Pi start dalam mode simulasi), `prints/`, `siren_legacy_backup_*.db`, `__pycache__/`, `.pytest_cache/`. Folder `siren-sensors/`, `BloodPressure-Model/`, dan `dataset/` juga tidak perlu.

Cara cepat menyalin ke flashdisk (misalnya drive `F:`) dari folder `Despro` di laptop:

```powershell
robocopy "SIREN\siren_app" "F:\SIREN\siren_app" /E /XD __pycache__ .pytest_cache prints .venv /XF siren.db siren.db-shm siren.db-wal runtime.json siren_legacy_backup_*.db
```

Pastikan `F:\SIREN\siren_app\models\` berisi `bp_model.joblib` dan `bp_model_metrics.json`. Tanpa keduanya SIREN tetap jalan, tetapi kotak *Estimasi TD (PPG)* akan tertulis "Model BP tidak ditemukan".

Lalu di Orange Pi, salin folder `SIREN` dari flashdisk ke `/home/orangepi/`.

### B3. Instal dan reboot

Di terminal Orange Pi, sebagai user desktop (bukan root):

```bash
cd /home/orangepi
bash SIREN/siren_app/deploy/install.sh
sudo reboot
```

Skrip ini memasang Python dan paket, Chromium, akses port USB (grup `dialout` dan `lp`, aturan udev, penghapusan `brltty` yang suka "merebut" chip CH340), zona waktu Asia/Jakarta, service server yang start otomatis, dan kiosk layar penuh saat login.

Setelah reboot, layar langsung menampilkan SIREN. Cek dari terminal: `systemctl status siren` harus *active (running)*.

### B4. Sambungkan ESP32 dan printer

**ESP32**: colok kedua board (yang sudah di-flash di A3) ke USB Orange Pi. Di menu **Perangkat**, kedua node harus *Terhubung*. Pastikan *Sumber data sensor* = **Sensor sungguhan**.

**Printer label (thermal 58 mm, ESC/POS)**: cari tahu cara printer tersambung:

```bash
ls /dev/usb/lp*            # ada → printer USB             → PRINTER_DEVICE=/dev/usb/lp0
ls -l /dev/serial/by-id/   # ada entri baru → USB-serial    → pakai path by-id lengkapnya
```

Bila tidak ada keduanya, printer hanya bisa lewat Bluetooth:

```bash
bluetoothctl                                 # lalu: scan on → pair <MAC> → trust <MAC> → quit
sudo rfcomm bind 0 <MAC> 1                   # → PRINTER_DEVICE=/dev/rfcomm0
```

Supaya `rfcomm bind` aktif setiap boot, buat `/etc/systemd/system/siren-rfcomm.service`:

```ini
[Unit]
Description=Bluetooth serial untuk printer SIREN
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/rfcomm bind 0 <MAC> 1
ExecStop=/usr/bin/rfcomm release 0

[Install]
WantedBy=multi-user.target
```

Lalu jalankan `sudo systemctl enable --now siren-rfcomm`.

Isi `SIREN/siren_app/.env`. Bila file itu belum ada (misalnya SIREN diambil dari GitHub), buat dulu dari contohnya: `cp SIREN/siren_app/.env.example SIREN/siren_app/.env`. Lalu restart server:

```env
PRINTER_BACKEND=escpos
PRINTER_DEVICE=/dev/usb/lp0
```

```bash
sudo systemctl restart siren
```

Uji dari menu **Perangkat → Cetak uji**.

> **Belum didukung:** printer saku "cat printer" (Phomemo, PeriPage, GB01/MX05, dan sejenisnya) yang hanya bisa dipakai lewat aplikasi HP *iPrint* atau *Tiny Print*. Printer seperti ini tidak membuat `/dev/usb/lp*` maupun `/dev/rfcomm0` yang bisa dicetak. Catat merek dan modelnya bila printer Anda termasuk jenis ini.
>
> **Daya:** printer menarik arus besar saat mencetak. Jalur dayanya harus terpisah dari Orange Pi (buck converter + kapasitor), supaya Orange Pi tidak restart.

### B5. Pemakaian sehari-hari

| Keperluan | Caranya |
|---|---|
| Menyalakan | Nyalakan Orange Pi; SIREN tampil sendiri |
| Keluar dari layar penuh | `Alt+F4`; buka lagi lewat ikon **SIREN** di desktop |
| Restart server | `sudo systemctl restart siren` |
| Melihat log | `journalctl -u siren -f` |
| Mode simulasi untuk demo | Menu **Perangkat** → **Simulasi** (hanya saat tidak ada pasien aktif). Kembalikan ke **Sensor sungguhan** sebelum dipakai di ambulans. |
| Daftar pintasan keyboard | Tekan `?` di layar mana pun |
| Notifikasi pasien memburuk | Banner kuning (NEWS2 naik ≥3, bunyi sekali) atau merah (menjadi MERAH, alarm berulang). Tekan **Saya mengerti** atau `K`. Bila tertulis "bunyi belum aktif", sentuh layar sekali. |
| PDF serah terima | Tersimpan di `~/Downloads`; salin ke flashdisk bila perlu |
| Data mentah | Menu **Riwayat** → ekspor CSV |
| Cadangan data | Saat server mati, salin `siren.db` dan `siren.db-wal` |
| Memperbarui SIREN | Salin ulang `siren_app/` (tanpa menimpa `siren.db`, `.env`, `runtime.json`), lalu jalankan `bash SIREN/siren_app/deploy/install.sh` |

### B6. Kalau ada masalah di Orange Pi

| Gejala | Penyebab | Solusi |
|---|---|---|
| Error `$'\r': command not found` saat instal | File tersalin dengan akhir baris Windows | `sed -i 's/\r$//' SIREN/siren_app/deploy/*.sh`, lalu ulangi B3 |
| Setelah reboot berhenti di layar login | Login otomatis belum aktif | B1 langkah 3 |
| SIREN tidak muncul, desktop kosong | Server gagal start | `journalctl -u siren -n 50`; baca baris error terakhir |
| Server menolak start: "skema lama" | `siren.db` dari versi lama ikut tersalin | Pindahkan `siren.db` sebagai cadangan, lalu `sudo systemctl restart siren` |
| Halaman "tidak dapat dihubungi" sesaat setelah boot | Server masih start | Tunggu beberapa detik; kiosk menunggu hingga 90 detik |
| Port ESP32 tidak muncul | `brltty`, kabel daya saja, atau grup `dialout` belum aktif | Jalankan ulang B3, ganti kabel data, lalu reboot |
| Port *Gagal dibuka: permission denied* | User belum masuk grup `dialout` | Perintah `groups` harus memuat `dialout`; reboot |
| SIREN start dalam mode simulasi | `runtime.json` dari laptop ikut tersalin | Menu **Perangkat** → **Sensor sungguhan** |
| Jam di linimasa salah | Tidak ada RTC | B1 langkah 5 |
| "Printer belum diatur" | `.env` belum diisi | B4 |
| "Printer tidak ditemukan di …" | Printer mati, kabel lepas, atau Bluetooth belum di-bind | Nyalakan printer; cek `ls /dev/usb/lp*` atau `rfcomm` |
| Cetak berhasil tapi kertas kosong atau huruf acak | Printer tidak memahami ESC/POS raster, atau baud salah | Coba `PRINTER_BAUD=115200`; bila tetap gagal, catat model printer |
| Label terpotong di kanan | Lebar cetak printer kurang dari 384 titik | `PRINTER_WIDTH_PX=360` |
| Orange Pi restart saat mencetak | Tegangan turun karena printer | Pisahkan jalur daya printer |

---

## Bagian C · Checklist UAT

Lakukan di Orange Pi yang sudah terpasang di ambulans, dengan kedua ESP32 dan printer tersambung. Gunakan mode **Sensor sungguhan**, kecuali disebut lain. Centang kolom terakhir bila hasilnya sesuai.

**Penguji:** ______________________ **Tanggal:** ____________ **Unit ambulans:** ____________

### C1. Perangkat dan start

| No | Uji | Hasil yang diharapkan | ✓ |
|---|---|---|---|
| 1 | Nyalakan Orange Pi, ukur dengan stopwatch | **Beranda** SIREN tampil layar penuh dalam **< 60 detik**, tanpa login manual, dengan menu Pantau Pasien, Riwayat, Perangkat | ☐ |
| 2 | Lihat jam di kanan atas | Sama dengan jam sebenarnya (WIB) | ☐ |
| 3 | Lihat status di bar atas | Titik **PPG**, **Suhu**, **Printer** hijau; tidak ada badge **SIMULASI** | ☐ |
| 4 | Menu **Perangkat** | Kedua node *Terhubung*; *Sumber data sensor* = **Sensor sungguhan** | ☐ |
| 5 | **Perangkat → Cetak uji** | Label "UJI" tercetak dalam < 3 detik | ☐ |

### C2. Sensor

| No | Uji | Hasil yang diharapkan | ✓ |
|---|---|---|---|
| 6 | Tempel jari di MAX30102 | Nadi dan SpO₂ muncul dalam beberapa detik, berlabel **SENSOR** | ☐ |
| 7 | Tahan jari 30 detik | Laju napas dan Estimasi TD (PPG) muncul | ☐ |
| 8 | Arahkan MLX90614 ke dahi | Suhu muncul dalam ±3 detik | ☐ |
| 9 | Angkat jari | Nilai PPG kosong, tertulis "Jari tidak terdeteksi" | ☐ |
| 10 | Cabut kabel USB node PPG, lalu colok lagi | Dalam 5 detik nilai hilang dan titik merah; setelah dicolok, tersambung sendiri dalam ±3 detik | ☐ |

### C3. Alur pasien dan triase

| No | Uji | Hasil yang diharapkan | ✓ |
|---|---|---|---|
| 11 | **Pantau Pasien**, lalu mulai tanpa nama | Diterima sebagai "Tanpa identitas" dengan kode SRN otomatis | ☐ |
| 12 | Isi usia 150 lalu simpan | Kolom ditandai merah: "Usia maksimal 120 tahun." | ☐ |
| 13 | Tekan `U` (tidak responsif) | Kategori **MERAH**, alasan "tidak responsif"; notifikasi punya tombol **Urungkan** | ☐ |
| 14 | Tekan **Urungkan** | Kategori langsung kembali ke sebelumnya | ☐ |
| 15 | Centang oksigen tambahan pada pasien NEWS2 3 | Kategori naik **satu** tingkat saja, tidak dua | ☐ |
| 16 | Isi TD manset 88/55 | Skor sistolik 3; shock index dihitung | ☐ |
| 17 | Mode simulasi, skenario *Pasien memburuk*, pantau 5 menit | Kategori naik bertahap; muncul "NEWS2 naik … dalam 60 detik" | ☐ |
| 18 | Saat kategori turun dari MERAH | Tertulis "Ditahan…" selama 30 detik, tidak berkedip | ☐ |

### C4. Kelengkapan dan serah terima

| No | Uji | Hasil yang diharapkan | ✓ |
|---|---|---|---|
| 19 | Pasien tanpa TD manset, tekan **Serah terima** | Halaman tidak terbuka; daftar periksa menunjukkan "TD sistolik manset" | ☐ |
| 20 | Tandai suhu "tidak dapat diukur" tanpa alasan | Ditolak: "Tulis alasan minimal 3 karakter." | ☐ |
| 21 | Lengkapi data, buka serah terima | Ringkasan, linimasa, dan catatan tampil lengkap | ☐ |
| 22 | **Selesai serah terima** (tekan dua kali) | Data terkunci; tombol **Pasien baru** muncul | ☐ |
| 23 | Buka **Perangkat** saat ada pasien aktif, coba ganti *Sumber data sensor* | Toggle abu-abu dan tidak bisa diklik, tertulis "Terkunci: ada pasien aktif (SRN-…)" | ☐ |

### C5. Dokumen serah terima

| No | Uji | Hasil yang diharapkan | ✓ |
|---|---|---|---|
| 24 | **Ekspor PDF** | PDF tersimpan; berisi kategori, vital, TD manset, peringatan, linimasa, dan alasan "tidak dapat diukur" | ☐ |
| 25 | **Cetak label** | Tercetak dalam < 3 detik; kategori, nama, vital, dan jam terbaca | ☐ |
| 26 | Pindai QR label dengan **3 HP berbeda**, jarak 10–30 cm | Ringkasan teks terbaca di ketiga HP tanpa internet | ☐ |

### C6. Ketahanan

| No | Uji | Hasil yang diharapkan | ✓ |
|---|---|---|---|
| 27 | Cabut daya Orange Pi saat memantau pasien, lalu nyalakan | Pasien aktif muncul kembali dengan data sebelumnya; alasan "tidak dapat diukur" tetap ada | ☐ |
| 28 | `sudo systemctl stop siren` saat layar terbuka | Banner merah "Koneksi ke server SIREN terputus" | ☐ |
| 29 | `sudo systemctl start siren` | Banner hilang, muncul "Koneksi ke server SIREN pulih" | ☐ |
| 30 | Ambulans berjalan di jalan bergelombang | Nadi dan laju napas diberi tanda "getaran tinggi" bila guncangan kuat | ☐ |

### C7. Notifikasi pasien memburuk

| No | Uji | Hasil yang diharapkan | ✓ |
|---|---|---|---|
| 31 | Mode **Simulasi** skenario *Pasien memburuk*, mulai pasien baru, tunggu | Saat NEWS2 naik ≥3: banner **kuning** "NEWS2 naik … poin" dengan **bunyi sekali**; tetap tampil sampai **Saya mengerti** ditekan | ☐ |
| 32 | Lanjutkan sampai kategori menjadi **MERAH** | Banner **merah** "Pasien memburuk: kategori MERAH" dengan **alarm berulang**; berhenti setelah **Saya mengerti** atau tombol `K` | ☐ |
| 33 | Buka halaman lain (Perangkat, Riwayat) saat ada notifikasi | Banner tetap muncul di halaman mana pun | ☐ |
| 34 | Centang *oksigen tambahan* pada pasien stabil | Tidak ada notifikasi (memberi oksigen bukan perburukan) | ☐ |
| 35 | Bunyi di kabin ambulans dengan volume normal | Alarm terdengar jelas dari bangku petugas | ☐ |
| 36 | Buka serah terima → *Catatan & tindakan* | Setiap notifikasi tercatat beserta jam konfirmasinya; juga ada di PDF | ☐ |

**Hasil UAT:** ☐ Lulus semua  ☐ Lulus dengan catatan  ☐ Belum lulus

Catatan: _________________________________________________________________
