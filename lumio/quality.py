"""Data-quality gate: decides, per segment and window, whether the data can be trusted before anything is scored.

Missing data is not a slowdown, and duplicated data is not a surge. Holding bad data back is what keeps
a broken collector from paging someone at midnight.
"""
from datetime import timedelta
import pandas as pd
from . import config, db

EXPECTED_BUCKETS = config.SCAN_MIN // config.BUCKET_MIN

def check(df, window_end, all_segs):
    """df: raw rows for the window. Returns (clean_df, {seg: result})."""
    out = {}
    stats = {}
    if len(df):
        df = df.copy()
        df["seg"] = df["bank"] + "|" + df["channel"] + "|" + df["platform"] + "|" + df["region"]
        dup = df.duplicated(subset=["seg", "ts", "app_version"])
        g = df.assign(dup=dup).groupby("seg")
        stats = pd.DataFrame({"buckets": g["ts"].nunique(), "dups": g["dup"].sum(), "last": g["ts"].max()}).to_dict("index")
    for s in all_segs:
        st = stats.get(s)
        buckets = int(st["buckets"]) if st else 0
        dups = int(st["dups"]) if st else 0
        last = pd.Timestamp(st["last"]).to_pydatetime() if st else None
        stale = ((window_end - timedelta(minutes=config.BUCKET_MIN)) - last).total_seconds() / 60 if last is not None else float(config.SCAN_MIN)
        comp = buckets / EXPECTED_BUCKETS
        if comp < config.DQ_MIN_COMPLETENESS:
            status, note = "hold", f"only {buckets} of {EXPECTED_BUCKETS} buckets arrived — held, not scored"
        elif dups:
            status, note = "dedup", f"{dups} duplicate records removed before scoring"
        else:
            status, note = "ok", ""
        out[s] = {"completeness": comp, "duplicates": dups, "stale_min": max(0.0, stale), "status": status, "note": note}
    clean = df.drop_duplicates(subset=["ts", "bank", "channel", "platform", "region", "app_version"]) if len(df) else df
    return clean, out
