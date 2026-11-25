# bp_calibrated (copied)

`calibrated.py`, `features_v2.py`, `kalman.py` and `multipoint.py` are copied
unchanged from `siren-bp-model-test/bp_model/` (branch `bp-live-test`, commit
`5d4d5a9`, 25 Nov 2026). The model and its settings are in `siren_app/models/`:

- `bp_calibrated_vitaldb.joblib`  gradient boosting, trained on VitalDB, layout v2
- `alert_settings_vitaldb.json`   Kalman settings and alert thresholds

To update: retrain in `siren-bp-model-test` (scripts/vitaldb_train.py), copy the
two model files to `siren_app/models/` and these four modules here again.
`app/services/bp_service.py` wraps the model for the app.
