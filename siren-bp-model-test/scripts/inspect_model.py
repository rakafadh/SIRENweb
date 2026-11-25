"""Step 1. What is inside bp_model.joblib.

    python scripts/inspect_model.py
"""
import _path  # noqa: F401
import json

import numpy as np
import sklearn

from bp_model import FEATURE_NAMES, BpModel

m = BpModel()
forest = m.model
print("Model type        ", type(forest).__name__)
print("Saved with sklearn", m.saved_sklearn_version, "| running", sklearn.__version__)
print("Feature version   ", m.feature_version)
print("Targets           ", m.bundle.get("target_names"))
print("Hyperparameters   ", {k: forest.get_params()[k] for k in ("n_estimators", "max_depth", "min_samples_leaf", "max_features")})
print("Inputs            ", forest.n_features_in_, "features")
print("Train/val/test    ", m.bundle.get("train_shape"), m.bundle.get("val_shape"), m.bundle.get("test_shape"))
print("  (rows = patients, because training averages every window of a patient)")

print("\nFeature importance, highest first")
imp = forest.feature_importances_
for i in np.argsort(imp)[::-1]:
    bar = "#" * int(round(imp[i] * 200))
    print(f"  {FEATURE_NAMES[i]:<18} {imp[i]:.3f} {bar}")

if m.metrics:
    print("\nStored test metrics (bp_model_metrics.json)")
    print(json.dumps(m.metrics["targets"], indent=2))
