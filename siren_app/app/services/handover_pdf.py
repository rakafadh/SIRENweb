"""Handover summary as a PDF, for the receiving ED team.

Uses fpdf2's built-in Helvetica so no font files need to ship with the app.
That font is Latin-1 only, hence the small transliteration table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fpdf import FPDF

from app.services.patients import ACVPU_LABEL

COLOUR_RGB = {"MERAH": (192, 57, 43), "KUNING": (224, 168, 0), "HIJAU": (46, 139, 87)}
NEWS2_PART_LABEL = {
    "respirasi": "Laju napas", "spo2": "SpO2", "oksigen": "Oksigen tambahan", "sistolik": "Sistolik (manset)",
    "nadi": "Nadi", "kesadaran": "Kesadaran", "suhu": "Suhu",
}
SOURCE_LABEL = {"SENSOR": "sensor", "MANUAL": "manual", "MOCK": "SIMULASI", None: "-"}
_TRANSLIT = str.maketrans({"₂": "2", "→": "->", "≥": ">=", "≤": "<=", "Δ": "delta ", "–": "-", "—": "-", "±": "+/-", "•": "-"})


def _t(value: Any) -> str:
    text = "-" if value is None or value == "" else str(value)
    return text.translate(_TRANSLIT).encode("latin-1", "replace").decode("latin-1")


def _when(iso: str | None, fmt: str = "%d-%m-%Y %H:%M:%S") -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone().strftime(fmt)


def _num(value: float | None, digits: int = 0) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


class _Doc(FPDF):
    def __init__(self, footer_text: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.footer_text = footer_text
        self.set_auto_page_break(auto=True, margin=16)
        self.set_margins(14, 14, 14)

    def footer(self):
        self.set_y(-11)
        self.set_font("Helvetica", size=7)
        self.set_text_color(110, 110, 110)
        self.cell(0, 4, _t(self.footer_text), align="L")
        self.cell(0, 4, f"Halaman {self.page_no()}/{{nb}}", align="R")

    def heading(self, text: str):
        self.ln(2)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(30, 30, 30)
        self.cell(0, 6, _t(text), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(200, 200, 200)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(1.5)

    def pairs(self, rows: list[tuple[str, Any]], label_w: float = 46):
        self.set_text_color(30, 30, 30)
        for label, value in rows:
            self.set_font("Helvetica", size=8.5)
            self.cell(label_w, 5, _t(label))
            self.set_font("Helvetica", "B", 8.5)
            self.multi_cell(0, 5, _t(value), new_x="LMARGIN", new_y="NEXT")

    def table(self, headers: list[str], rows: list[list[Any]], widths: list[float]):
        self.set_font("Helvetica", "B", 7.5)
        self.set_fill_color(238, 238, 238)
        for header, width in zip(headers, widths):
            self.cell(width, 5.5, _t(header), border=1, fill=True)
        self.ln()
        self.set_font("Helvetica", size=7.5)
        for row in rows:
            if self.get_y() > self.h - 22:
                self.add_page()
            for value, width in zip(row, widths):
                self.cell(width, 5, _t(value), border=1)
            self.ln()


def build_handover_pdf(detail: dict[str, Any], ambulance_id: str, bp_label: dict[str, Any], mode: str) -> bytes:
    patient = detail["patient"]
    latest = detail["latest_triage"] or {}
    first = detail["first_triage"] or {}
    generated = datetime.now().astimezone().strftime("%d-%m-%Y %H:%M:%S")

    footer = f"SIREN {ambulance_id} - {patient['code']} - dibuat {generated}. Alat bantu keputusan, bukan pengganti penilaian klinis."
    pdf = _Doc(footer)
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, "Ringkasan Serah Terima Pasien", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=8.5)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 5, _t(f"SIREN - unit ambulans {ambulance_id} - kode pasien {patient['code']}"), new_x="LMARGIN", new_y="NEXT")
    if mode == "MOCK":
        pdf.set_text_color(192, 57, 43)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "MODE SIMULASI - data sensor bukan pengukuran pasien nyata", new_x="LMARGIN", new_y="NEXT")

    # Current category, the first thing the receiving team needs
    colour = latest.get("colour")
    pdf.ln(2)
    rgb = COLOUR_RGB.get(colour, (120, 120, 120))
    pdf.set_fill_color(*rgb)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, _t(f"  Kategori terakhir: {colour or 'belum ada data'}"), fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(30, 30, 30)
    pdf.set_font("Helvetica", size=8.5)
    news = latest.get("news2_total")
    pdf.multi_cell(0, 5, _t(
        f"Sumber: {latest.get('source') or '-'}. NEWS2 {news if news is not None else '-'} "
        f"({latest.get('news2_measured', 0)}/7 parameter). START: {latest.get('start_colour') or '-'} "
        f"({latest.get('start_reason') or '-'}). Kategori terberat selama perjalanan: {detail['worst_colour'] or '-'}."
    ), new_x="LMARGIN", new_y="NEXT")

    pdf.heading("Identitas dan perjalanan")
    pdf.pairs([
        ("Nama", patient["display_name"]),
        ("Usia / jenis kelamin", f"{_num(patient['age'])} th / {patient['sex'] or '-'}"),
        ("Keluhan / mekanisme", patient["complaint"]),
        ("Komorbid", "ya" if patient["comorbid"] else "tidak"),
        ("RS tujuan", patient["destination"]),
        ("Petugas", patient["crew"]),
        ("Mulai pemantauan", _when(patient["created_at"])),
        ("Serah terima", _when(patient["handed_over_at"]) if patient["handed_over_at"] else "belum"),
    ])

    pdf.heading("Penilaian saat penjemputan (START)")
    pdf.pairs([
        ("Kategori START pertama", f"{first.get('start_colour') or '-'} ({first.get('start_reason') or '-'})"),
        ("Dapat berjalan sendiri", "ya" if patient["ambulatory"] else "tidak"),
        ("Perdarahan tak terkontrol", "ya" if patient["uncontrolled_bleeding"] else "tidak"),
        ("Risiko kehilangan anggota gerak", "ya" if patient["limb_loss_risk"] else "tidak"),
        ("Nyeri dada", "ya" if patient["chest_pain"] else "tidak"),
    ], label_w=58)

    pdf.heading("Kondisi terakhir")
    last = detail["timeline"][-1] if detail["timeline"] else {}
    sources = last.get("sources") or {}
    excused = detail.get("unmeasured") or {}

    def vital(key: str, unit: str, digits: int = 0) -> str:
        if last.get(key) is None:
            return f"tidak dapat diukur: {excused[key]}" if key in excused else "tidak ada data"
        return f"{_num(last.get(key), digits)} {unit} ({SOURCE_LABEL.get(sources.get(key))})"

    if patient["systolic"] is not None:
        cuff = f"{_num(patient['systolic'])}/{_num(patient['diastolic'])} mmHg, {_when(patient['bp_cuff_at'], '%H:%M')}"
    else:
        cuff = f"tidak dapat diukur: {excused['systolic']}" if "systolic" in excused else "belum diukur"

    pdf.pairs([
        ("Kesadaran (ACVPU)", ACVPU_LABEL.get(patient["acvpu"], patient["acvpu"])),
        ("Oksigen tambahan", "ya" if patient["on_oxygen"] else "tidak"),
        ("Skala nyeri", f"{_num(patient['pain'])}/10" if patient["pain"] is not None else "-"),
        ("Nadi", vital("hr", "/menit")),
        ("SpO2", vital("spo2", "%")),
        ("Laju napas", vital("rr", "/menit")),
        ("Suhu", vital("temp", "C", 1)),
        ("Tekanan darah manset", cuff),
        ("Shock index", _num(latest.get("shock_index"), 2)),
    ])

    parts = latest.get("news2_parts") or {}
    if parts:
        pdf.heading("Rincian skor NEWS2 terakhir")
        pdf.table(
            ["Parameter", "Skor"],
            [[NEWS2_PART_LABEL.get(k, k), "belum diukur" if v is None else v] for k, v in parts.items()],
            [70, 30],
        )

    if detail["alerts"]:
        pdf.heading("Peringatan selama perjalanan")
        pdf.table(["Pertama muncul", "Peringatan"],
                  [[_when(a["first_seen"], "%H:%M:%S"), a["alert"]] for a in detail["alerts"]], [28, 154])

    est = [r for r in detail["timeline"] if r.get("bp_est_status") == "OK"]
    pdf.heading("Estimasi tekanan darah dari PPG (eksperimental)")
    if est:
        r = est[-1]
        pdf.pairs([("Estimasi terakhir", f"{_num(r['bp_est_sbp'])}/{_num(r['bp_est_dbp'])} mmHg pada {_when(r['captured_at'], '%H:%M:%S')}")])
    else:
        pdf.pairs([("Estimasi", "tidak tersedia selama perjalanan")])
    pdf.set_font("Helvetica", "I", 7.5)
    pdf.set_text_color(90, 90, 90)
    accuracy = ""
    if "sbp_mae" in bp_label:
        accuracy = (f" Pada data uji {bp_label.get('dataset')} ({bp_label.get('test_subjects')} subjek): rata-rata meleset "
                    f"+/-{bp_label['sbp_mae']} mmHg (sistolik) dan +/-{bp_label['dbp_mae']} mmHg (diastolik); "
                    f"{bp_label['sbp_within_10_pct']}% estimasi sistolik dalam +/-10 mmHg.")
    pdf.multi_cell(0, 4, _t("Tidak dipakai untuk triase. Belum divalidasi pada sensor ini; bukan pengganti manset." + accuracy),
                   new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(30, 30, 30)

    if detail["events"]:
        pdf.heading("Catatan, input, dan tindakan")
        pdf.table(["Waktu", "Jenis", "Keterangan"],
                  [[_when(e["created_at"], "%H:%M:%S"), e["kind"], e["detail"][:110]] for e in detail["events"]],
                  [20, 22, 140])

    if detail["timeline"]:
        pdf.heading("Linimasa tanda vital")
        rows = []
        for r in _thin(detail["timeline"], 60):
            tri = r.get("triage") or {}
            rows.append([
                _when(r["captured_at"], "%H:%M:%S"), tri.get("colour"),
                "-" if tri.get("news2_total") is None else tri.get("news2_total"),
                _num(r["hr"]), _num(r["spo2"]), _num(r["rr"]), _num(r["temp"], 1),
                _num(r["systolic"]), tri.get("source"),
            ])
        pdf.table(["Waktu", "Kategori", "NEWS2", "Nadi", "SpO2", "Napas", "Suhu", "Sistolik", "Sumber kategori"],
                  rows, [18, 18, 14, 14, 14, 14, 14, 16, 60])

    return bytes(pdf.output())


def _thin(rows: list[dict], limit: int) -> list[dict]:
    """Keep every category change and enough evenly spaced rows to stay readable."""
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    keep = {int(i * step) for i in range(limit)} | {len(rows) - 1}
    prev = None
    for i, row in enumerate(rows):
        colour = (row.get("triage") or {}).get("colour")
        if colour != prev:
            keep.add(i)
        prev = colour
    return [rows[i] for i in sorted(keep)]
