"""Real data from the Numenta Anomaly Benchmark (NAB, MIT licence), re-timed onto the demo calendar.

  nyc_taxi.csv                         -> the rhythm of payment volume (real human demand: daily, weekly, and the
                                          labelled January 2015 blizzard, which lands on Mon 10 – Tue 11 Feb)
  ec2_request_latency_system_failure   -> Bank W's core-banking service (texture, a labelled one-off spike on
                                          Tue 11 Feb 09:40, and the labelled system failure on Thu 13 Feb 14:00)
  rds_cpu_utilization_cc0c53           -> the fraud-check database (texture, and its labelled level shift,
                                          replayed Wed 12 Feb 13:00–14:30)

Everything is deterministic: the same timestamps always give the same values.
"""
import os, json
from datetime import datetime, timedelta
from functools import lru_cache
import numpy as np, pandas as pd
from . import config

NAB_DIR = os.path.join(config.DATA_DIR, "nab")
FIVE = timedelta(minutes=5)

def _ns(x):
    """Nanosecond integers for a timestamp or index, whatever unit pandas chose."""
    if isinstance(x, pd.DatetimeIndex):
        return x.as_unit("ns").asi8
    return pd.Timestamp(x).as_unit("ns").value

def _read(name):
    return pd.read_csv(os.path.join(NAB_DIR, name), parse_dates=["timestamp"]).set_index("timestamp")["value"]

# ---------------------------------------------------------------------------------------- payment volume (taxi)
TAXI_ANCHOR_DEMO = datetime(2025, 2, 10)      # Monday
TAXI_ANCHOR_NAB = datetime(2015, 1, 26)       # Monday — the blizzard
TAXI_END = datetime(2015, 1, 31, 23, 30)

@lru_cache(maxsize=1)
def _volume_table():
    taxi = _read("nyc_taxi.csv")
    off = TAXI_ANCHOR_DEMO - TAXI_ANCHOR_NAB
    idx = pd.date_range(config.HISTORY_START, config.LIVE_END + timedelta(days=1), freq="5min")
    src = []
    for t in idx:
        s = t.to_pydatetime() - off
        while s > TAXI_END:
            s -= timedelta(days=7)            # Sun 16 Feb has no taxi day: reuse the previous Sunday
        src.append(s)
    src = pd.DatetimeIndex(src)
    raw = np.interp(_ns(src), _ns(taxi.index), taxi.values)
    # 'typical' week: median over the clean weeks of January 2015 at the same weekday and time of day
    clean = taxi["2015-01-03":"2015-01-24"]
    key = clean.index.weekday * 48 + clean.index.hour * 2 + clean.index.minute // 30
    typ_tab = clean.groupby(key).median()
    k = src.weekday * 48 + src.hour * 2 + src.minute // 30
    typ = typ_tab.reindex(k).values
    typ = np.interp(np.arange(len(typ)), np.arange(len(typ))[~np.isnan(typ)], typ[~np.isnan(typ)])
    # payments react to the city less violently than taxis do: keep the shape, damp the size of deviations
    vol = typ * np.power(np.clip(raw, 1, None) / np.clip(typ, 1, None), 0.5)
    day = (src.hour >= 10) & (src.hour < 20)
    scale = np.median(typ[day])
    mult = np.clip(vol / scale, 0.04, None)
    smooth = pd.Series(mult, index=idx).rolling(3, center=True, min_periods=1).mean()
    return smooth

def volume_mult(ts):
    return float(_volume_table().get(pd.Timestamp(ts), 1.0))

# ---------------------------------------------------------------------------------------- Bank W core banking
CB_FAIL_DEMO = datetime(2025, 2, 13, 14, 0)
CB_FAIL_NAB = datetime(2014, 3, 21, 3, 1)
CB_FAIL_END_DEMO = datetime(2025, 2, 13, 15, 10)
CB_SPIKE_DEMO = datetime(2025, 2, 11, 9, 40)      # NAB label 2014-03-18 22:41, re-timed

