#!/usr/bin/env python3
"""
SIREN - triage algorithm

Two tracks run in parallel, exactly as the flowchart describes.

  START   One classification, made once when the patient is picked up.
          Mixes what the paramedic observes with what the sensors measure.

  NEWS2   Continuous re-scoring over all seven parameters, for the whole
          journey. Detects deterioration that START, being a single
          snapshot, cannot see.

The two can disagree, and when they do the WORSE of the pair is taken. A
patient who walked to the ambulance is START Kuning; if their NEWS2 reaches
9 twenty minutes later the displayed colour must be Merah. Taking the worse
is the only safe rule, and it is the case this system exists to catch.

Pain never produces Merah. It raises the floor to Kuning and nothing more.
Severe pain from a closed fracture is not a life threat, and START places
such a patient in the Delayed category.

Shock index sits outside the colour logic entirely. It raises an alert and
never changes the category, because it is unreliable in the elderly on beta
blockers, in children, and in head injury.

Deliberately kept on the host rather than the ESP32 so thresholds can be
changed without reflashing, and so the scoring is easy to unit test.

NOTE ON MEASUREMENT QUALITY. The score is computed over all seven
parameters, which makes it a complete NEWS2 in structure. It does not make
it an accurate one. SpO2 is uncalibrated, temperature is anchored at a
single point, and respiration is derived rather than measured. Report the
uncertainty of each parameter separately from the completeness of the score.
"""

from dataclasses import dataclass, field
from typing import Optional

# Colours, ordered by severity so that max() picks the worse one.
HIJAU, KUNING, MERAH = 0, 1, 2
COLOUR_NAME = {HIJAU: "Hijau", KUNING: "Kuning", MERAH: "Merah"}

# ACVPU. NEWS2 uses ACVPU, not AVPU: the C is new Confusion and it scores 3.
ACVPU = ["alert", "confusion", "voice", "pain", "unresponsive"]

START_RR_MAX = 30.0        # START: respiration above this is immediate
START_PI_MIN = 0.30        # perfusion index proxy for a palpable radial pulse
DELTA_WINDOW_S = 60.0      # how far back deterioration is measured
DELTA_UPGRADE = 2          # rise in NEWS2 that forces an upgrade
SHOCK_INDEX_ALERT = 0.9    # HR/SBP above this suggests significant haemorrhage
PAIN_KUNING = 6            # pain at or above this forces Kuning, never Merah


# ------------------------------------------------------- paramedic inputs ---
@dataclass
class Operator:
    """What the paramedic enters. None means not yet assessed."""
    acvpu: str = "alert"              # one of ACVPU
    on_oxygen: bool = False           # supplemental oxygen, scores 2 in NEWS2
    uncontrolled_bleeding: bool = False
    limb_loss_risk: bool = False
    ambulatory: bool = False          # walked to the ambulance
    pain: Optional[int] = None        # 0 to 10
    chest_pain: bool = False
    elderly: bool = False
    paediatric: bool = False
    comorbid: bool = False
    systolic: Optional[float] = None  # from the cuff, mmHg
    diastolic: Optional[float] = None


# --------------------------------------------------------- NEWS2 scoring ----
def score_resp(v):
    if v is None:
        return None
    if v <= 8:
        return 3
    if v <= 11:
        return 1
    if v <= 20:
        return 0
    if v <= 24:
        return 2
    return 3


def score_spo2_scale1(v):
    if v is None:
        return None
    if v <= 91:
        return 3
    if v <= 93:
        return 2
    if v <= 95:
        return 1
    return 0


def score_oxygen(on_oxygen):
    return 2 if on_oxygen else 0


def score_systolic(v):
    if v is None:
        return None
    if v <= 90:
        return 3
    if v <= 100:
        return 2
    if v <= 110:
        return 1
    if v <= 219:
        return 0
    return 3


def score_pulse(v):
    if v is None:
        return None
    if v <= 40:
        return 3
    if v <= 50:
        return 1
    if v <= 90:
        return 0
    if v <= 110:
        return 1
    if v <= 130:
        return 2
    return 3


def score_acvpu(level):
    # Alert scores 0. Everything else, including new confusion, scores 3.
    return 0 if level == "alert" else 3


def score_temp(v):
    if v is None:
        return None
    if v <= 35.0:
        return 3
    if v <= 36.0:
        return 1
    if v <= 38.0:
        return 0
    if v <= 39.0:
        return 1
    return 2


@dataclass
class News2:
    parts: dict = field(default_factory=dict)
    total: Optional[int] = None
    measured: int = 0
    any_three: bool = False          # a single parameter scoring 3
    colour: int = HIJAU
    note: str = ""


def news2(vitals, op: Operator) -> News2:
    """vitals is a dict with rr, spo2, hr, temp. Missing values are skipped."""
    parts = {
        "respirasi": score_resp(vitals.get("rr")),
        "spo2": score_spo2_scale1(vitals.get("spo2")),
        "oksigen": score_oxygen(op.on_oxygen),
        "sistolik": score_systolic(op.systolic),
        "nadi": score_pulse(vitals.get("hr")),
        "kesadaran": score_acvpu(op.acvpu),
        "suhu": score_temp(vitals.get("temp")),
    }
    known = [v for v in parts.values() if v is not None]
    out = News2(parts=parts, measured=len(known))

    if not known:
        out.note = "tidak ada data"
        return out

    out.total = sum(known)
    out.any_three = any(v == 3 for v in known)

    # Thresholds from the NEWS2 chart. Note the boundary: an aggregate of
    # exactly 7 is HIGH risk, so the test is >= 7 and not > 7. Getting this
    # wrong sends a high-risk patient to Kuning.
    if out.total >= 7:
        out.colour = MERAH
        out.note = "risiko tinggi"
    elif out.total >= 5 or out.any_three:
        out.colour = KUNING
        out.note = "risiko sedang" if out.total >= 5 else "red score satu parameter"
    else:
        out.colour = HIJAU
        out.note = "risiko rendah"

    if out.measured < 7:
        out.note += f" ({out.measured}/7 parameter)"
    return out


