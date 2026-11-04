"""The simulated payment world, driven by real NAB traces.

Payment volume follows real human demand (NAB nyc_taxi). Every payment depends on upstream services
(UPI switch, card network, fraud check, each bank's core banking); when a service degrades, the payments
that depend on it slow down or fail — so root causes are real cause-and-effect, not labels.
Everything is seeded by timestamp: the same 10 days replay identically after a reset.
"""
import math, zlib
from datetime import datetime, timedelta
import numpy as np
from . import config, nab

BANK_F = {"Bank X": 1.0, "Bank Y": 0.8, "Bank Z": 0.6, "Bank W": 0.5}
CHAN_F = {"UPI": 1.0, "Card": 0.55}
PLAT_F = {"Android": 0.62, "iOS": 0.38}
REG_F = {"North": 0.40, "South": 0.35, "West": 0.25}
BASE_P95 = {"UPI": 1750.0, "Card": 2150.0}
BASE_SUCC = {"UPI": 0.986, "Card": 0.984}
BASE_VERSION = {"Bank X": "5.1", "Bank Y": "7.0", "Bank Z": "3.8", "Bank W": "2.4"}
SERVICE_BASE = {"upi-switch": 180.0, "card-network": 240.0, "fraud-check": 120.0}
SERVICE_BASE.update({c: 300.0 for c in config.CORE.values()})
SERVICE_WEIGHT = {"upi-switch": 0.8, "card-network": 0.8, "fraud-check": 0.9}
SERVICE_WEIGHT.update({c: 0.6 for c in config.CORE.values()})
HV_SHARE = 0.10
LAT_SIGMA = 0.35
D = datetime

def rng_for(ts, salt=""):
    return np.random.default_rng(zlib.crc32(f"{ts.isoformat()}{salt}".encode()))

def is_payday(ts):
    """Payday is the 15th and the last day of the month; if that falls on a weekend, the Friday before."""
    d = ts.date() if isinstance(ts, datetime) else ts
    for cand in (d.replace(day=15), (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)):
        while cand.weekday() >= 5:
            cand -= timedelta(days=1)
        if cand == d:
            return True
    return False

def calendar(ts):
    """(volume multiplier, latency multiplier, success shift, payday-evening flag) for this moment."""
    h = ts.hour + ts.minute / 60
    vol = nab.volume_mult(ts)
    lat = 1 + 0.18 * (min(vol, 1.6) - 0.25)
    succ = -0.004 * (min(vol, 1.6) - 0.25)
    pe = False
    if is_payday(ts) and 16.5 <= h < 22.5:
        edge = min(1.0, (h - 16.5) / 0.75, (22.5 - h) / 0.75)
        vol *= 1 + 0.6 * edge; lat *= 1 + 0.3 * edge; succ -= 0.002 * edge
        pe = True
    return vol, lat, succ, pe

def in_scope(row, scope):
    return all(row.get(k) == v for k, v in (scope or {}).items())

def _t(s):
    return datetime.fromisoformat(s) if isinstance(s, str) else s

def active(f, ts):
    return _t(f["start"]) <= ts < (_t(f["end"]) if f.get("end") else datetime.max)

def ramp(f, ts):
    s = _t(f["start"]); r = f.get("ramp_min", 0)
    if f.get("shape") == "drift" and f.get("end"):
        return min(1.0, max(0.0, (ts - s).total_seconds() / (_t(f["end"]) - s).total_seconds()))
    k = 1.0 if r <= 0 else min(1.0, max(0.0, ((ts - s).total_seconds() / 60 + 5) / r))
    if f.get("end") and f.get("shape") != "drift":
        left = (_t(f["end"]) - ts).total_seconds() / 60
        if left < 10:
            k *= max(0.0, left / 10)
    return k

