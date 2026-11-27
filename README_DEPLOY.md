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
| `siren-bp-model-test/` | Sumber model tekanan darah: pelatihan, evaluasi, dan uji live | Laptop saja, bila melatih ulang |
| `BloodPressure-Model/` | Model tekanan darah versi lama, tidak dipakai aplikasi lagi | Tidak dipakai |

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
| ESP32 DevKit (klasik) | MAX30102 + MPU6050 | `ppg` | SDA 26 · SCL 14 (dipakai bersama) |
| ESP32-S3 | MLX90614 | `temp` | SDA 4 · SCL 5 |

MPU6050 (sensor getaran) ada di board PPG, satu jalur I²C dengan MAX30102. Bila suatu saat disambung ke pin lain, tambahkan `-D SIREN_MPU_SDA=<pin>` dan `-D SIREN_MPU_SCL=<pin>` di bagian `[env:ppg]` pada `platformio.ini`.

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
| Laju napas | ±20 detik (perkiraan awal setelah 16 detik, penuh setelah 32 detik) |
| Estimasi TD (PPG) | Setelah ±30 detik gelombang **dan** satu kali TD manset diisi (kalibrasi). Pasien dan sensor sebaiknya diam saat manset diisi. |

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
| Estimasi TD: "Menunggu gelombang dari sensor jari" | Firmware PPG belum mengirim baris `W,` | Flash ulang board PPG (A3) |
| Estimasi TD tidak muncul setelah manset diisi | Kalibrasi gagal; alasannya tampil di layar (misalnya kendaraan bergerak) | Ukur manset lagi saat lebih tenang. Sementara itu triase memakai angka manset |
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

**Jangan ikut disalin:** `siren.db*` (data pasien laptop), `runtime.json` (bisa membuat Orange Pi start dalam mode simulasi), `prints/`, `siren_legacy_backup_*.db`, `__pycache__/`, `.pytest_cache/`. Folder `siren-sensors/`, `siren-bp-model-test/`, `BloodPressure-Model/`, dan `dataset/` juga tidak perlu.

Cara cepat menyalin ke flashdisk (misalnya drive `F:`) dari folder `Despro` di laptop:

```powershell
robocopy "SIREN\siren_app" "F:\SIREN\siren_app" /E /XD __pycache__ .pytest_cache prints .venv /XF siren.db siren.db-shm siren.db-wal runtime.json siren_legacy_backup_*.db
```

Pastikan `F:\SIREN\siren_app\models\` berisi `bp_calibrated_vitaldb.joblib` dan `alert_settings_vitaldb.json`. Tanpa keduanya SIREN tetap jalan, tetapi kotak *TD estimasi PPG* akan tertulis "Model TD tidak ditemukan di perangkat" dan triase hanya memakai TD manset.

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

**QR di label.** QR berisi tautan ke penampil SIREN QR (`https://thaniyuck.github.io/siren/`) dengan seluruh data perjalanan setelah tanda `#`. HP mengurai data itu sendiri, jadi data pasien tidak pernah terkirim ke server. Orange Pi tidak butuh internet sama sekali. HP pemindai butuh internet sekali untuk membuka penampilnya; setelah itu label berikutnya bisa dibuka tanpa sinyal. Angka penting (kategori, NEWS2, vital, TD manset, TD estimasi PPG, RS tujuan) tetap tercetak di kertas. Pengaturan di `.env`:

```env
SIREN_QR_BASE_URL=https://thaniyuck.github.io/siren/
SIREN_QR_MAX_VERSION=15      # 12 = modul QR lebih besar bila label sulit dipindai
SIREN_QR_INCLUDE_NAME=true   # false = nama hanya di kertas
```

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
| Notifikasi pasien memburuk | Kartu kecil di kiri atas: kuning (NEWS2 naik ≥3, bunyi sekali, tampil 5 detik) atau merah (menjadi MERAH, alarm 3 kali, tampil 7 detik). Tanpa tombol; hilang sendiri, atau sentuh kartunya / tekan `K` untuk menutup lebih cepat. Notifikasi baru muncul paling atas dan mendorong yang lama ke bawah. Bila tertulis "Bunyi mati", sentuh layar sekali. |
| Pengingat ukur TD manset | Kartu biru di kiri atas saat waktunya kalibrasi (tiap 10 menit, tiap 5 menit bila NEWS2 ≥ 5, atau lebih cepat bila TD berubah tajam). Kartu tidak hilang sendiri; tekan **Ukur**, isi TD, lalu **Simpan** (mengisi TD manset langsung dari panel kanan juga menutupnya). Bila belum juga diukur, pengingat berganti menjadi "Ukur TD manset sekarang". |
| Halaman Serah terima | Begitu dibuka, pemantauan berhenti: ringkasan memakai data saat tiba, dan tidak ada notifikasi atau pengingat. Kembali ke **Pemantauan** bila pasien belum jadi diserahkan; pemantauan berjalan lagi. |
| PDF serah terima | Tersimpan di `~/Downloads`; salin ke flashdisk bila perlu |
| Data mentah | Menu **Riwayat** → ekspor CSV |
| Cadangan data | Saat server mati, salin `siren.db` dan `siren.db-wal` |
| Memperbarui SIREN | Lihat B7 |

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
| QR label sulit dipindai HP | Modul QR terlalu kecil untuk kertas atau printer ini | `SIREN_QR_MAX_VERSION=12` lalu restart; titik tren jadi lebih jarang, modul lebih besar |
| HP menampilkan "Data di QR tidak lengkap atau rusak" | Label kusut atau terpindai sebagian | Pindai ulang lebih dekat dan tegak lurus; angka penting tetap ada di kertas |

