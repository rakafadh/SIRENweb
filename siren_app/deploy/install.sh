#!/usr/bin/env bash
# SIREN installer for the Orange Pi 4 Pro (Ubuntu image from Orange Pi).
#
# Run once, as the desktop user (not root), from the copied project folder:
#     bash siren_app/deploy/install.sh
# Safe to run again after updating the code.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"

if [ "$USER_NAME" = "root" ]; then
    echo "Jalankan sebagai user desktop (misalnya orangepi), bukan root." >&2
    exit 1
fi
case "$APP_DIR" in
    *" "*) echo "Path $APP_DIR mengandung spasi. Pindahkan proyek ke folder tanpa spasi." >&2; exit 1 ;;
esac
if [ ! -f "$APP_DIR/models/bp_model.joblib" ]; then
    echo "Peringatan: model BP tidak ditemukan di $APP_DIR/models/." >&2
    echo "Aplikasi tetap jalan, tetapi estimasi BP akan tampil 'model tidak ditemukan'." >&2
fi

echo "== Paket sistem"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip curl x11-xserver-utils
if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
    sudo apt-get install -y chromium-browser || sudo apt-get install -y chromium
fi
# brltty (braille display driver) claims CH340 USB-serial chips on Ubuntu and
# makes /dev/ttyUSB* vanish a second after plugging in.
if dpkg -s brltty >/dev/null 2>&1; then
    sudo apt-get remove -y brltty
fi

echo "== Lingkungan Python"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "== Akses port serial"
sudo usermod -aG dialout "$USER_NAME"
sudo install -m 644 "$APP_DIR/deploy/99-siren-serial.rules" /etc/udev/rules.d/99-siren-serial.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
# lp: USB printer-class label printers appear as /dev/usb/lp0, owned by group lp
sudo usermod -aG lp "$USER_NAME"

echo "== Zona waktu"
sudo timedatectl set-timezone Asia/Jakarta

echo "== Service server"
sed -e "s|__USER__|$USER_NAME|g" -e "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/siren.service" \
    | sudo tee /etc/systemd/system/siren.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable siren.service
sudo systemctl restart siren.service

echo "== Kiosk saat login"
chmod +x "$APP_DIR/deploy/kiosk.sh"
mkdir -p "$HOME/.config/autostart" "$HOME/Desktop"
sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/siren-kiosk.desktop" > "$HOME/.config/autostart/siren-kiosk.desktop"
# Same launcher on the desktop, to reopen SIREN after closing it with Alt+F4
sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/siren-kiosk.desktop" > "$HOME/Desktop/siren.desktop"
chmod +x "$HOME/Desktop/siren.desktop"

echo
echo "Selesai. Periksa:"
echo "  systemctl status siren        (harus 'active (running)')"
echo "  timedatectl                   (jam harus benar; lihat SIREN/README_DEPLOY.md bagian B1)"
echo "Reboot sekali agar grup dialout dan kiosk aktif:  sudo reboot"
