# bp_calibrated (copied)

`calibrated.py`, `features_v2.py`, `kalman.py`, `multipoint.py` and `quality.py`
are copied unchanged from `siren-bp-model-test/bp_model/` (branch `bp-live-test`,
commit `ccb6807`, 28 Nov 2026). The rescue quality gate in `quality.py` stays off:
it is switched on only by a `"quality"` block in the settings file, and it did not
hold on the VitalDB test set.

`app/services/bp_service.py` picks, like `scripts/live_bp.py`, the cleanest 30 s
window rather than always the newest: for an estimate, of the windows ending in
the last 10 s; for a cuff, of the windows ending in the 30 s before "Mulai ukur"
(or 45 to 75 s before the reading when Ukur was not pressed), falling back to
later windows only when none of those passes the quality gate. The model and its settings are in `siren_app/models/`:

- `bp_calibrated_vitaldb.joblib`  gradient boosting, trained on VitalDB, layout v2
- `alert_settings_vitaldb.json`   Kalman settings and alert thresholds

To update: retrain in `siren-bp-model-test` (scripts/vitaldb_train.py), copy the
two model files to `siren_app/models/` and these five modules here again.
`app/services/bp_service.py` wraps the model for the app.
