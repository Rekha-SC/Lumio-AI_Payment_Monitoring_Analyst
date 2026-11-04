"""Investigation tools. Each one runs a real query against the database and returns numbers.

The scripted investigator and the AI agent call exactly the same tools — the agent can choose which tool
to call next, but it never produces a number itself.
"""
import json
from datetime import timedelta
import numpy as np, pandas as pd
from sqlalchemy import select, and_
from . import config, db
from .model import mix_p95

DIM_LABEL = {"bank": "bank", "channel": "channel", "platform": "platform", "region": "region"}

def scope_label(scope):
    return " · ".join(scope[d] for d in config.DIMS if d in scope) or "All payments"

def segs_matching(scope, all_segs):
    return [s for s in all_segs if all(db.seg_parts(s)[k] == v for k, v in scope.items())]

def _window_scores(conn, since, until):
    q = select(db.window_scores).where(and_(db.window_scores.c.ts >= since, db.window_scores.c.ts < until))
    return pd.read_sql(q, conn)

def _metrics(conn, since, until, scope=None):
    q = select(db.metrics).where(and_(db.metrics.c.ts >= since, db.metrics.c.ts < until))
    for k, v in (scope or {}).items():
        q = q.where(getattr(db.metrics.c, k) == v)
    df = pd.read_sql(q, conn)
    return df.drop_duplicates(subset=["ts", "bank", "channel", "platform", "region", "app_version"])

def _agg(df):
    if not len(df):
        return None
    vol = df["volume"].sum()
    return {"volume": int(vol), "success": float(df["success_count"].sum() / max(vol, 1)),
            "p95_ms": float(mix_p95(df["p95_ms"].values[None, :], df["volume"].values[None, :])[0]),
            "avg_amount": float(np.average(df["avg_amount"], weights=df["volume"]))}

# --------------------------------------------------------------------------------------------- tools
def where_and_peers(conn, scope, segs, now):
    """How the affected segments compare with what is normal, and with their peers."""
    ws = _window_scores(conn, now - timedelta(minutes=config.SCAN_MIN), now)
    cur = ws[ws.seg.isin(segs)]
    out = {"segments": segs, "now": {
        "p95_ms": float(mix_p95(cur.p95_ms.values[None], cur.volume.values[None])[0]) if len(cur) else None,
        "exp_p95_ms": float(mix_p95(cur.exp_p95_ms.values[None], cur.volume.values[None])[0]) if len(cur) else None,
        "success": float(np.average(cur.success, weights=cur.volume)) if len(cur) else None,
        "exp_success": float(np.average(cur.exp_success, weights=cur.volume)) if len(cur) else None,
        "volume": int(cur.volume.sum()) if len(cur) else 0, "exp_volume": float(cur.exp_volume.sum()) if len(cur) else 0.0}}
    peers = []
    for d in [d for d in config.DIMS if d in scope]:
        sib_scope = {k: v for k, v in scope.items() if k != d}
        sib = ws[ws.seg.isin(segs_matching(sib_scope, ws.seg.tolist())) & ~ws.seg.isin(segs)]
        if len(sib):
            r = float(np.median(sib.p95_ms / sib.exp_p95_ms)); sr = float(np.median(sib.success - sib.exp_success))
            peers.append({"dimension": d, "others": sorted(set(db.seg_parts(s)[d] for s in sib.seg)),
                          "latency_vs_normal": round(r, 2), "success_gap_pts": round(sr * 100, 2),
                          "normal": bool(r < 1.25 and sr > -0.01)})
    out["peers"] = peers
    n = out["now"]
    lat_x = (n["p95_ms"] / n["exp_p95_ms"]) if n["p95_ms"] else None
    normal_peers = [p for p in peers if p["normal"]]
    vx = n["volume"] / max(1.0, n["exp_volume"])
    finding = (f"{scope_label(scope)}: payment time {n['p95_ms']/1000:.1f} s vs {n['exp_p95_ms']/1000:.1f} s normal "
               f"({lat_x:.1f}×), success {n['success']*100:.1f}% vs {n['exp_success']*100:.1f}%, "
               f"volume {n['volume']:,} vs {n['exp_volume']:,.0f} expected ({vx*100-100:+.0f}%). ")
    n["vol_x"] = round(vx, 2); n["lat_x"] = round(lat_x, 2)
    if normal_peers:
        finding += "Peers are normal: " + "; ".join(f"other {p['dimension']}s ({', '.join(p['others'][:3])}) at {p['latency_vs_normal']}×" for p in normal_peers) + "."
    return finding, out