def rollout_share(f, ts):
    s = _t(f["start"])
    if ts < s:
        return 0.0
    share = min(f["to_share"], f["to_share"] * ((ts - s).total_seconds() / 60 + 5) / f.get("ramp_min", 30))
    stop = min([_t(x) for x in (f.get("paused_at"), f.get("rollback_at")) if x] or [datetime.max])
    if ts >= stop:
        share *= max(0.0, 1 - ((ts - stop).total_seconds() / 60 + 5) / 15)
    return share

def service_state(ts, faults, rng):
    """Latency factor and error rate of every upstream service for this bucket."""
    out = {}
    for s in config.ALL_SERVICES:
        f, err = math.exp(rng.normal(0, 0.05)), 0.002
        if s == config.CORE["Bank W"]:
            r = nab.corebank_w(ts)
            if r: f, err = f * r[0] / 1.03, r[1]
        if s == "fraud-check":
            r = nab.fraudcheck(ts)
            if r: f = f * r
        out[s] = [f, err]
    for fl in faults:
        if fl.get("service") and fl["kind"] in ("service", "maintenance") and active(fl, ts):
            k = ramp(fl, ts)
            out[fl["service"]][0] *= 1 + (fl.get("service_x", 1) - 1) * k
            out[fl["service"]][1] += (fl.get("error_rate", 0.002) - 0.002) * k
    return out

