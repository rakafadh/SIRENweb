from __future__ import annotations

import ast
import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import RandomizedSearchCV

DATASET_DIR = Path(__file__).resolve().parent.parent / 'dataset'
# The app loads the model from here, so retraining updates SIREN directly.
ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / 'siren_app' / 'models'
ARTIFACTS_DIR.mkdir(exist_ok=True, parents=True)


def parse_subjects(filepath: Path) -> list[str]:
    raw = filepath.read_text(encoding='utf-8').strip()
    if not raw:
        return []
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except (ValueError, SyntaxError):
        pass
    return [line.strip() for line in raw.splitlines() if line.strip()]


def load_subject_data(subjects: list[str], dataset_dir: Path):
    X_list = []
    y_list = []
    used_subjects = []

    for subject in subjects:
        ppg_path = dataset_dir / 'ppg' / f'{subject}_ppg.npy'
        label_path = dataset_dir / 'labels' / f'{subject}_labels.npy'
        if not ppg_path.exists() or not label_path.exists():
            continue

        ppg = np.load(ppg_path)
        labels = np.load(label_path)
        if ppg.shape[0] != labels.shape[0]:
            raise ValueError(f'Shape mismatch for {subject}: ppg {ppg.shape}, labels {labels.shape}')

        patient_features = np.mean([extract_features(ppg[idx]) for idx in range(ppg.shape[0])], axis=0)
        patient_labels = np.mean(labels, axis=0)

        X_list.append(patient_features)
        y_list.append(patient_labels)
        used_subjects.append(subject)

    if not X_list:
        raise ValueError('No valid subject data was loaded from the dataset.')

    X = np.asarray(X_list, dtype=np.float64)
    y = np.asarray(y_list, dtype=np.float64)
    return X, y, used_subjects


def extract_features(signal: np.ndarray) -> np.ndarray:
    sig = np.asarray(signal, dtype=np.float64)
    if sig.size == 0:
        return np.zeros(26, dtype=np.float64)

    centered = sig - np.mean(sig)
    diff = np.diff(sig)
    abs_diff = np.abs(diff)
    fft_vals = np.abs(np.fft.rfft(centered))
    freqs = np.fft.rfftfreq(sig.size, d=1 / 125.0)

    feature_list = [
        np.mean(sig),
        np.std(sig),
        np.min(sig),
        np.max(sig),
        np.median(sig),
        np.ptp(sig),
        np.sqrt(np.mean(sig ** 2)),
        np.mean(np.abs(sig)),
        np.mean(abs_diff),
        np.max(abs_diff),
        np.std(diff),
        np.mean(np.abs(centered)),
        np.mean(np.abs(np.diff(sig, n=2))),
        np.sum(np.abs(sig)),
        np.sum(np.square(sig)),
        np.sum(sig > np.median(sig)),
        np.sum(sig < np.median(sig)),
        float(np.sum(centered > 0) / max(len(centered), 1)),
        float(np.sum(centered < 0) / max(len(centered), 1)),
        float(np.sum(np.diff(np.sign(centered)) != 0) / max(len(centered), 1)),
    ]

    spectral = []
    if fft_vals.size > 0:
        top_idx = np.argsort(fft_vals)[-6:]
        spectral.extend(fft_vals[top_idx].tolist())
        spectral.append(float(freqs[np.argmax(fft_vals)]))
        spectral.append(float(np.sum(fft_vals[1:10]) / max(np.sum(fft_vals), 1e-9)))
    else:
        spectral.extend([0.0, 0.0, 0.0])

    downsampled = np.asarray(np.linspace(sig.min(), sig.max(), num=8))
    feature_list.extend(downsampled.tolist())
    feature_list.extend(spectral)
    return np.asarray(feature_list, dtype=np.float64)


def metrics_for_targets(y_true, y_pred):
    error = y_pred - y_true
    mae = mean_absolute_error(y_true, y_pred, multioutput='raw_values')
    rmse = np.sqrt(mean_squared_error(y_true, y_pred, multioutput='raw_values'))
    sd_error = np.std(error, axis=0, ddof=1) if len(error) > 1 else np.zeros(error.shape[1], dtype=np.float64)
    me = np.mean(error, axis=0)
    pearson = np.array([
        np.corrcoef(y_true[:, i], y_pred[:, i])[0, 1]
        for i in range(y_true.shape[1])
    ], dtype=np.float64)
    r2 = r2_score(y_true, y_pred, multioutput='raw_values')
    return {
        'MAE': mae,
        'RMSE': rmse,
        'SD_of_error': sd_error,
        'ME': me,
        'Pearson_r': pearson,
        'R2': r2,
    }