def change_point(conn, segs, now, metric="performance"):
    """When did it start? Walks back through 5-minute buckets until the deviation disappears."""
    q = select(db.bucket_scores).where(and_(db.bucket_scores.c.ts >= now - timedelta(hours=4), db.bucket_scores.c.ts < now,
                                            db.bucket_scores.c.seg.in_(segs)))
    bs = pd.read_sql(q, conn)
    if not len(bs):
        return "No bucket history.", {"start": now.isoformat()}
    g = bs.groupby("ts").apply(lambda d: pd.Series({
        "ratio": mix_p95(d.p95_ms.values[None], d.volume.values[None])[0] / mix_p95(d.exp_p95_ms.values[None], d.volume.values[None])[0],
        "sgap": np.average(d.success - d.exp_success, weights=d.volume),
        "vratio": d.volume.sum() / max(1.0, d.exp_volume.sum())}), include_groups=False).sort_index()
    cur_r = g.ratio.iloc[-3:].median(); cur_s = g.sgap.iloc[-3:].median(); cur_v = g.vratio.iloc[-3:].median()
    thr_r = 1 + 0.35 * max(0.0, cur_r - 1); thr_s = 0.35 * min(0.0, cur_s)
    dev = (g.ratio > thr_r) | ((g.sgap < thr_s) & (cur_s < -0.005))
    if "volume" in metric and abs(np.log(max(cur_v, 0.01))) > 0.2:
        lv = np.log(g.vratio.clip(lower=0.01)); dev = dev | (np.sign(lv) == np.sign(np.log(cur_v))) & (lv.abs() > 0.35 * abs(np.log(cur_v)))
    start = g.index[-1]
    for ts in reversed(g.index):
        if dev.loc[ts]:
            start = ts
        else:
            break
    start = pd.Timestamp(start).to_pydatetime()
    return f"Deviation begins in the {start:%H:%M} bucket and has persisted since.", {"start": start.isoformat(),
            "series": [{"ts": t.strftime("%H:%M"), "ratio": round(float(r), 2)} for t, r in g.ratio.items()][-24:]}

def recent_releases(conn, scope, start):
    q = select(db.releases).where(and_(db.releases.c.ts >= start - timedelta(hours=2), db.releases.c.ts <= start + timedelta(minutes=15)))
    rel = pd.read_sql(q, conn)
    if "bank" in scope: rel = rel[rel.bank == scope["bank"]]
    if "platform" in scope: rel = rel[rel.platform == scope["platform"]]
    items = [{"ts": r.ts.strftime("%H:%M"), "bank": r.bank, "platform": r.platform, "component": r.component,
              "version": r.version, "minutes_before_start": round((start - r.ts.to_pydatetime()).total_seconds() / 60)} for r in rel.itertuples()]
    if not items:
        return "No releases touched this scope in the two hours before it started.", {"releases": []}
    i = items[0]
    return (f"{i['bank']} {i['platform']} {i['component']} v{i['version']} started rolling out at {i['ts']}, "
            f"{i['minutes_before_start']} min before the deviation."), {"releases": items}

def version_split(conn, scope, start, now, new_version=None, metric="performance"):
    df = _metrics(conn, start, now, scope)
    rows = []
    for v, d in df.groupby("app_version"):
        a = _agg(d); a["version"] = v; rows.append(a)
    rows.sort(key=lambda r: -r["p95_ms"])
    if len(rows) < 2:
        return "Only one app version in this scope — version is not the differentiator.", {"versions": rows, "culprit": None}
    if metric == "volume" and new_version:
        new = next((r for r in rows if r["version"] == new_version), None)
        old = max((r for r in rows if r["version"] != new_version), key=lambda r: r["volume"])
        if new:
            ratio = new["volume"] / max(1, old["volume"])
            culprit = new_version if ratio < 0.35 else None
            txt = (f"Since the release, v{new_version} users made {new['volume']:,} payments vs {old['volume']:,} on v{old['version']} "
                   f"({ratio*100:.0f}% as many, although both versions have similar numbers of users). ")
            txt += f"Payments from v{new_version} have almost stopped." if culprit else "Volumes look proportional."
            return txt, {"versions": rows, "culprit": culprit, "vol_ratio": round(ratio, 2), "other": old["version"], "lat_x": None, "success_gap_pts": None}
    worst, rest = rows[0], rows[1:]
    best = min(rest, key=lambda r: r["p95_ms"])
    lat_x = worst["p95_ms"] / best["p95_ms"]; sgap = (best["success"] - worst["success"]) * 100
    culprit = worst["version"] if (lat_x > 1.6 or sgap > 2.0) else None
    txt = "; ".join(f"v{r['version']}: {r['p95_ms']/1000:.1f} s, {r['success']*100:.1f}% success ({r['volume']:,} payments)" for r in rows)
    return (f"{txt}. " + (f"Only v{culprit} is affected ({lat_x:.1f}× slower)." if culprit else "Versions behave alike.")), \
        {"versions": rows, "culprit": culprit, "lat_x": round(lat_x, 2), "success_gap_pts": round(sgap, 2)}

