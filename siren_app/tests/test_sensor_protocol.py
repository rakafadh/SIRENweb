from app.services.sensor_protocol import parse_line

# Real rows from siren-sensors/siren_log.csv (wall_time column removed): no finger yet
NO_FINGER = "13085,9.9,60,1722,0.007,0.00,-1.0,-,-1.0,0,-1,-1.0,nan,nan,nan,nan,0,28.70,30.88,29.90,0.0474,0.1421,3.82"
WITH_PATIENT = "52000,24.8,60,120000,0.010,1.20,76.4,b,30.0,12,97,15.5,15.0,16.0,15.5,0.4,40,35.40,29.10,36.60,0.0300,0.0900,2.10"


def test_ppg_row_drops_sentinels_and_flags_no_finger():
    parsed = parse_line(NO_FINGER)
    assert parsed.kind == "PPG"
    data = parsed.data
    assert data["hr"] is None and data["spo2"] is None and data["rr"] is None
    assert data["finger"] is False
    # 29.9 C is cabin air plus the calibration offset, not a patient
    assert data["tcore"] is None
    assert data["accel_rms"] == 0.0474


def test_ppg_row_with_patient():
    data = parse_line(WITH_PATIENT).data
    assert (data["hr"], data["spo2"], data["rr"], data["tcore"]) == (76.4, 97, 15.5, 36.6)
    assert data["finger"] is True
    assert data["pi"] == 1.2


def test_waveform_line():
    parsed = parse_line("W,52000,24.80,120100,119950,119800")
    assert parsed.kind == "WAVE"
    assert parsed.data == {"device_ms": 52000, "fs": 24.8, "samples": [120100.0, 119950.0, 119800.0]}


def test_temperature_csv_line():
    parsed = parse_line("T,8000,35.40,29.50,36.60,35.20,0.0210,0.0700,1.90,7")
    assert parsed.kind == "TEMP"
    assert parsed.data["tcore"] == 36.6
    assert parsed.data["accel_rms"] == 0.021
    assert parsed.data["settled"] is True


def test_temperature_without_sensor_flag_is_not_used():
    # flags = 4: IMU only, MLX did not answer at boot
    parsed = parse_line("T,8000,nan,nan,nan,nan,0.0210,0.0700,1.90,4")
    assert parsed.data["tcore"] is None
    assert parsed.data["accel_rms"] == 0.021


def test_legacy_temperature_console_line():
    line = "t=  12.4s  Tcore=36.82C(raw 35.10)  Tamb=29.40C  accel=0.012g peak=0.040g gyro=1.2dps  sent=12 fail=0"
    parsed = parse_line(line)
    assert parsed.kind == "TEMP"
    assert parsed.data["tcore"] == 36.82
    assert parsed.data["accel_rms"] == 0.012


def test_noise_and_headers_are_ignored():
    for line in [
        "SIREN PPG node",
        "millis,fs,led,ir_dc,drift,pi,hr,hr_src,sdnn,intervals,spo2,rr,riiv,riav,rifv,spread,beats,tobj,tamb,tcore,accel_rms,accel_peak,gyro_rms",
        "Filters redesigned for 10.0 Hz",
        "12,34",
        "",
        b"\xff\xfe garbage",
    ]:
        assert parse_line(line) is None
