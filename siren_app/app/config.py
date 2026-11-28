import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{(BASE_DIR / 'siren.db').as_posix()}"
    # A relative SQLite path would depend on the working directory of whoever
    # started the server. Anchor it to the app folder instead.
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith("sqlite:////"):
        path = Path(url[len(prefix):])
        if not path.is_absolute():
            return f"{prefix}{(BASE_DIR / path).resolve().as_posix()}"
    return url


# Cuff-calibrated BP model, trained in siren-bp-model-test (scripts/vitaldb_train.py)
# and copied here, so one folder (siren_app) is all a device needs.
_BP_DIR = BASE_DIR / "models"


class Settings:
    DATABASE_URL: str = _database_url()
    AMBULANCE_ID: str = os.getenv("AMBULANCE_ID") or os.getenv("KIOSK_CODE") or "SIREN-01"

    # Serial. Empty SERIAL_PORTS means every USB serial port is opened and
    # identified by what it sends, so the two boards may enumerate in any order.
    SERIAL_BAUD: int = int(os.getenv("SERIAL_BAUD", "115200"))
    SERIAL_PORTS: list[str] = [p.strip() for p in os.getenv("SERIAL_PORTS", "").split(",") if p.strip()]
    # Starting mode only. Once the mode is switched on the Perangkat page, the
    # choice is saved to STATE_PATH and used from then on.
    MOCK_SENSORS: bool = _bool("MOCK_SENSORS", "false")
    # stable | deteriorating
    MOCK_SCENARIO: str = os.getenv("MOCK_SCENARIO", "stable").strip().lower()
    STATE_PATH: Path = Path(os.getenv("SIREN_STATE_PATH") or BASE_DIR / "runtime.json")

    # A reading older than this is treated as absent, never held on screen.
    SENSOR_STALE_S: float = float(os.getenv("SENSOR_STALE_S", "5"))
    # A manually entered vital is used as a fallback for this long.
    MANUAL_TTL_S: float = float(os.getenv("MANUAL_TTL_S", "900"))
    # A manual respiratory rate has no time limit: it stays until the sensor has
    # given a valid RR for this long without a gap, then the sensor takes over.
    RR_MANUAL_RELEASE_S: float = float(os.getenv("RR_MANUAL_RELEASE_S", "10"))
    # How often the monitor writes a snapshot to the database.
    SNAPSHOT_INTERVAL_S: float = float(os.getenv("SNAPSHOT_INTERVAL_S", "10"))
    # Provisional: above this RMS vehicle vibration, HR and RR are flagged.
    MOTION_FLAG_G: float = float(os.getenv("MOTION_FLAG_G", "0.15"))

    # Work-up modifier ages (laporan: lansia, anak). Indonesian definitions:
    # lansia from 60 (UU 13/1998), anak below 18 (UU 35/2014).
    ELDERLY_AGE: int = int(os.getenv("ELDERLY_AGE", "60"))
    PAEDIATRIC_AGE: int = int(os.getenv("PAEDIATRIC_AGE", "18"))

    BP_MODEL_PATH: Path = Path(os.getenv("BP_MODEL_PATH") or _BP_DIR / "bp_calibrated_vitaldb.joblib")
    BP_SETTINGS_PATH: Path = Path(os.getenv("BP_SETTINGS_PATH") or _BP_DIR / "alert_settings_vitaldb.json")
    # A cuff reading typed in without "Mulai ukur" calibrates with the PPG
    # window that ended this long before it: inflating, reading and typing
    # take about this long, and the window must come from before the cuff
    # squeezed the arm (siren-bp-model-test, live_bp.py --entry-lag).
    BP_CUFF_ENTRY_LAG_S: float = float(os.getenv("BP_CUFF_ENTRY_LAG_S", "45"))

    # Handover label printer, see app/services/label_printer.py.
    # none | file | escpos
    PRINTER_BACKEND: str = os.getenv("PRINTER_BACKEND", "none").strip().lower()
    # escpos: /dev/usb/lp0, /dev/rfcomm0, /dev/ttyUSB2, COM5 or socket://ip:9100.
    # file: folder for PNG labels (relative to the app folder).
    PRINTER_DEVICE: str = os.getenv("PRINTER_DEVICE", "").strip()
    PRINTER_BAUD: int = int(os.getenv("PRINTER_BAUD", "9600"))
    # 384 dots = 48 mm print width of a 58 mm printer at 203 dpi
    PRINTER_WIDTH_PX: int = int(os.getenv("PRINTER_WIDTH_PX", "384"))

    # Label QR: a link to the SIREN QR viewer with the whole trip packed after
    # the '#' (app/siren_qr). The phone decodes it; the data never reaches a server.
    QR_BASE_URL: str = os.getenv("SIREN_QR_BASE_URL", "https://thaniyuck.github.io/siren/").strip()
    # 15 prints 0.50 mm modules and keeps one trend point per minute. If paper
    # labels scan poorly, 12 gives 0.63 mm modules and a coarser trend.
    QR_MAX_VERSION: int = int(os.getenv("SIREN_QR_MAX_VERSION", "15"))
    # Team decision 22 Nov: the name goes in the QR to help the hospital match
    # the patient. false keeps it on paper only.
    QR_INCLUDE_NAME: bool = _bool("SIREN_QR_INCLUDE_NAME", "true")

    # Tests switch this off and drive the monitor and sensors by hand.
    RUN_BACKGROUND: bool = _bool("SIREN_BACKGROUND", "true")


if Settings.PRINTER_BACKEND == "file" and not Path(Settings.PRINTER_DEVICE or "prints").is_absolute():
    Settings.PRINTER_DEVICE = str(BASE_DIR / (Settings.PRINTER_DEVICE or "prints"))

settings = Settings()
