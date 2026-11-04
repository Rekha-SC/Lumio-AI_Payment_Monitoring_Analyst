# Lumio — ML pipeline walkthrough (behind the dashboard)

Notebook: `analysis/Lumio_ML_Pipeline_Walkthrough.ipynb` in Lumio_Live_Demo.zip. It runs the app's real back-end modules step by step on a private database.

## Results
| Pipeline step | What we did | Key result |
|---|---|---|
| Load | CSV → SQL (Lumio schema), reconciliation, streaming ingest | CSV and database match exactly; one 5-min batch = 48 rows summarising ~6,642 payments |
| Clean | Validation, quality gate, mixture-p95 aggregation | Missing collector → *hold*; duplicates → *dedup*; no false pages from bad data |
| EDA | Distributions, seasonality, payday, noise vs volume | Log transform for time and volume; payday evening 1.7× a normal Friday |
| Features | 9 calendar + segment features; nothing measured "now" | Avoids leakage; expectations can be pre-computed |
| Split | Time-based train / validation / test | Random split R² 0.829 vs honest time split 0.808 |
| Train & evaluate | Seasonal baseline vs Ridge vs gradient boosting | Gradient-boosting latency test R² **0.807**, error ≈ **122 ms** (MAPE 5.0 %) |
| Tune | Randomised search + time-series CV; validation curve | Tuned validation MAE 0.0506 vs app 0.0512 vs defaults 0.0507 |
| Score | Noise model × robust segment spread → z | Notebook model reproduces the app's model exactly |
| Threshold | Sensitivity sweep, confirmation, false-alarm breakdown | At 4.5 + 2 scans: 11/11 incidents, median 30 min to detect; precision 0.19 model-only → 1.00 with storm advisory + change calendar |
| Live | Stream → ingest → score → `bucket_scores` → dashboard chart + heatmap | The data updates every 5 min; the model stays fixed |
| Monitor | Daily accuracy, PSI drift, save/load | Stable accuracy; storm detected as temporary drift → advisory, not retraining |

import warnings; warnings.filterwarnings("ignore")
import os, sys, time, json, math, sqlite3
from pathlib import Path
from datetime import datetime, timedelta

# Point Lumio's back end at a private notebook database BEFORE importing it
HERE = Path.cwd()
APP = next(p for p in [HERE.parent, HERE, HERE / "lumio-live-demo"] if (p / "lumio" / "model.py").exists())
DB_PATH = HERE / "pipeline_demo.db"
for f in [DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")]:
    if f.exists(): f.unlink()
os.environ["LUMIO_DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["LUMIO_MODEL_DIR"] = str(HERE / "models_notebook")
sys.path.insert(0, str(APP))

import numpy as np, pandas as pd
import matplotlib.pyplot as plt, matplotlib.dates as mdates, seaborn as sns
from matplotlib.patches import FancyBboxPatch
from IPython.display import display, Markdown
from sqlalchemy import select, insert, text
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.model_selection import TimeSeriesSplit, RandomizedSearchCV, train_test_split, validation_curve
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score, precision_score, recall_score, f1_score, confusion_matrix, ConfusionMatrixDisplay

from lumio import config, db, ingest, quality, world            # ← the app's own back-end modules
from lumio.model import Baseline, aggregate, features, FEATS, lat_noise, succ_noise, vol_noise, mix_p95

sns.set_theme(style="whitegrid")
plt.rcParams.update({"figure.figsize": (11, 4), "figure.dpi": 110, "axes.titleweight": "bold", "axes.titlesize": 12,
                     "axes.spines.top": False, "axes.spines.right": False})
pd.set_option("display.max_columns", 40); pd.set_option("display.width", 200)
TEAL, AMBER, INK, RED, GREY, BLUE = "#0F766E", "#D97706", "#14161A", "#B91C1C", "#9CA3AF", "#2563EB"
DATA = APP / "data" / "dataset"
SEG = ["bank", "channel", "platform", "region"]
R = {}                                   # results collected for the summary
def say(t): display(Markdown(t))
print("Lumio back end imported from:", APP.name + "/lumio")
print("Notebook database:", DB_PATH.name, "| live period:", config.LIVE_START, "→", config.LIVE_END)

# Professor's likely questions on the pipeline

**1. Does the model update with every transaction?**
No. Transactions are summarised into 5-minute rows; the model scores each new row against what it learned from history. It is re-trained on a schedule, not continuously, so outages never become "normal" (Step 0, Step 10).

**2. Why aggregate to 5 / 15 minutes instead of scoring each payment?**
A single payment carries almost no signal; a few hundred have a measurable average, failure rate and p95 with known noise. Aggregation also scales to millions of payments and protects privacy.

**3. How did you split the data, and why not randomly?**
By time: train 17 Jan – 2 Feb, validate 3 – 6 Feb, test on the 10 live days. A random split leaks neighbouring windows from the same day into training and overstates accuracy (Step 5).

**4. What features did you engineer, and what did you leave out on purpose?**
Calendar (fractional hour, weekday, weekend, payday, payday evening) and segment identity. Anything measured "now" was left out, because it would leak the anomaly into the expectation (Step 4).

**5. Why log-transform the targets?**
Payment time and volume are right-skewed and change multiplicatively; logs make residuals symmetric and comparable across segments (Step 3).

**6. Why gradient boosting rather than linear regression?**
It captures interactions (e.g. Card × evening × payday) without hand-made terms, and it was best or equal on validation. Ridge and the seasonal baseline were kept as benchmarks (Step 6).

**7. How did you tune it, and did tuning matter much?**
Randomised search with time-series CV, then checked on validation; the validation curve shows where over-fitting starts. The gain was modest: features and data quality mattered more (Step 7).

**8. How do you avoid over-fitting?**
Time-based validation, the train vs validation gap, regularisation (min samples per leaf, L2), and a validation curve on boosting rounds.

**9. Which evaluation metrics, and why not accuracy?**
MAE, RMSE, R², MAPE for the regression; precision, recall, F1, incidents caught and time to detect for detection. Incidents are rare, so accuracy would be misleading (Steps 6, 9).

**10. How was the alert threshold chosen?**
A sweep of sensitivity against precision, recall, false alarms and incidents caught, plus 2-scan confirmation. The default keeps every incident caught with a safety margin; the false alarms that remain come from known contexts (storm, maintenance), which later layers handle (Step 9).

**10b. Your model-only precision is low. Isn't that a weak model?**
No. The breakdown shows almost all of those flags are real volume collapses during the storm and real slowness during planned maintenance. The *data* really was abnormal, but it was not an incident. Separating "abnormal" from "actionable" is the job of the context layers, and with them precision is close to 1.

**11. Why does a small segment get a wider band?**
Fewer payments mean more sampling noise. The z-score divides by a volume-based noise model times a robust (MAD) per-segment spread (Steps 3, 8).

**12. What happens when data is missing or duplicated?**
The quality gate holds the segment (no scoring, a data ticket) or removes duplicates before scoring (Step 2).

**13. How would you know when to retrain?**
Monitor daily error against the validation level and drift (PSI); retrain on schedule or on a lasting shift, never on a temporary event like the storm (Step 11).

**14. Is the notebook really the same as the app?**
Yes. It imports the app's modules, trains with `Baseline().train()` from the same SQL table, and shows the predictions are identical (Step 8).