def generate_bucket(ts, faults, live=True, samples=True):
    """One 5-minute bucket: segment metrics (per app version), service metrics and sampled transactions."""
    rng = rng_for(ts)
    vol_m, lat_m, succ_s, payday_eve = calendar(ts)
    svc = service_state(ts, faults, rng)
    rows, svc_rows, smp = [], [], []
    for s, (f, err) in svc.items():
        svc_rows.append({"ts": ts, "service": s, "p95_ms": round(SERVICE_BASE[s] * lat_m * f, 1),
                         "error_rate": round(max(0.0, err + rng.normal(0, 0.0004)), 5)})
    seg_faults = [f for f in faults if f["kind"] in ("segment", "dq_gap", "dq_dup") and active(f, ts)]
    releases = [f for f in faults if f["kind"] == "release"]
    for bi, bank in enumerate(config.BANKS):
        for ch in config.CHANNELS:
            dep = config.services_for(ch, bank)
            dep_lat = 1.0; dep_fail = 0.0
            for s in dep:
                dep_lat *= 1 + SERVICE_WEIGHT[s] * (svc[s][0] - 1)
                dep_fail += 0.9 * max(0.0, svc[s][1] - 0.002)
            for pl in config.PLATFORMS:
                versions = [(BASE_VERSION[bank], 1.0, None)]
                for rf in releases:
                    if rf["bank"] == bank and rf["platform"] == pl:
                        sh = rollout_share(rf, ts)
                        if sh > 0:
                            versions = [(BASE_VERSION[bank], 1 - sh, None), (rf["version"], sh, rf)]
                for rg in config.REGIONS:
                    base_vol = 1800 * BANK_F[bank] * CHAN_F[ch] * PLAT_F[pl] * REG_F[rg] * vol_m
                    for ver, vshare, rf in versions:
                        row = {"bank": bank, "channel": ch, "platform": pl, "region": rg, "app_version": ver}
                        p95 = BASE_P95[ch] * (1 + 0.06 * bi) * (1.05 if rg == "West" else 1) * (0.95 if pl == "iOS" else 1) * lat_m * dep_lat
                        succ = BASE_SUCC[ch] + succ_s - dep_fail
                        vx, hv_fail, attack, dup, gap = 1.0, 0.0, 0.0, False, False
                        if rf and in_scope(row, rf.get("effect_scope")):
                            p95 *= rf.get("latency_x", 1); succ -= rf.get("success_drop", 0); vx *= rf.get("volume_x", 1)
                        for f in seg_faults:
                            if not in_scope(row, f.get("scope")):
                                continue
                            k = ramp(f, ts)
                            if f["kind"] == "dq_gap": gap = True; continue
                            if f["kind"] == "dq_dup": dup = True; continue
                            if f.get("high_value_only"):
                                hv_fail = f.get("hv_fail", 0.6) * k; succ -= HV_SHARE * hv_fail
                            else:
                                succ -= f.get("success_drop", 0) * k
                            p95 *= 1 + (f.get("latency_x", 1) - 1) * k
                            if f.get("attack"):
                                ax = 1 + (f.get("volume_x", 1) - 1) * k; vx *= ax; attack = (ax - 1) / ax
                            elif f.get("volume_x"):
                                vx *= 1 + (f["volume_x"] - 1) * k
                        n = max(1, int(rng.poisson(max(1.0, base_vol * vshare * vx))))   # collectors report quiet segments too
                        succ_clean = succ
                        if attack:
                            succ = (1 - attack) * succ + attack * 0.15
                        if rng.random() < 0.001: p95 *= 2.2              # network blip
                        if rng.random() < 0.001: n = max(1, n // 10)    # partial API dropout
                        noise = 0.025 + 0.45 / math.sqrt(n)
                        p95 = p95 * math.exp(rng.normal(0, noise))
                        sc = int(rng.binomial(n, min(0.9999, max(0.0, succ))))
                        amt = (38 if ch == "UPI" else 64) * math.exp(rng.normal(0, 0.05)) * (1 - 0.9 * attack)
                        if gap:
                            continue
                        rec = dict(row, ts=ts, volume=n, success_count=sc, p95_ms=round(p95, 1), avg_amount=round(amt, 2))
                        rows.append(rec)
                        if dup:
                            rows.append(dict(rec))
                        if samples and live:
                            smp.extend(_samples(rng, rec, succ_clean, p95, hv_fail, attack))
    return {"ts": ts, "metrics": rows, "services": svc_rows, "samples": smp, "payday_evening": payday_eve}

CATS_UPI = ["Grocery", "Food", "Transfers", "Bills", "Fuel"]
CATS_CARD = ["Grocery", "Food", "Retail", "Fuel", "Bills"]
CATS_HV = ["Electronics", "Travel"]

def _samples(rng, rec, succ, p95, hv_fail, attack):
    out = []
    k = int(min(12, max(2, rec["volume"] // 25)))
    med = p95 / math.exp(1.645 * LAT_SIGMA)
    for _ in range(k):
        lat = float(rng.lognormal(math.log(med), LAT_SIGMA))
        if attack and rng.random() < attack:
            amount, cat = float(rng.uniform(0.5, 4.99)), "Digital goods"
            status = "failed" if rng.random() < 0.85 else "success"
        else:
            hv = rec["channel"] == "Card" and rng.random() < HV_SHARE
            if hv:
                amount = float(rng.uniform(500, 2500)); cat = CATS_HV[int(rng.integers(2))]
            else:
                amount = float(math.exp(rng.normal(math.log(25 if rec["channel"] == "UPI" else 45), 0.8)))
                cat = (CATS_UPI if rec["channel"] == "UPI" else CATS_CARD)[int(rng.integers(5))]
            if hv and hv_fail > 0 and rng.random() < hv_fail:
                status, lat = "timeout", lat * 3
            else:
                base_fail = (1 - succ) if not hv_fail else (1 - succ - HV_SHARE * hv_fail)
                status = "failed" if rng.random() < max(0.0, base_fail) else "success"
        out.append({"ts": rec["ts"], "bank": rec["bank"], "channel": rec["channel"], "platform": rec["platform"],
                    "region": rec["region"], "app_version": rec["app_version"], "amount": round(amount, 2),
                    "merchant_category": cat, "latency_ms": round(lat, 1), "status": status})
    return out

# ============================================================================== the 10-day storyline
def iso(*a):
    return D(2025, 2, *a).isoformat()

def story_faults():
    """What happens over the 10 days. Lumio is never told any of this except what a real team would see
    (deploy feed, change calendar, weather feed)."""
    return [
        # ---- Fri 7 Feb (business day 1)
        {"id": "cardnet", "kind": "service", "service": "card-network", "start": iso(7, 10, 20), "end": iso(7, 11, 0),
         "ramp_min": 5, "service_x": 3.0, "error_rate": 0.065},
        {"id": "gap-z-west", "kind": "dq_gap", "scope": {"bank": "Bank Z", "region": "West"}, "start": iso(7, 14, 0), "end": iso(7, 14, 50)},
        {"id": "rel-x52", "kind": "release", "bank": "Bank X", "platform": "Android", "version": "5.2", "start": iso(7, 18, 58),
         "to_share": 0.4, "ramp_min": 30, "rollback_at": iso(7, 23, 0), "effect_scope": {"channel": "UPI"},
         "latency_x": 3.2, "success_drop": 0.055, "note": "Bank X mobile app v5.2 — staged rollout to 40% of Android users"},
        # ---- Sat 8 Feb
        {"id": "mnt-w", "kind": "maintenance", "service": config.CORE["Bank W"], "scope": {"bank": "Bank W"}, "start": iso(8, 2, 0),
         "end": iso(8, 4, 30), "ramp_min": 5, "service_x": 3.0, "error_rate": 0.03, "change": "CHG-2291",
         "note": "Bank W core banking upgrade"},
        {"id": "attack-y", "kind": "segment", "scope": {"bank": "Bank Y", "channel": "Card"}, "start": iso(8, 15, 30), "end": iso(8, 16, 30),
         "ramp_min": 5, "attack": True, "volume_x": 3.5},
        # ---- Sun 9 → Mon 10
        {"id": "drift-upi", "kind": "service", "service": "upi-switch", "start": iso(9, 21, 0), "end": iso(10, 3, 0),
         "shape": "drift", "service_x": 2.8, "error_rate": 0.004},
        # ---- Mon 10 Feb (business day 2)
        {"id": "rel-z39", "kind": "release", "bank": "Bank Z", "platform": "iOS", "version": "3.9", "start": iso(10, 10, 55),
         "to_share": 0.5, "ramp_min": 10, "rollback_at": iso(10, 12, 40), "effect_scope": {},
         "volume_x": 0.1, "note": "Bank Z mobile app v3.9 — rollout to 50% of iOS users"},
        # ---- Tue 11 Feb (business day 3) — blizzard comes from NAB data
        {"id": "west", "kind": "segment", "scope": {"region": "West"}, "start": iso(11, 15, 0), "end": iso(11, 16, 0),
         "ramp_min": 5, "latency_x": 1.8, "success_drop": 0.08},
        # ---- Wed 12 Feb (business day 4) — fraud-check DB level shift comes from NAB data
        {"id": "dup-w-north", "kind": "dq_dup", "scope": {"bank": "Bank W", "region": "North"}, "start": iso(12, 9, 30), "end": iso(12, 9, 50)},
        {"id": "hv-y", "kind": "segment", "scope": {"bank": "Bank Y", "channel": "Card"}, "start": iso(12, 20, 0), "end": iso(12, 22, 30),
         "ramp_min": 10, "high_value_only": True, "hv_fail": 0.6, "latency_x": 1.8},
        # ---- Thu 13 Feb (business day 5) — Bank W core banking failure comes from NAB data
        {"id": "upi-out", "kind": "service", "service": "upi-switch", "start": iso(13, 8, 15), "end": iso(13, 9, 0),
         "ramp_min": 5, "service_x": 2.5, "error_rate": 0.075},
        # ---- Fri 14 Feb (business day 6) — payday + Valentine's
        {"id": "cb-x", "kind": "service", "service": config.CORE["Bank X"], "start": iso(14, 19, 30), "end": iso(14, 21, 0),
         "ramp_min": 10, "service_x": 2.8, "error_rate": 0.012},
        # ---- Sat 15 / Sun 16
        {"id": "gap-y", "kind": "dq_gap", "scope": {"bank": "Bank Y"}, "start": iso(15, 11, 0), "end": iso(15, 11, 40)},
        {"id": "mnt-x", "kind": "maintenance", "service": config.CORE["Bank X"], "scope": {"bank": "Bank X"}, "start": iso(16, 3, 0),
         "end": iso(16, 5, 0), "ramp_min": 5, "service_x": 3.0, "error_rate": 0.03, "change": "CHG-2307",
         "note": "Bank X core banking patching"},
    ]

def story_truth():
    """The answer key used by the Proof tab (hidden from the detector)."""
    T = lambda *a: D(2025, 2, *a)
    return [
        dict(name="Card network partial outage", kind="incident", start_ts=T(7, 10, 20), end_ts=T(7, 11, 0), scope={"channel": "Card"},
             cause={"type": "dependency", "detail": "card-network"}, day=1),
        dict(name="Collector stops sending: Bank Z West", kind="data_quality", start_ts=T(7, 14, 0), end_ts=T(7, 14, 50),
             scope={"bank": "Bank Z", "region": "West"}, cause={"type": "data_quality", "detail": "missing data"}, day=1),
        dict(name="Bank X Android v5.2 slows UPI payments", kind="incident", start_ts=T(7, 19, 0), end_ts=T(7, 23, 15),
             scope={"bank": "Bank X", "channel": "UPI", "platform": "Android"}, cause={"type": "release", "detail": "5.2"}, day=1),
        dict(name="Planned maintenance: Bank W core banking", kind="not_incident", start_ts=T(8, 2, 0), end_ts=T(8, 4, 30),
             scope={"bank": "Bank W"}, cause={"type": "maintenance", "detail": "CHG-2291"}, day=2),
        dict(name="Card-testing bot attack on Bank Y cards", kind="incident", start_ts=T(8, 15, 30), end_ts=T(8, 16, 30),
             scope={"bank": "Bank Y", "channel": "Card"}, cause={"type": "fraud", "detail": "card testing"}, day=2),
        dict(name="UPI switch memory leak (slow drift)", kind="incident", start_ts=T(9, 21, 0), end_ts=T(10, 3, 0),
             scope={"channel": "UPI"}, cause={"type": "dependency", "detail": "upi-switch"}, day=3),
        dict(name="Bank Z iOS v3.9 login bug — payments vanish", kind="incident", start_ts=T(10, 11, 0), end_ts=T(10, 12, 45),
             scope={"bank": "Bank Z", "platform": "iOS"}, cause={"type": "release", "detail": "3.9"}, day=4),
        dict(name="Winter storm (real NAB blizzard): volume collapses everywhere", kind="advisory", start_ts=T(10, 14, 30), end_ts=T(11, 23, 0),
             scope={}, cause={"type": "system_wide", "detail": "volume"}, day=4, source="NAB nyc_taxi"),
        dict(name="One-off core-banking spike at Bank W (real NAB)", kind="transient", start_ts=T(11, 9, 35), end_ts=T(11, 9, 45),
             scope={"bank": "Bank W"}, cause={"type": "transient", "detail": "spike"}, day=5, source="NAB ec2_request_latency"),
        dict(name="Regional internet outage: West", kind="incident", start_ts=T(11, 15, 0), end_ts=T(11, 16, 0), scope={"region": "West"},
             cause={"type": "regional", "detail": "West"}, day=5),
        dict(name="Duplicate records: Bank W North", kind="data_quality", start_ts=T(12, 9, 30), end_ts=T(12, 9, 50),
             scope={"bank": "Bank W", "region": "North"}, cause={"type": "data_quality", "detail": "duplicates"}, day=6),
        dict(name="Fraud-check database saturates (real NAB level shift)", kind="incident", start_ts=T(12, 13, 0), end_ts=T(12, 14, 30),
             scope={"channel": "Card"}, cause={"type": "dependency", "detail": "fraud-check"}, day=6, source="NAB rds_cpu_utilization"),
        dict(name="High-value card payments time out at Bank Y", kind="incident", start_ts=T(12, 20, 0), end_ts=T(12, 22, 30),
             scope={"bank": "Bank Y", "channel": "Card"}, cause={"type": "novel", "detail": "amount_band:> $500"}, day=6),
        dict(name="UPI switch partial outage", kind="incident", start_ts=T(13, 8, 15), end_ts=T(13, 9, 0), scope={"channel": "UPI"},
             cause={"type": "dependency", "detail": "upi-switch"}, day=7),
        dict(name="Bank W core banking failure (real NAB system failure)", kind="incident", start_ts=T(13, 14, 0), end_ts=T(13, 15, 10),
             scope={"bank": "Bank W"}, cause={"type": "dependency", "detail": config.CORE["Bank W"]}, day=7, source="NAB ec2_request_latency"),
        dict(name="Payday + Valentine's Day rush", kind="not_incident", start_ts=T(14, 16, 30), end_ts=T(14, 22, 30), scope={},
             cause={"type": "expected", "detail": "payday"}, day=8),
        dict(name="Bank X core banking saturates during the payday peak", kind="incident", start_ts=T(14, 19, 30), end_ts=T(14, 21, 0),
             scope={"bank": "Bank X"}, cause={"type": "dependency", "detail": config.CORE["Bank X"]}, day=8),
        dict(name="Collector stops sending: Bank Y (all segments)", kind="data_quality", start_ts=T(15, 11, 0), end_ts=T(15, 11, 40),
             scope={"bank": "Bank Y"}, cause={"type": "data_quality", "detail": "missing data"}, day=9),
        dict(name="Planned maintenance: Bank X core banking", kind="not_incident", start_ts=T(16, 3, 0), end_ts=T(16, 5, 0),
             scope={"bank": "Bank X"}, cause={"type": "maintenance", "detail": "CHG-2307"}, day=10),
    ]

def feed_events():
    """Things a real team would see in their own feeds (deploy pipeline, change calendar, weather, business calendar)."""
    T = lambda *a: D(2025, 2, *a)
    return [
        (T(7, 18, 58), "deploy", "Deploy feed: Bank X Android app v5.2 staged rollout started (target 40% of users)", {}),
        (T(8, 1, 45), "change", "Change calendar: planned maintenance CHG-2291 — Bank W core banking upgrade, 02:00–04:30", {}),
        (T(10, 10, 55), "deploy", "Deploy feed: Bank Z iOS app v3.9 rollout started (target 50% of users)", {}),
        (T(10, 16, 0), "weather", "Weather feed: winter storm warning for the region, Mon 17:00 until Tue evening", {"until": T(11, 22, 0).isoformat()}),
        (T(14, 8, 0), "calendar", "Business calendar: payday (the 15th falls on Saturday) and Valentine's Day — evening rush expected", {}),
        (T(16, 2, 45), "change", "Change calendar: planned maintenance CHG-2307 — Bank X core banking patching, 03:00–05:00", {}),
    ]

def maintenance_windows():
    return [{"start": _t(f["start"]), "end": _t(f["end"]), "scope": f["scope"], "change": f["change"], "note": f["note"]}
            for f in story_faults() if f["kind"] == "maintenance"]

def custom_fault(effect, scope, now):
    """A fault injected live from the dashboard."""
    start = now + timedelta(minutes=5)
    base = {"id": f"custom-{int(now.timestamp())}", "start": start.isoformat(), "end": (start + timedelta(minutes=75)).isoformat(),
            "scope": scope, "ramp_min": 5, "custom": True}
    label = " · ".join(scope.values())
    if effect == "slowdown":
        base.update(kind="segment", name=f"Injected slowdown: {label}", latency_x=2.6, success_drop=0.01)
    elif effect == "failures":
        base.update(kind="segment", name=f"Injected failures: {label}", success_drop=0.07)
    elif effect == "drop":
        base.update(kind="segment", name=f"Injected volume drop: {label}", volume_x=0.25)
    else:
        base.update(kind="dq_gap", name=f"Injected data gap: {label}")
    base["cause"] = {"type": "data_quality" if base["kind"] == "dq_gap" else "unexplained", "detail": effect}
    return base
