# Lumio — data analytics project (for the professor)

This folder is the **analysis side** of the capstone. The live app shows Lumio working; this notebook shows the data analytics behind it, step by step, on the same dataset.

| File | What it is | Open with |
|---|---|---|
| `Lumio_Data_Analytics_Report.pdf` | The full report: every chart, table and finding, without code (31 pages) | Any PDF reader |
| `Lumio_Data_Analytics_Report.html` | The same report as a web page | Any browser |
| `Lumio_Data_Analytics_Project.ipynb` | The runnable notebook (code + results) | Jupyter, VS Code, or Google Colab |
| `Lumio_Data_Analytics_Project_with_code.pdf` / `.html` | The notebook with all code shown | PDF reader / browser |
| `PROFESSOR_QA.md` | 35+ likely professor questions with prepared answers, plus the key numbers | Any text viewer |

### Behind the dashboard: the ML pipeline walkthrough (separate notebook)

| File | What it is |
|---|---|
| `Lumio_ML_Pipeline_Walkthrough.ipynb` | Runs Lumio's **real back-end code** step by step: load (CSV and SQL), clean (quality gate), EDA, feature engineering, time-based train/validation/test split, training and evaluation, hyperparameter tuning, anomaly scores, threshold tuning, live streaming into the database, rebuilding the dashboard chart and heatmap from SQL, monitoring and retraining. Answers "does it update with every transaction?" |
| `Lumio_ML_Pipeline_Walkthrough.pdf` / `.html` | The same with all code and outputs (33 pages) |
| `Lumio_ML_Pipeline_Report.pdf` / `.html` | Results only, without code (18 pages) |

It creates its own `pipeline_demo.db` and `models_notebook/` in this folder and never touches the live app's database. Runtime about 2 minutes.

## What it covers

CRISP-DM project framing · data dictionary · data quality · EDA · **SQL** (JOIN, CTE, window functions) · **hypothesis testing** (t-test, χ², ANOVA + Tukey, Mann-Whitney, Fisher, confidence intervals, effect sizes) · **linear regression** (simple, multiple, confounding, diagnostics) · **time series** (MSTL, ADF/KPSS, ACF/PACF, Holt-Winters, SARIMA, walk-forward validation) · **logistic regression** & evaluation (ROC, precision-recall) · **gradient boosting** + explainability · **K-means clustering** + PCA · **Monte Carlo simulation** (business case, sensitivity, alert design) · ethics · results, limitations, recommendations.

## Run it yourself

```
pip install -r requirements.txt
jupyter notebook Lumio_Data_Analytics_Project.ipynb
```
Then choose **Run All**. The analytics notebook takes about 1 minute; the pipeline notebook about 2 minutes (it imports the app's `lumio` package from the parent folder, so keep the folder layout).

**Google Colab:** upload the notebook and the `data/dataset` folder, keeping the same folder layout (`data/dataset/...` next to the notebook).