@lru_cache(maxsize=1)
def _cb():
    s = _read("ec2_request_latency_system_failure.csv")
    return s, float(s[:CB_FAIL_NAB - timedelta(hours=1)].median())

def corebank_w(ts):
    """(latency factor, error rate) for Bank W core banking. History is synthetic; the evening replay is NAB."""
    if ts < config.LIVE_START:
        return None
    s, med = _cb()
    off = CB_FAIL_DEMO - CB_FAIL_NAB
    if CB_FAIL_DEMO <= ts < CB_FAIL_END_DEMO:
        regime = s[CB_FAIL_NAB:]                  # the real failure regime, cycled to last 70 minutes
        v = float(regime.iloc[int((ts - CB_FAIL_DEMO) / FIVE) % len(regime)])
    else:
        t = ts - off
        if ts >= CB_FAIL_END_DEMO:
            t -= timedelta(days=7)                # after the failure: continue with normal texture a week earlier
        v = float(np.interp(_ns(t), _ns(s.index), s.values))
    dev = v / med - 1
    labelled = (CB_FAIL_DEMO <= ts < CB_FAIL_END_DEMO) or abs((ts - CB_SPIKE_DEMO).total_seconds()) <= 900
    if not labelled:
        dev *= 0.5                                # everyday texture: keep the shape, soften the size
    lat = 1 + 2.5 * abs(dev)
    err = 0.002 + 0.12 * max(0.0, abs(dev) - 0.1)
    return lat, err

# ---------------------------------------------------------------------------------------- fraud-check DB
FC_SHIFT_NAB = datetime(2014, 2, 25, 7, 15)
FC_DEMO_START = datetime(2025, 2, 12, 13, 0)
FC_DEMO_END = datetime(2025, 2, 12, 14, 30)
FC_TEXTURE_NAB = datetime(2014, 2, 14, 14, 30)

@lru_cache(maxsize=1)
def _fc():
    s = _read("rds_cpu_utilization_cc0c53.csv")
    return s, float(s[:FC_SHIFT_NAB - timedelta(hours=2)].median())

def fraudcheck(ts):
    """Latency factor for the fraud-check service (CPU of its database, relative to normal)."""
    if ts < config.LIVE_START:
        return None
    s, med = _fc()
    if FC_DEMO_START <= ts < FC_DEMO_END:
        t = FC_SHIFT_NAB + (ts - FC_DEMO_START)
    else:
        t = FC_TEXTURE_NAB + (ts - config.LIVE_START)
    v = float(np.interp(_ns(t), _ns(s.index), s.values))
    f = max(0.7, v / med)
    return f if FC_DEMO_START <= ts < FC_DEMO_END else 1 + 0.5 * (f - 1)

def sources():
    lab = json.load(open(os.path.join(NAB_DIR, "nab_labels.json")))
    return [
        {"file": "realKnownCause/nyc_taxi.csv", "used_for": "Payment volume rhythm at every bank (all 31 days)",
         "real_anomaly": "January 2015 blizzard (labelled 2015-01-27 00:00)", "demo_time": "Mon 10 Feb evening – Tue 11 Feb",
         "labels": lab["nyc_taxi.csv"]["labels"]},
        {"file": "realKnownCause/ec2_request_latency_system_failure.csv", "used_for": "Bank W core-banking service health",
         "real_anomaly": "One-off spike (labelled 2014-03-18 22:41) and system failure (labelled 2014-03-21 03:01)",
         "demo_time": "Spike Tue 11 Feb 09:40 · failure Thu 13 Feb 14:00–15:10",
         "labels": lab["ec2_request_latency_system_failure.csv"]["labels"]},
        {"file": "realAWSCloudwatch/rds_cpu_utilization_cc0c53.csv", "used_for": "Fraud-check database load",
         "real_anomaly": "Sustained CPU level shift (labelled 2014-02-25 07:15)", "demo_time": "Wed 12 Feb 13:00–14:30",
         "labels": lab["rds_cpu_utilization_cc0c53.csv"]["labels"]},
    ]
