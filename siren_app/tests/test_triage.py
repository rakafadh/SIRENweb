"""The rules from the final report, section 'Logika Triase SIREN dengan Dua Jalur Paralel'."""

from app.services import triage as TR

NORMAL = {"hr": 75, "spo2": 98, "rr": 16, "temp": 36.8, "pi": 1.5}


def op(**kwargs):
    base = {"systolic": 125}
    base.update(kwargs)
    return TR.Operator(**base)


def colour(vitals, operator, history=None):
    return TR.classify(vitals, operator, history).name


# --- START ---------------------------------------------------------------
def test_start_red_criteria():
    assert TR.start(NORMAL, op(acvpu="unresponsive")).colour == TR.MERAH
    assert TR.start(NORMAL, op(uncontrolled_bleeding=True)).colour == TR.MERAH
    assert TR.start(NORMAL, op(limb_loss_risk=True)).colour == TR.MERAH
    assert TR.start({**NORMAL, "rr": 31}, op()).colour == TR.MERAH
    assert TR.start({**NORMAL, "pi": 0.2}, op()).colour == TR.MERAH


def test_start_rr_exactly_30_is_not_red():
    assert TR.start({**NORMAL, "rr": 30}, op()).colour != TR.MERAH


def test_chest_pain_and_pain_six_are_yellow():
    assert TR.start(NORMAL, op(chest_pain=True)).colour == TR.KUNING
    assert TR.start(NORMAL, op(pain=6)).colour == TR.KUNING
    assert TR.start(NORMAL, op(pain=5, ambulatory=True)).colour == TR.HIJAU


def test_pain_never_produces_red():
    assert colour(NORMAL, op(pain=10)) == "Kuning"


def test_walking_patient_is_green():
    assert TR.start(NORMAL, op(ambulatory=True)).colour == TR.HIJAU
    assert colour(NORMAL, op(ambulatory=True)) == "Hijau"


# --- NEWS2 ---------------------------------------------------------------
def test_news2_covers_seven_parameters():
    result = TR.news2(NORMAL, op())
    assert result.measured == 7
    assert result.total == 0


def test_news2_aggregate_seven_is_red_not_yellow():
    # RR 22 (2) + SpO2 93 (2) + HR 115 (2) + temp 38.5 (1) = 7
    vitals = {"hr": 115, "spo2": 93, "rr": 22, "temp": 38.5}
    result = TR.news2(vitals, op())
    assert result.total == 7
    assert result.colour == TR.MERAH


def test_news2_five_or_single_three_is_yellow():
    assert TR.news2({"hr": 115, "spo2": 93, "rr": 21, "temp": 36.8}, op()).colour == TR.KUNING  # 2+2+2 = 6
    single = TR.news2({**NORMAL, "spo2": 90}, op())
    assert single.total == 3 and single.colour == TR.KUNING


def test_consciousness_and_oxygen_are_scored():
    assert TR.news2(NORMAL, op(acvpu="confusion")).parts["kesadaran"] == 3
    assert TR.news2(NORMAL, op(on_oxygen=True)).parts["oksigen"] == 2


def test_missing_parameters_are_skipped_and_flagged():
    result = TR.classify({"hr": 80}, TR.Operator())
    assert result.news.measured == 3   # HR, oxygen, consciousness
    assert any("belum lengkap" in alert for alert in result.alerts)


# --- combination ---------------------------------------------------------
def test_worse_of_two_wins():
    sick = {"hr": 135, "spo2": 90, "rr": 26, "temp": 39.5, "pi": 1.0}
    result = TR.classify(sick, op(ambulatory=True))
    assert result.name == "Merah"
    assert result.source == "NEWS2"


def test_delta_news2_upgrades_one_level():
    vitals = {"hr": 105, "spo2": 95, "rr": 16, "temp": 36.8, "pi": 1.0}  # total 2, still green on its own
    history = [(0.0, 0), (65.0, 2)]   # newest entry is the current score
    result = TR.classify(vitals, op(ambulatory=True), history)
    assert result.delta == 2
    assert result.name == "Kuning"
    assert result.source.startswith("ΔNEWS2")


def test_work_up_raises_vulnerable_green_to_yellow():
    assert colour(NORMAL, op(ambulatory=True, elderly=True)) == "Kuning"
    assert colour(NORMAL, op(ambulatory=True, comorbid=True)) == "Kuning"


def test_shock_index_alerts_without_changing_colour():
    vitals = {**NORMAL, "hr": 88}
    result = TR.classify(vitals, op(ambulatory=True, systolic=95))  # 88/95 = 0.93
    assert result.shock >= 0.9
    assert any("shock index" in alert for alert in result.alerts)
    assert result.name == "Hijau"
