"""Daily and whole-period reports, computed from what Lumio has actually processed so far."""
import json
from datetime import datetime, timedelta, date as Date
import numpy as np, pandas as pd
from sqlalchemy import select, and_, func
from . import config, db, world
from .model import mix_p95

def days():
    out = []
    for i in range(config.DAYS):
        d = (config.LIVE_START + timedelta(days=i)).date()
        out.append({"date": d.isoformat(), "label": d.strftime("%a %d %b"), "business": d.weekday() < 5,
                    "payday": world.is_payday(datetime.combine(d, datetime.min.time())), "index": i + 1})
    bd = 0
    for o in out:
        if o["business"]:
            bd += 1; o["business_day"] = bd
    return out

def day_report(d):
    if isinstance(d, str):
        d = Date.fromisoformat(d)
    a = datetime.combine(d, datetime.min.time()); b = a + timedelta(days=1)
    st = db.state(); sim = st.get("sim_time") or config.LIVE_START
    with db.engine().connect() as c:
        bs = pd.read_sql(select(db.bucket_scores).where(and_(db.bucket_scores.c.ts >= a, db.bucket_scores.c.ts < min(b, sim))), c)
        incs = [dict(r) for r in c.execute(select(db.incidents).where(and_(db.incidents.c.opened_ts >= a, db.incidents.c.opened_ts < b))
                                           .order_by(db.incidents.c.id)).mappings()]
        naive = c.execute(select(func.sum(db.scans.c.naive_alerts)).where(and_(db.scans.c.ts > a, db.scans.c.ts <= b))).scalar() or 0
        mails = c.execute(select(func.count()).select_from(db.outbox).where(and_(db.outbox.c.ts >= a, db.outbox.c.ts < b))).scalar() or 0
    played = 0.0 if sim <= a else min(1.0, (sim - a).total_seconds() / 86400)
    out = {"date": d.isoformat(), "label": d.strftime("%A %d %B %Y"), "short": d.strftime("%a %d %b"), "business": d.weekday() < 5,
           "payday": world.is_payday(a), "played": round(played, 3), "naive_pages": int(naive), "emails": int(mails)}
    if len(bs):
        out.update(payments=int(bs.volume.sum()), success=round(float(np.average(bs.success, weights=bs.volume)) * 100, 2),
                   p95_s=round(float(mix_p95(bs.p95_ms.values[None], bs.volume.values[None])[0]) / 1000, 2))
        bs["hour"] = pd.to_datetime(bs.ts).dt.hour
        hourly = []
        for h, g in bs.groupby("hour"):
            hourly.append({"h": int(h), "payments": int(g.volume.sum()), "expected": round(float(g.exp_volume.sum())),
                           "success": round(float(np.average(g.success, weights=g.volume)) * 100, 2),
                           "p95": round(float(mix_p95(g.p95_ms.values[None], g.volume.values[None])[0]) / 1000, 2),
                           "exp_p95": round(float(mix_p95(g.exp_p95_ms.values[None], g.volume.values[None])[0]) / 1000, 2)})
        out["hourly"] = hourly
    else:
        out.update(payments=0, success=None, p95_s=None, hourly=[])
    rows = []
    for i in incs:
        a_ = json.loads(i["alert"] or "{}")
        mttd = round((i["opened_ts"] - i["started_ts"]).total_seconds() / 60) if i["started_ts"] else None
        dur = round(((i["resolved_ts"] or sim) - (i["started_ts"] or i["opened_ts"])).total_seconds() / 60)
        rows.append({"id": i["id"], "kind": i["kind"], "severity": i["severity"], "status": i["status"], "headline": i["headline"],
                     "opened": i["opened_ts"].strftime("%H:%M"), "started": i["started_ts"].strftime("%H:%M") if i["started_ts"] else None,
                     "resolved": i["resolved_ts"].strftime("%H:%M") if i["resolved_ts"] else None, "detect_min": mttd, "duration_min": dur,
                     "value_at_risk": a_.get("value_at_risk") or 0, "explained_by": i["explained_by"], "acked_by": i["acked_by"],
                     "teams": [t for t in (json.loads(i["routing"]).get("to") if i["routing"] else [])]})
    real = [r for r in rows if r["kind"] == "incident"]
    out["incidents"] = rows
    out["n_incidents"] = len(real)
    out["n_tickets"] = sum(r["kind"] == "data_quality" for r in rows)
    out["n_advisories"] = sum(r["kind"] == "advisory" for r in rows)
    out["value_at_risk"] = round(sum(r["value_at_risk"] for r in real))
    det = [r["detect_min"] for r in real if r["detect_min"] is not None]
    out["mttd"] = round(float(np.median(det))) if det else None
    return out

def summary():
    reps = [day_report(d["date"]) for d in days()]
    real = [r for rep in reps for r in rep["incidents"] if r["kind"] == "incident"]
    det = [r["detect_min"] for r in real if r["detect_min"] is not None]
    return {"days": [{k: v for k, v in r.items() if k not in ("hourly",)} for r in reps],
            "payments": sum(r["payments"] for r in reps), "incidents": len(real),
            "tickets": sum(r["n_tickets"] for r in reps), "advisories": sum(r["n_advisories"] for r in reps),
            "value_at_risk": sum(r["value_at_risk"] for r in reps), "naive_pages": sum(r["naive_pages"] for r in reps),
            "emails": sum(r["emails"] for r in reps), "mttd": round(float(np.median(det))) if det else None}

def day_csv(d):
    if isinstance(d, str):
        d = Date.fromisoformat(d)
    a = datetime.combine(d, datetime.min.time()); b = a + timedelta(days=1)
    with db.engine().connect() as c:
        df = pd.read_sql(select(db.window_scores).where(and_(db.window_scores.c.ts >= a, db.window_scores.c.ts < b)).order_by(db.window_scores.c.ts), c)
    parts = df.seg.str.split("|", expand=True) if len(df) else pd.DataFrame(columns=[0, 1, 2, 3])
    df["bank"], df["channel"], df["platform"], df["region"] = parts[0], parts[1], parts[2], parts[3]
    cols = ["ts", "bank", "channel", "platform", "region", "volume", "exp_volume", "success", "exp_success", "p95_ms", "exp_p95_ms",
            "z_lat", "z_succ", "z_vol", "flags", "status"]
    return df[cols].to_csv(index=False)