def train_model():
    train_ids = parse_subjects(DATASET_DIR / 'train_subjects.txt')
    val_ids = parse_subjects(DATASET_DIR / 'val_subjects.txt')
    test_ids = parse_subjects(DATASET_DIR / 'test_subjects.txt')

    train_X, train_y, train_used = load_subject_data(train_ids, DATASET_DIR)
    val_X, val_y, val_used = load_subject_data(val_ids, DATASET_DIR)
    test_X, test_y, test_used = load_subject_data(test_ids, DATASET_DIR)

    print(f'Train samples: {train_X.shape[0]} | Validation samples: {val_X.shape[0]} | Test samples: {test_X.shape[0]}')
    print(f'Train subjects: {len(train_used)} | Validation subjects: {len(val_used)} | Test subjects: {len(test_used)}')

    candidate_params = [
        {'n_estimators': 60, 'max_depth': 8, 'min_samples_leaf': 2, 'max_features': 'sqrt'},
        {'n_estimators': 100, 'max_depth': 12, 'min_samples_leaf': 2, 'max_features': 0.7},
    ]

    best_model = None
    best_params = None
    best_val_metrics = None

    for params in candidate_params:
        model = RandomForestRegressor(random_state=42, n_jobs=-1, **params)
        model.fit(train_X, train_y)
        val_pred = model.predict(val_X)
        val_metrics = metrics_for_targets(val_y, val_pred)
        print('Candidate params:', params)
        print(json.dumps({
            'MAE': val_metrics['MAE'].tolist(),
            'RMSE': val_metrics['RMSE'].tolist(),
            'SD_of_error': val_metrics['SD_of_error'].tolist(),
            'ME': val_metrics['ME'].tolist(),
            'Pearson_r': val_metrics['Pearson_r'].tolist(),
            'R2': val_metrics['R2'].tolist(),
        }, indent=2))

        if best_val_metrics is None or np.mean(val_metrics['MAE']) < np.mean(best_val_metrics['MAE']):
            best_model = model
            best_params = params
            best_val_metrics = val_metrics

    val_pred = best_model.predict(val_X)
    val_metrics = metrics_for_targets(val_y, val_pred)
    print('Validation metrics:', json.dumps({
        'best_params': best_params,
        'MAE': val_metrics['MAE'].tolist(),
        'RMSE': val_metrics['RMSE'].tolist(),
        'SD_of_error': val_metrics['SD_of_error'].tolist(),
        'ME': val_metrics['ME'].tolist(),
        'Pearson_r': val_metrics['Pearson_r'].tolist(),
        'R2': val_metrics['R2'].tolist(),
    }, indent=2))

    model_bundle = {
        'model': best_model,
        'feature_version': 'signal_summary_v1',
        'target_names': ['systolic_bp_mmHg', 'diastolic_bp_mmHg'],
        'training_subjects': train_used,
        'validation_subjects': val_used,
        'test_subjects': test_used,
        'train_shape': train_X.shape,
        'val_shape': val_X.shape,
        'test_shape': test_X.shape,
    }

    model_path = ARTIFACTS_DIR / 'bp_model.joblib'
    joblib.dump(model_bundle, model_path)
    print(f'Model artifact saved to {model_path}')

    test_pred = best_model.predict(test_X)
    test_metrics = metrics_for_targets(test_y, test_pred)
    print('Test metrics:', json.dumps({
        'MAE': test_metrics['MAE'].tolist(),
        'RMSE': test_metrics['RMSE'].tolist(),
        'SD_of_error': test_metrics['SD_of_error'].tolist(),
        'ME': test_metrics['ME'].tolist(),
        'Pearson_r': test_metrics['Pearson_r'].tolist(),
        'R2': test_metrics['R2'].tolist(),
    }, indent=2))

    return {'best_model': best_model, 'val_metrics': val_metrics, 'test_metrics': test_metrics}


if __name__ == '__main__':
    train_model()
