# Lumio live demo — presenter script (12–15 minutes)

**Before you start:**
- Run the app.
- Set up email (see EMAIL_SETUP.md) and send a test.
- Press **Reset**.
- Open your inbox in a second window.
- Keep the **Live** tab in front.

---

### 1. The setup (Fri 7 Feb, 00:00) — 1 min

> "Ten days of payments: six business days and two weekends. 48 slices of traffic: four banks, UPI and card, Android and iOS, three regions. The rhythm of demand comes from real data, the Numenta Anomaly Benchmark. Lumio has learned what normal looks like for every hour of the week, and it has never seen these ten days."

Show the **Dataset** tab for a moment: the three NAB sources and the calendar, with the answer key still hidden.

### 2. Business day 1 — the card network (Jump → Fri 08:00, then Next anomaly) — 2 min

Press **Play** at 4×. At 10:20 the Card column turns amber. Lumio waits for a second scan, then alerts at 10:45.

> "One alert, not 24. Lumio found the cause, the card-network service, and emailed only Card Operations and SRE."

**Switch to your inbox:** `[HIGH] INC-0001` arrives with the chart, the action items and the buttons. Click **Acknowledge**, pick a team, and watch the dashboard update.

### 3. The release that slows payments (Next anomaly → Fri 18:45) — 2 min

At 18:58 the deploy feed shows the v5.2 rollout. The alert comes at 19:30.

Open **Evidence**:
- the investigation trace
- v5.2 is 3.3× slower than v5.1
- routed to Release Management and Mobile Engineering

Click **Pause v5.2 rollout**.

> "The rollout is paused. Lumio keeps watching and closes the incident itself."

### 4. The weekend — calm on purpose (Jump → Sat 08:00) — 1 min

> "Planned maintenance overnight? It's in the change calendar. No page."

Then **Next anomaly**: the card-testing attack at 15:30. Lumio alerts Fraud & Risk. Click **Block pattern (fraud rule)**, and the attack stops.

### 5. The storm — real data (Jump → Mon 13:00, play at 6×) — 2 min

Volume falls at every bank: this is NAB's real January 2015 blizzard. Success rates and payment times stay healthy.

> "A naive system would page every bank. Lumio sends one advisory to Business Operations: this is demand, not a fault."

Keep playing into **Tuesday 15:00**: the West region outage appears *during* the storm, and Lumio still separates it.

### 6. Real failures and the AI agent (Jump → Wed 12:30) — 2 min

- 13:00: the fraud-check database saturates. This is NAB's real level shift. Every card payment slows; the alert names fraud-check.
- 20:00 (Next anomaly): high-value card payments time out at Bank Y. The scripted checks can't explain it, so the **AI agent** finds that payments over $500 fail about half the time. Point at the cost line in the drawer.

### 7. Payday + Valentine's (Jump → Fri 14 16:00, play at 6×) — 1 min

> "The busiest evening of the month. A fixed rule would fire about 50 alerts every 15 minutes. Lumio stays quiet — until 19:30, when Bank X's core banking really does saturate. That one it catches."

### 8. Reports and proof — 2 min

- **Reports:** the day cards, hourly charts and **Email this report**. The end-of-day emails are already in your inbox.
- **Proof:** every planted event, what Lumio did, how fast, and which team got it. Zero false alarms.
- **Settings → Alert rules:** drag the sensitivity slider and show that the amber line on the chart moves.

### Q&A move — "break something"

Click **Inject**, let the audience pick a bank, and choose *Payments start failing*. Play at 4×. Within about 30 simulated minutes the alert appears, routed, and in the inbox.

> "There's no hidden cause here, so Lumio says *cause unknown* and hands over the evidence. It doesn't make one up."
