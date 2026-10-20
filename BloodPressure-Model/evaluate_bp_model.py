"""Evaluate the saved BP artifact on the test split and write the numbers siren_app shows.

Run from this folder after training:

    python evaluate_bp_model.py

Writes siren_app/models/bp_model_metrics.json. siren_app reads that file to label every
estimate with how far the model is typically off, and to scale sensor PPG to the
amplitude range the model was trained on.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import joblib
import numpy as np

from train_bp_model import ARTIFACTS_DIR, DATASET_DIR, load_subject_data, metrics_for_targets, parse_subjects

TOLERANCES_MMHG = (5, 10, 15)


def ppg_reference_scale(subjects: list[str]) -> dict[str, float]:
    """Median per-window mean and standard deviation of the training PPG.

    MIMIC-BP stores the bedside monitor's pleth in arbitrary units, not
    normalised per window, and several features (mean, energy, sum) depend on
    absolute level. Sensor PPG is rescaled to these values before inference.
    """
    means, stds = [], []
    for subject in subjects:
        path = DATASET_DIR / 'ppg' / f'{subject}_ppg.npy'
        if not path.exists():
            continue
        windows = np.load(path)
        means.extend(windows.mean(axis=1).tolist())
        stds.extend(windows.std(axis=1).tolist())
    return {'mean': float(np.median(means)), 'std': float(np.median(stds)), 'windows': len(means)}


def main() -> None:
    bundle = joblib.load(ARTIFACTS_DIR / 'bp_model.joblib')
    model = bundle['model']

    train_ids = parse_subjects(DATASET_DIR / 'train_subjects.txt')
    test_ids = parse_subjects(DATASET_DIR / 'test_subjects.txt')
    test_X, test_y, test_used = load_subject_data(test_ids, DATASET_DIR)
    _, train_y, _ = load_subject_data(train_ids, DATASET_DIR)

    pred = model.predict(test_X)
    metrics = metrics_for_targets(test_y, pred)
    abs_err = np.abs(pred - test_y)

    # A model that always answers the training mean. Anything worth showing
    # has to beat this.
    baseline_mae = np.mean(np.abs(test_y - train_y.mean(axis=0)), axis=0)

    targets = {}
    for i, name in enumerate(['sbp', 'dbp']):
        targets[name] = {
            'MAE': round(float(metrics['MAE'][i]), 2),
            'RMSE': round(float(metrics['RMSE'][i]), 2),
            'SD_of_error': round(float(metrics['SD_of_error'][i]), 2),
            'ME': round(float(metrics['ME'][i]), 2),
            'Pearson_r': round(float(metrics['Pearson_r'][i]), 3),
            'R2': round(float(metrics['R2'][i]), 3),
            'MAPE_pct': round(float(np.mean(abs_err[:, i] / test_y[:, i]) * 100), 1),
            'within_mmHg_pct': {
                str(tol): round(float(np.mean(abs_err[:, i] <= tol) * 100), 1) for tol in TOLERANCES_MMHG
            },
            'baseline_mean_MAE': round(float(baseline_mae[i]), 2),
        }

    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'feature_version': bundle.get('feature_version'),
        'dataset': 'MIMIC-BP',
        'test_subjects': len(test_used),
        'targets': targets,
        'ppg_reference': ppg_reference_scale(train_ids),
        'input': {'fs_hz': 125, 'window_samples': 3750},
    }

    out_path = ARTIFACTS_DIR / 'bp_model_metrics.json'
    out_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    print(f'Written to {out_path}')


if __name__ == '__main__':
    main()