### B7. Memperbarui SIREN

Lakukan saat ada versi baru di branch `main`, dan **tidak ada pasien aktif**.

1. **Ambil versi baru di laptop.** Di folder `SIREN`: `git pull`, atau unduh ulang dari GitHub.
2. **Flash ulang ESP32 bila firmware berubah.** Cek di riwayat GitHub apakah ada perubahan di `siren-sensors/src/ppg/` atau `siren-sensors/src/temp/`. Flash hanya board yang berubah (A3), satu board tercolok setiap kali upload.
3. **Uji sebentar di laptop** (A2 dan A4): `py -3.11 -m pip install -r requirements.txt`, lalu jalankan SIREN dan pastikan semua vital muncul.
4. **Cadangkan data Orange Pi.** Matikan server dengan `sudo systemctl stop siren`, lalu salin `siren.db` dan `siren.db-wal` ke flashdisk.
5. **Salin `siren_app/` baru ke Orange Pi** dengan aturan yang sama seperti B2. Jangan menimpa `siren.db*`, `.env`, dan `runtime.json` yang sudah ada di Orange Pi.
6. **Instal ulang paket dan reboot:**
   ```bash
   cd /home/orangepi
   bash SIREN/siren_app/deploy/install.sh
   sudo reboot
   ```
   Skrip aman dijalankan ulang dan memasang paket Python baru bila ada.
7. **Cek setelah reboot:** `systemctl status siren` harus *active (running)*, menu **Perangkat** menunjukkan kedua node *Terhubung*, lalu coba **Cetak uji**.

**Pembaruan model TD terkalibrasi (November 2026):** board PPG perlu di-flash ulang supaya mengirim penanda perubahan intensitas LED, dan paket `scipy` baru terpasang lewat langkah 6. Board suhu tidak perlu di-flash ulang.

**Pemindahan MPU6050 ke board PPG:** **kedua** board perlu di-flash ulang. Board PPG sekarang membaca MPU6050 sendiri, dan board suhu tidak lagi mencarinya. Setelah flash, monitor serial board PPG harus menampilkan `MPU6050 ready.`

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
| 7 | Tahan jari 30 detik | Laju napas muncul (±20 detik) dan Estimasi TD (PPG) muncul | ☐ |
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
| 25 | **Cetak label** | Tercetak dalam < 3 detik; kategori, nama, vital, TD manset, TD estimasi PPG, dan RS tujuan terbaca | ☐ |
| 26 | Pindai QR label dengan **3 HP berbeda** (Android lama, iPhone), jarak 10–30 cm, HP terhubung internet | Penampil SIREN QR terbuka; kategori, NEWS2, vital terakhir, dan TD manset sama dengan kertas; grafik tren tampil | ☐ |
| 27 | Pindai sekali dengan internet, nyalakan mode pesawat, pindai label lain | Penampil tetap terbuka tanpa sinyal | ☐ |

### C6. Ketahanan

| No | Uji | Hasil yang diharapkan | ✓ |
|---|---|---|---|
| 28 | Cabut daya Orange Pi saat memantau pasien, lalu nyalakan | Pasien aktif muncul kembali dengan data sebelumnya; alasan "tidak dapat diukur" tetap ada | ☐ |
| 29 | `sudo systemctl stop siren` saat layar terbuka | Banner merah "Koneksi ke server SIREN terputus" | ☐ |
| 30 | `sudo systemctl start siren` | Banner hilang, muncul "Koneksi ke server SIREN pulih" | ☐ |
| 31 | Ambulans berjalan di jalan bergelombang | Nadi dan laju napas diberi tanda "getaran tinggi" bila guncangan kuat | ☐ |

### C7. Notifikasi pasien memburuk

| No | Uji | Hasil yang diharapkan | ✓ |
|---|---|---|---|
| 32 | Mode **Simulasi** skenario *Pasien memburuk*, mulai pasien baru, tunggu | Saat NEWS2 naik ≥3: kartu **kuning** kecil di kiri atas "NEWS2 naik … poin" dengan **bunyi sekali**, tanpa tombol; hilang sendiri setelah 5 detik | ☐ |
| 33 | Lanjutkan sampai kategori menjadi **MERAH** | Kartu **merah** "Pasien memburuk: kategori MERAH" dengan **alarm 3 kali**; kartu yang lebih lama terdorong ke bawahnya; hilang sendiri setelah 7 detik atau saat disentuh / tombol `K` | ☐ |
| 34 | Buka halaman lain (Perangkat, Riwayat) saat ada notifikasi; lalu buka **Serah terima** | Kartu tetap muncul di Perangkat dan Riwayat; di Serah terima tidak ada notifikasi sama sekali | ☐ |
| 35 | Centang *oksigen tambahan* pada pasien stabil | Tidak ada notifikasi (memberi oksigen bukan perburukan) | ☐ |
| 36 | Bunyi di kabin ambulans dengan volume normal | Alarm terdengar jelas dari bangku petugas | ☐ |
| 37 | Buka serah terima → *Catatan & tindakan* | Setiap notifikasi tercatat (beserta jam bila ditutup petugas); juga ada di PDF | ☐ |

**Hasil UAT:** ☐ Lulus semua  ☐ Lulus dengan catatan  ☐ Belum lulus

Catatan: _________________________________________________________________