# ------------------------------------------------------ START (snapshot) ----
@dataclass
class Start:
    colour: Optional[int] = None
    reason: str = ""
    complete: bool = False


def start(vitals, op: Operator) -> Start:
    """START, as a single classification made at pickup.

    Order matters: the immediate criteria are checked first, so a patient
    who meets any of them is Merah regardless of everything after.
    """
    out = Start()

    if op.acvpu == "unresponsive":
        return Start(MERAH, "tidak responsif", True)
    if op.uncontrolled_bleeding:
        return Start(MERAH, "pendarahan tak terkendali", True)
    if op.limb_loss_risk:
        return Start(MERAH, "risiko kehilangan anggota gerak", True)

    rr = vitals.get("rr")
    if rr is not None and rr > START_RR_MAX:
        return Start(MERAH, f"laju napas {rr:.0f} > {START_RR_MAX:.0f}", True)

    # Perfusion index stands in for the radial pulse check. A palpable radial
    # pulse roughly implies a systolic above 80, and PI falls before pressure
    # does. This is a proxy, not an equivalent, and should be written up as one.
    pi = vitals.get("pi")
    if pi is not None and pi > 0 and pi < START_PI_MIN:
        return Start(MERAH, f"perfusi rendah (PI {pi:.2f})", True)

    # Pain caps at Kuning and can never produce Merah. Severe pain from a
    # closed fracture is not a life threat, and START classifies such a
    # patient Delayed. Checked before the ambulatory shortcut so a walking
    # patient in significant pain is not sent to Hijau.
    if op.chest_pain:
        return Start(KUNING, "nyeri dada", True)
    if op.pain is not None and op.pain >= PAIN_KUNING:
        return Start(KUNING, f"nyeri {op.pain}/10", True)

    if op.ambulatory:
        return Start(HIJAU, "dapat berjalan sendiri", True)

    # Everything measurable has been checked and nothing is immediate.
    if rr is None or pi is None:
        return Start(KUNING, "menunggu data sensor", False)
    return Start(KUNING, "tidak ada kriteria segera", True)


# ------------------------------------------------- deterioration and shock --
def delta_news2(history, window_s=DELTA_WINDOW_S):
    """Rise in NEWS2 across the window. Positive means getting worse.

    history is a list of (timestamp_seconds, total_score), oldest first.
    """
    pts = [(t, s) for t, s in history if s is not None]
    if len(pts) < 2:
        return None
    now = pts[-1][0]
    past = [(t, s) for t, s in pts if now - t >= window_s]
    if not past:
        return None
    return pts[-1][1] - past[-1][1]


def shock_index(hr, systolic):
    """HR divided by systolic. Above 0.9 predicts transfusion need in trauma.

    Updates whenever heart rate updates, so it keeps moving between cuff
    readings. Tachycardia is the earlier compensatory sign anyway.
    """
    if hr is None or systolic is None or systolic <= 0:
        return None
    return hr / systolic


# ---------------------------------------------------------------- combine ---
@dataclass
class Triage:
    colour: int = HIJAU
    source: str = ""
    news: News2 = field(default_factory=News2)
    start: Start = field(default_factory=Start)
    delta: Optional[int] = None
    shock: Optional[float] = None
    alerts: list = field(default_factory=list)

    @property
    def name(self):
        return COLOUR_NAME[self.colour]


def classify(vitals, op: Operator, history=None, workup=True) -> Triage:
    n = news2(vitals, op)
    s = start(vitals, op)

    candidates = [(n.colour, "NEWS2")]
    if s.colour is not None:
        candidates.append((s.colour, "START"))

    # The worse of the two wins. Equal severity credits NEWS2, since it is
    # the live measurement rather than a snapshot taken at pickup.
    colour, source = max(candidates, key=lambda c: c[0])

    t = Triage(colour=colour, source=source, news=n, start=s)
    t.delta = delta_news2(history or [])
    t.shock = shock_index(vitals.get("hr"), op.systolic)

    # Deterioration upgrades one level even if the absolute score is still
    # low. A patient climbing from 1 to 4 is not reassuring.
    if t.delta is not None and t.delta >= DELTA_UPGRADE and t.colour < MERAH:
        t.colour += 1
        t.source = f"ΔNEWS2 +{t.delta}"
        t.alerts.append(f"NEWS2 naik {t.delta} dalam {DELTA_WINDOW_S:.0f} detik")

    # Work-up modifier. This is the team's own addition, not part of START
    # or NEWS2, and should be labelled as such in the report.
    if workup and t.colour == HIJAU and (op.elderly or op.paediatric or op.comorbid):
        t.colour = KUNING
        t.source = "work-up"
        t.alerts.append("dinaikkan karena usia rentan atau komorbiditas")

    if t.shock is not None and t.shock >= SHOCK_INDEX_ALERT:
        t.alerts.append(f"shock index {t.shock:.2f}, curiga perdarahan")

    if op.chest_pain:
        t.alerts.append("nyeri dada dilaporkan")

    # Pain is recorded and flagged but does NOT set the colour on its own.
    # Severe pain from a closed fracture is not a life threat, and START
    # classifies such a patient Kuning.
    if op.pain is not None and op.pain >= 8:
        t.alerts.append(f"nyeri berat ({op.pain}/10)")

    if n.measured < 7:
        t.alerts.append(f"NEWS2 belum lengkap, {n.measured}/7 parameter")

    return t