def dependencies(conn, segs, start, now, observed_x=1.0):
    """Every upstream service these payments call (channel services + each bank's core banking), now vs the same
    time on the previous 7 days. A service is the culprit when it is slow or erroring enough to explain the payments."""
    parts = [db.seg_parts(s) for s in segs]
    svcs = sorted({x for p in parts for x in config.services_for(p["channel"], p["bank"])})
    cur = pd.read_sql(select(db.service_metrics).where(and_(db.service_metrics.c.ts >= max(start, now - timedelta(minutes=30)),
                                                            db.service_metrics.c.ts < now, db.service_metrics.c.service.in_(svcs))), conn)
    out = []
    for s in svcs:
        c = cur[cur.service == s]
        if not len(c):
            continue
        base_p, base_e = [], []
        for d in range(1, 8):
            a = c.ts.min() - timedelta(days=d); b = c.ts.max() + timedelta(minutes=5) - timedelta(days=d)
            q = select(db.service_metrics.c.p95_ms, db.service_metrics.c.error_rate).where(and_(
                db.service_metrics.c.service == s, db.service_metrics.c.ts >= a, db.service_metrics.c.ts < b))
            for p95, e in conn.execute(q):
                base_p.append(p95); base_e.append(e)
        if not base_p:
            continue
        np_, ne = float(np.median(base_p)), float(np.median(base_e))
        ratio = float(c.p95_ms.median()) / np_
        err = float(c.error_rate.median())
        explained = 0.8 * max(0.0, ratio - 1) / max(0.05, observed_x - 1)
        hot = (ratio >= 1.25 and explained >= 0.5) or (err > 0.01 and err / max(ne, 1e-4) > 5)
        out.append({"service": s, "p95_ms": round(float(c.p95_ms.median()), 1), "normal_ms": round(np_, 1), "ratio": round(ratio, 2),
                    "error_pct": round(err * 100, 2), "normal_error_pct": round(ne * 100, 2), "hot": bool(hot),
                    "score": round(max(ratio - 1, 0) + 20 * max(0.0, err - ne), 3)})
    out.sort(key=lambda r: -r["score"])
    hot = [r for r in out if r["hot"]]
    if hot:
        h = hot[0]
        txt = f"{h['service']} is at {h['p95_ms']:.0f} ms vs {h['normal_ms']:.0f} ms normal ({h['ratio']}×)"
        if h["error_pct"] > 1:
            txt += f", errors {h['error_pct']}% vs {h['normal_error_pct']}%"
        return txt + f". The other {len(out) - 1} dependencies are normal.", {"services": out, "culprit": h["service"]}
    return f"All {len(out)} upstream dependencies are within their normal range.", {"services": out, "culprit": None}

def query_samples(conn, scope, start, now, group_by, measure="failures"):
    """Transaction-level samples (the raw evidence) grouped by any field. Used for causes outside the pre-built slices.
    measure='failures' compares failure rates; measure='latency' compares payment times. Each group gets a significance test,
    so a handful of unlucky samples can never be reported as a cause."""
    from scipy.stats import fisher_exact, mannwhitneyu
    q = select(db.txn_samples).where(and_(db.txn_samples.c.ts >= start, db.txn_samples.c.ts < now))
    for k, v in scope.items():
        if k in DIM_LABEL:
            q = q.where(getattr(db.txn_samples.c, k) == v)
    df = pd.read_sql(q, conn)
    if not len(df):
        return "No samples.", {"groups": [], "n": 0, "group_by": group_by, "measure": measure}
    df["bad"] = df.status != "success"
    if group_by == "amount_band":
        df["grp"] = pd.cut(df.amount, [0, 5, 50, 200, 500, 1e9], labels=["< $5", "$5–50", "$50–200", "$200–500", "> $500"]).astype(str)
    else:
        df["grp"] = df[group_by].astype(str)
    total_bad = df.bad.sum()
    groups = []
    for g, d in df.groupby("grp"):
        rest = df[df.grp != g]
        fr = d.bad.mean(); rr = rest.bad.mean() if len(rest) else 0
        bad_g, bad_r = int(d.bad.sum()), int(rest.bad.sum())
        p_fail = float(fisher_exact([[bad_g, len(d) - bad_g], [bad_r, len(rest) - bad_r]], alternative="greater")[1]) if len(rest) else 1.0
        lr = float(d.latency_ms.median() / rest.latency_ms.median()) if len(rest) else 1.0
        p_lat = float(mannwhitneyu(d.latency_ms, rest.latency_ms, alternative="greater").pvalue) if len(rest) and len(d) > 2 else 1.0
        groups.append({"group": g, "samples": int(len(d)), "share_of_samples": round(len(d) / len(df), 2),
                       "failures": bad_g, "failure_rate": round(float(fr), 3), "rest_failure_rate": round(float(rr), 3),
                       "lift": round(float(fr / max(rr, 0.002)), 1), "share_of_failures": round(float(bad_g / max(total_bad, 1)), 2),
                       "p_fail": p_fail, "lat_ratio": round(lr, 2), "p_lat": p_lat,
                       "timeouts": int((d.status == "timeout").sum()), "avg_amount": round(float(d.amount.mean()), 2)})
    groups.sort(key=lambda r: -(r["lift"] if measure == "failures" else r["lat_ratio"]))
    top = groups[0]
    ok = explains(top, measure)
    if measure == "failures":
        txt = (f"By {group_by}: '{top['group']}' fails {top['failure_rate']*100:.0f}% of the time vs {top['rest_failure_rate']*100:.1f}% "
               f"for the rest ({top['lift']}×, {top['failures']} failures in {top['samples']} samples)")
    else:
        txt = (f"By {group_by}: '{top['group']}' payments take {top['lat_ratio']}× as long as the rest "
               f"({top['samples']} samples, {top['share_of_samples']*100:.0f}% of the scope)")
    txt += " — statistically significant, explains it." if ok else " — not strong or significant enough to be the cause."
    return txt, {"group_by": group_by, "measure": measure, "groups": groups, "n": int(len(df))}

