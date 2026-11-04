# Lumio — live demo

**See what your systems are hiding.** Lumio is an always-on AI data analyst that sits on top of a bank's monitoring tools. It catches payment problems that never trip a technical alarm, works out the cause, and emails the right department with what to do next.

This package runs the whole thing on a laptop: ten days of realistic payment data streaming live, with every anomaly found, explained, routed and emailed as it happens.

| | |
|---|---|
| **Data** | 10 days, Fri 7 – Sun 16 February 2025: **6 business days + 2 weekends**. 48 payment segments (4 banks × UPI/Card × Android/iOS × 3 regions) plus 8 upstream services, every 5 minutes |
| **Real data** | Three traces from the **Numenta Anomaly Benchmark** (NAB) drive volume and two services, including three of NAB's real labelled anomalies: a blizzard, a system failure and a database level shift |
| **Anomalies** | 19 planted events. Lumio is never told about them; the Proof tab scores it live |
| **Speeds** | 1× (narrate) · 2× · 4× · 6× · 8× · 10×, plus *Jump to* any day and *Next anomaly* |
| **Email** | Real HTML emails through your own mailbox, routed to 12 departments, with a chart, action items and working buttons |

---

## Start it (about 2 minutes)

You need **Python 3.10 or newer** ([python.org](https://www.python.org/downloads/)).

| Computer | Do this |
|---|---|
| Windows | Double-click **`start_windows.bat`** |
| Mac / Linux | Run **`./start_mac_linux.sh`** |
| Any | `pip install -r requirements.txt`, then `python run_local.py` |

The dashboard opens at **http://localhost:8000**. The first start loads three weeks of training history and trains the models in about 30 seconds. Use `python run_local.py --fresh` to wipe everything and start over.

**Email:** open **Settings → Email delivery**, then follow [EMAIL_SETUP.md](EMAIL_SETUP.md) (Gmail takes about 3 minutes). Without email set up, every alert is still composed and shown in the **Email outbox** tab.

**As separate services on PostgreSQL:** `docker compose up --build`. This runs the API, the simulator (which posts to the ingest API), the worker (detection, investigation and email) and PostgreSQL 16.

---

## What happens over the 10 days

| Day | Planted event | What Lumio does | Routed to |
|---|---|---|---|
| **Fri 7** (business day 1) | 10:20 Card network partial outage | Alert. Cause: card-network service, 3× slower with 6% errors | Card Operations, SRE |
| | 14:00 Bank Z West collector stops sending | Holds the data, no page; data ticket | Monitoring & Data |
| | 19:00 Bank X Android app v5.2 slows UPI | Alert. Cause: v5.2 release, 3.3× slower than v5.1. **Pause rollout** button | Release Mgmt, Mobile Eng |
| **Sat 8** | 02:00 Planned maintenance, Bank W core banking | Recognised from the change calendar: no page | — |
| | 15:30 Card-testing bot attack on Bank Y | Alert. Cause: bursts of payments under $5, 86% declined. **Block pattern** button | Fraud & Risk, Card Ops |
| **Sun 9** | 21:00 UPI switch memory leak (slow drift) | Alert as it crosses the line; widens as it spreads | SRE |
| **Mon 10** (BD 2) | 10:55 Bank Z iOS v3.9 login bug | Alert: "payments vanishing" on v3.9 | Release Mgmt, Mobile Eng |
| | 15:00 → **Winter storm (real NAB blizzard)** | One advisory: demand, not a fault. No segment pages | Business Operations |
| **Tue 11** (BD 3) | 09:40 One-off core-banking spike at Bank W (real NAB) | Seen once, not paged | — |
| | 15:00 Regional internet outage, West, *during the storm* | Alert. Cause: regional network | Network Ops, SRE |
| **Wed 12** (BD 4) | 09:30 Duplicate records, Bank W North | Removes duplicates; data ticket | Monitoring & Data |
| | 13:00 **Fraud-check DB saturates (real NAB level shift)** | Alert. Cause: fraud-check service | Fraud & Risk, SRE |
| | 20:00 High-value card payments time out at Bank Y | Scripts can't explain it, so the **AI agent** finds payments over $500 (about half fail) | Payments Product, Card Ops |
| **Thu 13** (BD 5) | 08:15 UPI switch partial outage | Alert. Cause: upi-switch | SRE |
| | 14:00 **Bank W core banking failure (real NAB system failure)** | Alert. Cause: core-banking-W | Core Banking, SRE |
| **Fri 14** (BD 6) | Payday + Valentine's evening rush | No alert: learned pattern. A fixed rule pages ~50 segments per scan | — |
| | 19:30 Bank X core banking saturates during the payday peak | Alert: the real problem is separated from the expected rush | Core Banking, SRE |
| **Sat 15** | 11:00 Collector stops sending, all of Bank Y | Data ticket, no page | Monitoring & Data |
| **Sun 16** | 03:00 Planned maintenance, Bank X core banking | No page | — |

The full 10-day test scores **19 of 19 with zero false alarms**. A fixed-threshold rule would have paged about 4,900 times on the same data.

**Emails during the run:**
- an alert for each incident
- a "resolved" email when it closes
- an escalation to the Duty Manager if nobody acknowledges within 30 simulated minutes
- data-quality tickets
- the storm advisory
- an end-of-day report at 18:00 on each business day

Every type can be switched off in Settings.

---

## How triggers work

Every 15 simulated minutes Lumio scores all 48 segments on three signals:

- **payment time**
- **success rate**
- **payment volume**

Each signal is compared with what its ML models expect *for that moment*: hour, weekday and payday are all accounted for. A segment is abnormal when it moves more than the **sensitivity** threshold, shown as the amber line on the chart. **Business guardrails** apply on top: a success-rate floor and a payment-time ceiling. Before anyone is paged, three more checks apply:

1. **Data-quality gate:** missing or duplicated monitoring data is held back, so it can never raise an alarm.
2. **Grouping:** abnormal segments are grouped into one story ("Bank X · UPI · Android", not three alerts).
3. **Confirmation:** the story must persist for **2 scans**, which stops one-off blips from paging anyone.

Planned maintenance from the change calendar is expected. System-wide demand swings become a single advisory.

**Root cause** is found by scripted checks, in this order:

1. recent release and app-version split
2. upstream services (latency and errors)
3. region
4. fraud signature

Only when they all fail does the **AI agent** search transaction samples. Every claim it makes must pass a significance test before it's believed.

Everything is editable live in **Settings → Alert rules**.

## Speeds

| Speed | One business day takes | All 10 days take |
|---|---|---|
| 1× | 24 min | 4 h |
| 4× | 6 min | 1 h |
| 10× | ~2.4 min | ~24 min |

At 1×, 5 simulated minutes pass every 5 seconds. **Jump to** a day fast-forwards with every scan processed on the way (about 15–30 seconds per simulated day) and pauses on arrival. **Next anomaly** stops 15 minutes before the next planted event.

## The dashboard

| Tab | What it shows |
|---|---|
| **Live** | KPIs; payment time vs expected with the alert line; success and volume charts; alert cards with their routed teams and action buttons; the 48-segment heatmap; the activity feed |
| **Incidents** | Every incident, advisory and data ticket, with routing and acknowledgements |
| **Reports** | A 10-day summary, a card for each day, hourly charts, CSV download and **Email this report** |
| **Email outbox** | Every email exactly as received, with status (sent, failed or preview) and a resend button |
| **Data quality** | What the gate held or cleaned |
| **Learning** | Acknowledgements, actions and "not useful" feedback, and the per-segment thresholds |
| **Proof** | The answer key vs Lumio, live, plus the stress test and AI-agent cost |
| **Dataset** | NAB sources, the 10-day calendar (answer key hidden until revealed) and CSV downloads |
| **Settings** | Email server, alert rules and departments |
| **How it works** | The architecture with live counters |

## Files

```
lumio/          backend: simulator, ingest, quality gate, models, detector, investigator, agent,
                narrator, routing, mailer, reports, API
ui/             dashboard (HTML/JS, no build step)
data/nab/       the three NAB source files + labels + licence (MIT)
data/dataset/   the 10-day dataset and 3-week history as CSV, answer key, feeds
tests/          end-to-end test: python -m tests.test_e2e   (add --inject for live injections)
tools/          export_dataset.py regenerates data/dataset
analysis/       data analytics notebook + report (PDF/HTML) + professor Q&A — see analysis/README.md
```

## Honest notes

- **Simulated payments:** the payments are simulated. Their volume rhythm comes from real NAB human-demand data, and two services follow real NAB system traces, re-timed onto February 2025 and scaled. The labelled NAB anomalies are real; their impact on payments is modelled.
- **The offline agent:** without an `ANTHROPIC_API_KEY`, the AI agent runs as a deterministic planner using the same tools. With a key, Claude chooses the queries, and its answer is re-checked by code.
- **Simulated actions:** the "Pause rollout" and "Block pattern" buttons change the simulated world. In production they would call the release tool or the fraud engine.
- **Email buttons:** they link to the dashboard address set in Settings (`http://localhost:8000` by default), so they work on the presenting laptop.
