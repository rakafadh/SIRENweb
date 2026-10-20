from pathlib import Path
import numpy as np

base = Path('e:/Main Room/UI KEOS (was ITB)/Tingkat Akhir/Despro/dataset')
ppg_dir = base / 'ppg'
label_dir = base / 'labels'
files = sorted(ppg_dir.glob('*.npy'))
print('ppg_files', len(files))
print('sample file', files[0].name)
wave = np.load(files[0])
print('wave shape', wave.shape, wave.dtype)
label_file = label_dir / f"{files[0].stem.replace('_ppg', '')}_labels.npy"
print('label file', label_file)
labels = np.load(label_file)
print('label shape', labels.shape, labels.dtype)
print('first label', labels[0])
for split_name in ['train_subjects.txt','val_subjects.txt','test_subjects.txt']:
    subjects = [line.strip() for line in (base / split_name).read_text().splitlines() if line.strip()]
    print(split_name, len(subjects), subjects[:5])