def explains(g, measure):
    """The bar a sample group must clear before it can be called a cause."""
    if measure == "failures":
        return g["lift"] >= 3 and g["failures"] >= 8 and g["share_of_failures"] >= 0.4 and g["p_fail"] < 1e-4
    return g["lat_ratio"] >= 1.8 and g["samples"] >= 20 and g["share_of_samples"] <= 0.6 and g["p_lat"] < 1e-4

def impact(conn, scope, segs, start, now, slice_filter=None, slice_share=None, slice_avg=None):
    """Payments affected, extra failures and value at risk — computed, never estimated by the AI."""
    df = _metrics(conn, start, now, scope)
    sl = df
    if slice_filter:
        for k, v in slice_filter.items():
            sl = sl[sl[k] == v]
    a_all = _agg(df) or {"volume": 0, "avg_amount": 0, "success": 0, "p95_ms": 0}
    a = _agg(sl) or a_all
    affected = a["volume"] * (slice_share if slice_share else 1)
    if slice_avg:
        a = dict(a, avg_amount=slice_avg)
    ws = _window_scores(conn, start, now)
    ws = ws[ws.seg.isin(segs)]
    extra_fail = float((ws.volume * (ws.exp_success - ws.success).clip(lower=0)).sum())
    cur = _metrics(conn, now - timedelta(minutes=config.SCAN_MIN), now, scope)
    if slice_filter:
        for k, v in slice_filter.items():
            cur = cur[cur[k] == v]
    c = _agg(cur) or a
    lw = ws[ws.ts == ws.ts.max()] if len(ws) else ws
    normal_p95 = float(mix_p95(lw.exp_p95_ms.values[None], lw.volume.values[None])[0]) if len(lw) else None
    normal_succ = float(np.average(lw.exp_success, weights=lw.volume)) if len(lw) else None
    missing = float((ws.exp_volume - ws.volume).clip(lower=0).sum()) if "exp_volume" in ws else 0.0
    surplus = float((ws.volume - ws.exp_volume).clip(lower=0).sum()) if "exp_volume" in ws else 0.0
    vol_now = float(lw.volume.sum()) / config.SCAN_MIN if len(lw) else 0.0
    vol_norm = float(lw.exp_volume.sum()) / config.SCAN_MIN if len(lw) else 0.0
    out = {"affected": int(round(affected)), "extra_failures": int(round(extra_fail)), "avg_amount": round(a["avg_amount"], 2),
           "missing": int(round(missing)), "surplus": int(round(surplus)), "per_min": round(vol_now, 1), "normal_per_min": round(vol_norm, 1),
           "value_at_risk": round(affected * a["avg_amount"], 0), "p95_ms": round(c["p95_ms"], 0), "success": round(c["success"], 4),
           "normal_p95_ms": round(normal_p95, 0) if normal_p95 else None, "normal_success": round(normal_succ, 4) if normal_succ else None}
    return (f"{out['affected']:,} payments in the affected slice since {start:%H:%M}; about {out['extra_failures']:,} extra failures; "
            f"value at risk ${out['value_at_risk']:,.0f} at ${out['avg_amount']:.0f} average."), out
