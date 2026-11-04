# Lumio capstone — professor's likely questions

Prepared answers for the viva / project defence. Section numbers refer to `Lumio_Data_Analytics_Report` (or the notebook).

## Numbers to remember
| Question | Answer from the data |
|---|---|
| Q1 · What is normal? | Strong daily + weekly seasonality (strength 0.99 / 0.98); busiest hour ≈ **19×** the quietest |
| Q2 · Do groups differ? | Bank matters (η² = 17.4%, large); Android vs iOS is a small effect (d = 0.34); channel vs failure is significant but negligible (Cramér's V = 0.0072) |
| Q3 · What drives payment time? | UPI switch alone explains R² = 0.85 of UPI payment time; multiple regression R² = 0.64 (train) / 0.61 (unseen live data); gradient boosting R² = 0.62 |
| Q3b · Root cause | App v5.2 is **3.2× slower** than v5.1 (Mann-Whitney, p < 0.001) |
| Q4 · Forecasting | Best model **SARIMA(1, 0, 1)(0,1,0)₁₆₈**, MAPE **5.9 %** on normal days (15 % lower error than seasonal naive); 82% of storm hours fall outside the 95 % interval vs 6% of other hours |
| Q5 · Detection | Fixed threshold: precision 0.11, 643 false-alarm scans/day · z-score rule: precision 0.63, recall 0.97, 47/day · logistic ROC-AUC **0.998**. All caught 5/5 test incidents; the full live app (with grouping + confirmation) scores 19/19 with zero false alarms |
| Q6 · Segments | K-means finds **4** behavioural clusters |
| Q7 · Is it worth it? | Median saving **$5.50M / year** for this network; P(net benefit > 0) = **100.0%**; still positive in most years down to ~5% of this size |

### A. Problem & project design
**1. Why is this a data analytics problem and not just an IT monitoring problem?**
Because the failures are *statistical*: every server is "up", but a slice of customers sees slower or failed payments. Spotting them needs baselines, seasonality, hypothesis tests and segmentation, which is analytics work, not uptime checks.

**2. Which methodology did you follow?**
CRISP-DM (Section 0): business understanding → data understanding → preparation → modelling → evaluation → deployment (the live app).

**3. Who are the stakeholders and how does your output reach them?**
Ops/SRE, Release, Fraud, Business Operations, Leadership (Section 0.2). The live app routes each alert by e-mail to the responsible department, with evidence and action buttons.

**4. How did you define success before you started?**
Section 0.4: R² ≥ 0.5 on unseen data, MAPE ≤ 10 %, catching every test incident with fewer false alarms than a fixed rule, and a positive expected net benefit. Section 13 reports against these.

### B. Data
**5. Is the data real?**
Partly, and that is deliberate. Volume rhythm, a blizzard, a system failure and a database level-shift come from the **Numenta Anomaly Benchmark** (real, labelled). Payment outcomes are simulated on top so the ground truth is known exactly. Real bank data is confidential *and* unlabelled, so you could not measure precision and recall honestly with it.

**6. How did you handle missing data and duplicates?**
Duplicates removed; gaps **not** filled with zeros, because a zero would fake an outage. IQR outliers kept because they are real peak behaviour (Section 2).

**7. Why use p95 instead of the average payment time?**
Latency is right-skewed; the 95th percentile captures the slow tail customers actually feel and is the industry-standard SLI.

**8. Why did you log-transform payment time?**
To reduce skew, stabilise variance and make coefficients read as % effects (Section 6.3).

### C. Statistics
**9. Your p-values are all tiny. Does that mean everything matters?**
No. With ~40 M payments, tiny differences are significant. That is why we report **effect sizes** (Cohen's d, η², Cramér's V). Channel vs failure is significant but negligible (Section 5.2).

**10. Why Welch's t-test instead of Student's t-test?**
Levene's test showed unequal variances; Welch does not assume them.

**11. Why Mann-Whitney for the release comparison?**
p95 values are skewed and the samples are small, so a non-parametric test is safer. Fisher's exact test is used for failure counts in a 2×2 table.

**12. Isn't running many tests a multiple-comparisons problem?**
Yes. Section 11.4 simulates it: 13,824 checks a day at z > 3 still give dozens of false pages a day. We control it with confirmation over 2 scans, grouping and minimum-volume rules, the operational equivalent of a family-wise correction. In a purely academic setting, Bonferroni or Benjamini-Hochberg would be the textbook fixes.

### D. Regression
**13. Interpret one coefficient for me.**
Use Section 6.3, e.g. "Holding bank, region, hour and load constant, UPI payments are about X % faster than card payments", where X comes from the coefficient table.

**14. Did you check the regression assumptions?**
Yes (Section 6.4): residual plot, Q-Q plot, Breusch-Pagan (heteroscedasticity → robust HC3 errors), Durbin-Watson (autocorrelation in time data), VIF (multicollinearity).

**15. What is confounding, and where did you see it?**
Amount appeared to slow payments, but card payments are both bigger and slower. Adding channel removed the effect (Section 6.2). This is omitted-variable bias.

**16. Why is R² on training data different from R² on the test data?**
Test data is unseen and from a different period. A small drop is normal; a large drop would mean overfitting. We report both (6.3, 6.5).

**17. Linear regression vs gradient boosting: which would you deploy, and why?**
Gradient boosting fits better (captures interactions), and we explain it with permutation importance and partial dependence. Linear regression stays as the interpretable benchmark and the teaching model (Section 9).

### E. Time series
**18. What is stationarity and why does it matter?**
Constant mean and variance over time; ARIMA-type models assume it. ADF and KPSS (opposite nulls) show seasonal differencing is needed (Section 7.2).

**19. How did you choose the SARIMA orders?**
The ACF shows 24 h and 168 h seasonality, so we difference by one week (D = 1, s = 168). The PACF of the differenced series suggests a low order, and we compared candidate ARMA orders by **AIC** and by **validation MAPE** on the last training week, choosing by validation because the goal is forecasting (Section 7.4). A payday regressor was added. It was then tested walk-forward on the 10 live days against seasonal naive and Holt-Winters.

**19b. AIC picked a different model from validation. Which is right?**
AIC measures in-sample fit with a complexity penalty; validation measures accuracy on unseen days. For a forecasting goal, out-of-sample performance wins, and here the validation choice also did better on the test days.

**20. What is walk-forward validation and why use it?**
Each day we refit on everything known up to midnight and forecast the next 24 hours, which is how the forecast would be used in operations. It never lets the model see the future, and it tests the model 10 separate times instead of once.

**20b. Why exclude the storm when measuring forecast accuracy?**
No model should predict a blizzard from payment history. We measure accuracy on normal hours, then show the storm is *detected* because it falls outside the prediction interval (Section 7.5).

**21. Why not shuffle the data for cross-validation?**
Shuffling leaks the future into training. We use time-based splits and TimeSeriesSplit.

### F. Classification & evaluation
**22. Why not report accuracy?**
Only about 2–4 % of scans are inside an incident: a model that always says "normal" would be over 96 % accurate and completely useless. We use precision, recall, F1, ROC-AUC and PR-AUC.

**23. Precision or recall: which matters more here?**
Both have a cost: a missed incident loses money; a false alarm wastes people's time and erodes trust (alert fatigue). The threshold curve (Section 8.1) shows the trade-off, and the Monte Carlo puts dollars on it.

**24. How do you interpret the logistic-regression odds ratios?**
The multiplier on the odds of a real incident per 1-SD rise in a feature (Section 8).

**25. Your logistic model was trained with labels. Doesn't that use the answer key?**
Only for this classroom comparison, and only on the training days. The live app's detector never sees labels; it uses the unsupervised expected-vs-actual rules and is scored afterwards.

### G. Clustering & Monte Carlo
**26. How did you pick k in K-means?**
Elbow method plus silhouette score (Section 10). The silhouette peaks at k = 2, but that is just UPI vs Card, which we already knew. k = 4 still has reasonable separation and reveals a high-traffic/fast vs low-traffic/slow tier inside each channel, which is actionable. Features were standardised first because K-means uses distances. We also dropped the demand-shape features after finding they carried no information.

**27. Why Monte Carlo and not just one ROI number?**
The inputs are uncertain. Monte Carlo gives a distribution: median saving, probability of a positive return, a bad-year (5th percentile) case, and the bank size where the case breaks even (Section 11).

**27b. How did you measure the cost of an incident?**
From the data: excess failures against the segment's normal failure rate, plus vanished payments measured by **difference-in-differences** against unaffected segments (a control group). Without the control group, a quiet Sunday night or the storm would be miscounted as lost money.

**28. How many simulations are enough?**
Until the running mean stabilises. The convergence plot shows the CI narrowing as 1/√n; 10,000 is well past that.

**29. Which assumption matters most?**
The tornado chart (Section 11.3) ranks them, which tells the bank what to measure first in a pilot.

### H. Ethics, limitations & future work
**30. Any privacy concerns?**
Only aggregated, non-personal telemetry is used; production would add pseudonymisation and access controls (PIPEDA / GDPR / PCI-DSS). See Section 12.

**31. What are the main limitations?**
Simulated outcomes, one season of history, known incident types, autocorrelation and business-case assumptions (Section 13).

**32. If you had 3 more months, what would you do?**
Pilot on one bank's real data; 12 months of history for yearly seasonality; drift monitoring and monthly retraining; validate the Monte Carlo inputs; A/B-test alert thresholds with the on-call team.

**33. What did you personally learn?**
*(Answer in your own words.)* For example: that "significant" isn't "important", that the baseline matters more than the threshold, and that the hardest part of analytics is turning a number into an action someone takes.

---
*Data: Numenta Anomaly Benchmark (Lavin & Ahmad, 2015; MIT licence) + simulated payments. Methods: pandas, SQLite, SciPy, statsmodels, scikit-learn.*
