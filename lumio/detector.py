"""Detector: every 15 minutes, gate the data, compare every segment with what is normal for right now,
group abnormal segments into one story, and alert only when the story survives the confirmation scans.

Three signals per segment: payment time, success rate and payment volume — plus business guardrails
(a floor on success rate and a ceiling on payment time) that always alert, whatever the model thinks."""
import json, math, time, secrets, logging
from datetime import timedelta, datetime
from itertools import combinations
import numpy as np, pandas as pd
from sqlalchemy import select, insert, update, and_
from . import config, db, quality, investigator, tools, world, settings
from .model import Baseline, aggregate, mix_p95

log = logging.getLogger("lumio.detector")
ALL_SEGS = [db.seg_key(b, c, p, r) for b in config.BANKS for c in config.CHANNELS for p in config.PLATFORMS for r in config.REGIONS]

def cover(abnormal, scored, min_precision=0.6):
    """Explain a set of abnormal segments with as few, as general filters as possible.
    e.g. 24 card segments -> {channel: Card};  3 segments -> {bank: Bank X, channel: UPI, platform: Android}."""
    abn, remaining, out = set(abnormal), set(abnormal), []
    parts = {s: db.seg_parts(s) for s in scored}
    while remaining:
        best = None
        for r in range(1, 5):
            for dims in combinations(config.DIMS, r):
                for vals in {tuple(parts[s][d] for d in dims) for s in remaining if s in parts}:
                    match = [s for s in scored if all(parts[s][d] == v for d, v in zip(dims, vals))]
                    hit = [s for s in match if s in abn]
                    prec = len(hit) / len(match)
                    cov = len(set(hit) & remaining)
                    if prec < min_precision or cov == 0:
                        continue
                    key = (cov, prec, -r)
                    if best is None or key > best[0]:
                        best = (key, dict(zip(dims, vals)), hit)
        if best is None:
            break
        out.append({"scope": best[1], "segs": sorted(best[2])})
        remaining -= set(best[2])
    return out

def overlap(a, b):
    """Jaccard similarity: a small story inside a big incident is a different story."""
    a, b = set(a), set(b)
    return len(a & b) / max(1, len(a | b))

class Detector:
    def __init__(self):
        self.model = Baseline.load()
        if self.model is None:
            self.model = Baseline().train(); self.model.save()
        self.model.precompute(config.LIVE_START - timedelta(hours=6), config.LIVE_END + timedelta(minutes=30))
        with db.engine().begin() as c:
            db.set_state(c, model_ready=True)
        self._naive_baseline()
        self.reset_memory()

    def reset_memory(self):
        self.candidates = []
        self.prev_dq = {}
        self.last_payday_note = None
        self.maint_noted = set()
        self.adv_count = 0
        self.adv_until = None     # volume-only stories are part of a system-wide shift until this time

    # ------------------------------------------------------------------ the 'simple rule' Lumio is compared with
    def _naive_baseline(self):
        with db.engine().connect() as c:
            df = pd.read_sql(select(db.metrics).where(and_(db.metrics.c.ts >= config.LIVE_START - timedelta(days=7),
                                                           db.metrics.c.ts < config.LIVE_START)), c)
        w = aggregate(df, config.SCAN_MIN)
        w["seg"] = w.bank + "|" + w.channel + "|" + w.platform + "|" + w.region
        w["mod"] = pd.to_datetime(w.ts).dt.hour * 60 + pd.to_datetime(w.ts).dt.minute
        w["lp"] = np.log(w.p95_ms)
        b = w.groupby(["seg", "mod"]).agg(lp_med=("lp", "median"), su_med=("success", "median"), vol_med=("volume", "median")).reset_index()
        mad = w.groupby("seg").apply(lambda d: pd.Series({
            "lp_mad": 1.4826 * np.median(np.abs(d.lp - d.groupby("mod").lp.transform("median"))),
            "su_mad": 1.4826 * np.median(np.abs(d.success - d.groupby("mod").success.transform("median")))}), include_groups=False).reset_index()
        self.naive = b.merge(mad, on="seg").set_index(["seg", "mod"])

    def naive_flags(self, agg):
        idx = list(zip(agg.seg, pd.to_datetime(agg.ts).dt.hour * 60 + pd.to_datetime(agg.ts).dt.minute))
        nb = self.naive.reindex(idx)
        zl = (np.log(agg.p95_ms.values) - nb.lp_med.values) / np.maximum(nb.lp_mad.values, 0.02)
        zs = (agg.success.values - nb.su_med.values) / np.maximum(nb.su_mad.values, 0.002)
        return (zl > 3) | (zs < -3), nb.vol_med.values

    # ------------------------------------------------------------------ 5-minute scoring (feeds the live chart)
    def score_buckets(self, since, until):
        with db.engine().connect() as c:
            df = pd.read_sql(select(db.metrics).where(and_(db.metrics.c.ts >= since, db.metrics.c.ts < until)), c)
        if not len(df):
            return 0
        rules = settings.get("rules")
        agg = aggregate(df, config.BUCKET_MIN)
        exp_lat, exp_succ, zl, zs, sd = self.model.predict(agg)
        ev, _ = self.model.predict_volume(agg)
        rows = []
        for i, r in enumerate(agg.itertuples()):
            rows.append({"ts": r.ts.to_pydatetime(), "seg": f"{r.bank}|{r.channel}|{r.platform}|{r.region}", "volume": int(r.volume),
                         "p95_ms": float(r.p95_ms), "success": float(r.success), "exp_p95_ms": float(exp_lat[i]),
                         "exp_success": float(exp_succ[i]), "band_lo": float(exp_lat[i] * math.exp(-3 * sd[i])),
                         "band_hi": float(exp_lat[i] * math.exp(3 * sd[i])), "status": "ok",
                         "exp_volume": float(ev[i] / 3), "alert_hi": float(exp_lat[i] * math.exp(rules["sensitivity"] * sd[i] / math.sqrt(3)))})
        with db.engine().begin() as c:
            c.execute(insert(db.bucket_scores), rows)
        return len(rows)

    # ------------------------------------------------------------------ the 15-minute scan
    def scan(self, end):
        t0 = time.time()
        start = end - timedelta(minutes=config.SCAN_MIN)
        rules = settings.get("rules")
        sens, vsens, confirm = rules["sensitivity"], rules["volume_sensitivity"], int(rules["confirm_scans"])
        with db.engine().connect() as c:
            raw = pd.read_sql(select(db.metrics).where(and_(db.metrics.c.ts >= start, db.metrics.c.ts < end)), c)
            delta = {r.seg: r.threshold for r in c.execute(select(db.sensitivity))}
            maint = [dict(r) for r in c.execute(select(db.maintenance).where(and_(db.maintenance.c.start_ts < end, db.maintenance.c.end_ts > start))).mappings()]
            feeds = [dict(r) for r in c.execute(select(db.events).where(and_(db.events.c.kind.in_(["weather", "calendar"]), db.events.c.ts <= end))).mappings()]
        clean, dq = quality.check(raw, end, ALL_SEGS)
        hold = [s for s, d in dq.items() if d["status"] == "hold"]
        scored = [s for s in ALL_SEGS if s not in hold]
        events, ws_rows = [], []
        segkey = lambda r: f"{r.bank}|{r.channel}|{r.platform}|{r.region}"
        agg = aggregate(clean[clean.apply(lambda r: segkey(r) not in hold, axis=1)], config.SCAN_MIN) if len(clean) else pd.DataFrame()
        perf, vol_abn, naive_n, info = [], [], 0, {}
        vol_ratio = lat_ratio = 1.0
        if len(agg):
            agg["ts"] = start
            agg["seg"] = agg.bank + "|" + agg.channel + "|" + agg.platform + "|" + agg.region
            exp_lat, exp_succ, zl, zs, _ = self.model.predict(agg)
            ev, zv = self.model.predict_volume(agg)
            nflag, nvol = self.naive_flags(agg)
            naive_n = int(nflag.sum())
            for i, r in enumerate(agg.itertuples()):
                d = delta.get(r.seg, 0.0)
                enough = r.volume >= 30          # too few payments to judge speed or success (quiet night segments)
                f_lat, f_suc = enough and zl[i] > sens + d, enough and zs[i] < -(sens + d)
                f_vol = abs(zv[i]) > vsens + d and ev[i] >= 20
                # business guardrails, but only when the evidence is solid: the success rate must be below the floor even
                # at the top of its 95% confidence range, and payment time needs enough payments to measure a p95
                upper = r.success + 1.96 * math.sqrt(max(r.success * (1 - r.success), 1e-4) / max(r.volume, 1)) + 1 / max(r.volume, 1)
                f_guard = (upper * 100 < rules["success_floor"]) or (r.volume >= 100 and r.p95_ms / 1000 > rules["latency_ceiling"])
                flags = "".join(k for k, v in (("L", f_lat), ("S", f_suc), ("V", f_vol), ("G", f_guard)) if v)
                if f_lat or f_suc or f_guard: perf.append(r.seg)
                if f_vol: vol_abn.append(r.seg)
                info[r.seg] = (float(zl[i]), float(zs[i]), float(zv[i]), r.volume, float(ev[i]))
                ws_rows.append({"ts": start, "seg": r.seg, "volume": int(r.volume), "p95_ms": float(r.p95_ms), "success": float(r.success),
                                "exp_p95_ms": float(exp_lat[i]), "exp_success": float(exp_succ[i]), "z_lat": float(zl[i]),
                                "z_succ": float(zs[i]), "status": "abnormal" if flags else "ok", "naive_flag": bool(nflag[i]),
                                "exp_volume": float(ev[i]), "z_vol": float(zv[i]), "flags": flags})
            vol_ratio = float(agg.volume.sum() / max(1.0, float(np.nansum(ev))))
            lat_ratio = float(mix_p95(agg.p95_ms.values[None], agg.volume.values[None])[0] / mix_p95(exp_lat[None], agg.volume.values[None])[0])
        for s in hold:
            ws_rows.append({"ts": start, "seg": s, "volume": 0, "p95_ms": None, "success": None, "exp_p95_ms": None, "exp_success": None,
                            "z_lat": None, "z_succ": None, "status": "hold", "naive_flag": False, "exp_volume": None, "z_vol": None, "flags": "H"})

        # ---------------------------------------------------------------- data quality: tickets, not pages
        new_hold = [s for s in hold if self.prev_dq.get(s) != "hold"]
        back = [s for s, st in self.prev_dq.items() if st == "hold" and s not in hold]
        dedup = [s for s, d in dq.items() if d["status"] == "dedup" and self.prev_dq.get(s) != "dedup"]
        dedup_over = [s for s, st in self.prev_dq.items() if st == "dedup" and dq[s]["status"] != "dedup"]
        dq_new = []
        for grp in cover(new_hold, ALL_SEGS, 0.99):
            note = dq[grp["segs"][0]]["note"]
            events.append(("data_quality", "quality-gate", f"Scan {end:%H:%M} · Data-quality hold: {tools.scope_label(grp['scope'])} — {note}. Held back, so a broken collector cannot raise a false alarm.", {"segs": grp["segs"]}))
            dq_new.append((grp, "missing", note))
        for grp in cover(dedup, ALL_SEGS, 0.99):
            n = sum(dq[s]["duplicates"] for s in grp["segs"])
            events.append(("data_quality", "quality-gate", f"Scan {end:%H:%M} · {tools.scope_label(grp['scope'])}: {n} duplicate records removed before scoring — a double count would have looked like a surge.", {"segs": grp["segs"]}))
            dq_new.append((grp, "duplicates", f"{n} duplicate records removed before scoring"))
        for grp in cover(back, ALL_SEGS, 0.99):
            events.append(("data_quality", "quality-gate", f"Scan {end:%H:%M} · Data flowing again for {tools.scope_label(grp['scope'])} — hold lifted.", {}))
        self.prev_dq = {s: d["status"] for s, d in dq.items()}

        # ---------------------------------------------------------------- system-wide shifts
        # a system-wide demand shift: most segments move the same way at once (looser per-segment bar, consistent direction)
        zvs = [info[x][2] for x in info]
        soft_down = sum(z < -2.5 for z in zvs); soft_up = sum(z > 2.5 for z in zvs)
        shift = abs(math.log(max(vol_ratio, 1e-3)))
        with db.engine().connect() as c0:
            adv_open = c0.execute(select(db.incidents.c.id).where(and_(db.incidents.c.kind == "advisory", db.incidents.c.status == "open"))).first() is not None
        if adv_open:   # hysteresis: stays open until the whole system is back within ~15% of normal
            advisory = bool(scored) and shift >= math.log(1.15)
        else:          # opens only for a big, broad shift (at least ~35%, most segments moving together)
            advisory = bool(scored) and shift >= math.log(1.55) and (len(vol_abn) / len(scored) > 0.6 or max(soft_down, soft_up) / len(scored) > 0.7)
        vol_stories = [] if advisory else cover(vol_abn, scored)
        perf_wide = bool(scored) and len(perf) / len(scored) > 0.6
        perf_stories = [{"scope": {}, "segs": sorted(perf)}] if perf_wide else cover(perf, scored)
        stories = []
        for p in perf_stories:
            m = next((v for v in vol_stories if overlap(v["segs"], p["segs"]) >= 0.5), None)
            if m: vol_stories.remove(m)
            stories.append(dict(p, metric="volume+performance" if m else "performance"))
        stories += [dict(v, metric="volume") for v in vol_stories]
        if advisory or (self.adv_until and end <= self.adv_until):
            dropped = [x for x in stories if x["metric"] == "volume"]
            stories = [x for x in stories if x["metric"] != "volume"]
            if dropped:
                events.append(("watching", "detector", f"Scan {end:%H:%M} · Volume dips in {', '.join(tools.scope_label(x['scope']) for x in dropped[:3])} are part of the system-wide demand shift — no separate alert.", {}))

        # ---------------------------------------------------------------- planned maintenance: expected, not paged
        kept = []
        for st in stories:
            mw = next((m for m in maint if len([s for s in st["segs"] if tools.segs_matching(json.loads(m["scope"]), [s])]) >= 0.5 * len(st["segs"])), None)
            if mw:
                if mw["id"] not in self.maint_noted:
                    self.maint_noted.add(mw["id"])
                    events.append(("suppressed", "detector", f"Scan {end:%H:%M} · {tools.scope_label(st['scope'])} deviates — but planned maintenance {mw['change']} ({mw['note']}, {mw['start_ts']:%H:%M}–{mw['end_ts']:%H:%M}) is in progress. Expected: no page.", {}))
                continue
            kept.append(st)
        stories = kept

        if naive_n >= 5 and len(agg) and len(perf) <= 2 and world.calendar(start)[3]:
            if self.last_payday_note is None or end - self.last_payday_note >= timedelta(minutes=60):
                self.last_payday_note = end
                events.append(("suppressed", "detector", f"Scan {end:%H:%M} · Payday rush: volume {vol_ratio*100-100:+.0f}% vs an ordinary day's model, payment time up too, at every bank. "
                               f"Matches the learned payday pattern — no alert. A fixed-threshold rule would have paged {naive_n} segments.", {"naive": naive_n}))

        opened, adv_open = [], None
        with db.engine().begin() as c:
            if ws_rows: c.execute(insert(db.window_scores), ws_rows)
            c.execute(insert(db.dq_checks), [{"ts": start, "seg": s, **{k: d[k] for k in ("completeness", "duplicates", "stale_min", "status", "note")}} for s, d in dq.items()])
            open_inc = [dict(r) for r in c.execute(select(db.incidents).where(db.incidents.c.status == "open")).mappings()]

            # data-quality tickets open and close themselves
            for grp, what, note in dq_new:
                res = c.execute(insert(db.incidents).values(kind="data_quality", signature=tools.scope_label(grp["scope"]), status="open", opened_ts=end,
                                started_ts=start, last_ts=end, normal_scans=0, severity="low", metric="data", token=secrets.token_urlsafe(12),
                                scope=json.dumps({"filter": grp["scope"], "segments": grp["segs"]}),
                                headline=f"Data-quality ticket: {'missing data' if what == 'missing' else 'duplicate records'} from {tools.scope_label(grp['scope'])}",
                                cause=json.dumps({"type": "data_quality", "detail": what, "note": note}), explained_by="script"))
                opened.append(("dq", res.inserted_primary_key[0], grp, note))
            for inc in [i for i in open_inc if i["kind"] == "data_quality"]:
                segs = json.loads(inc["scope"])["segments"]
                still = any(dq[s]["status"] in ("hold", "dedup") for s in segs)
                if not still:
                    c.execute(update(db.incidents).where(db.incidents.c.id == inc["id"]).values(status="resolved", resolved_ts=end))
                    events.append(("resolved", "quality-gate", f"Scan {end:%H:%M} · Data ticket #{inc['id']} closed — {tools.scope_label(json.loads(inc['scope'])['filter'])} data is complete again.", {"incident_id": inc["id"]}))
                    opened.append(("resolved", inc["id"], None, None))

            # system-wide demand advisory
            adv = next((i for i in open_inc if i["kind"] == "advisory"), None)
            if advisory:
                self.adv_count += 1
                self.adv_until = end + timedelta(hours=3)
                if adv:
                    c.execute(update(db.incidents).where(db.incidents.c.id == adv["id"]).values(last_ts=end, normal_scans=0))
                elif self.adv_count >= confirm:
                    ctx = [f["message"] for f in feeds if f["kind"] == "weather" and json.loads(f["meta"] or "{}").get("until", "") >= end.isoformat()]
                    res = c.execute(insert(db.incidents).values(kind="advisory", signature="All payments", status="open", opened_ts=end, last_ts=end,
                                    normal_scans=0, severity="info", metric="volume", token=secrets.token_urlsafe(12),
                                    scope=json.dumps({"filter": {}, "segments": scored}), headline=None))
                    iid = res.inserted_primary_key[0]
                    opened.append(("advisory", iid, {"vol_ratio": vol_ratio, "context": ctx, "start": end - timedelta(minutes=config.SCAN_MIN * confirm)}, None))
                    events.append(("advisory", "detector", f"Scan {end:%H:%M} · Payment volume {abs(vol_ratio*100-100):.0f}% {'below' if vol_ratio < 1 else 'above'} normal at every bank, while success rates and payment times are healthy — a system-wide demand change, not a fault. Advisory #{iid} sent to Business Operations." + (f" Context: {ctx[0]}" if ctx else ""), {"incident_id": iid}))
                else:
                    events.append(("watching", "detector", f"Scan {end:%H:%M} · Volume {abs(vol_ratio*100-100):.0f}% {'below' if vol_ratio < 1 else 'above'} normal at every bank at once — checking whether it persists.", {}))
            else:
                self.adv_count = 0
                if adv:
                    n = (adv["normal_scans"] or 0) + 1
                    vals = {"normal_scans": n}
                    if n >= 4:   # demand changes fade slowly: wait an hour before calling it over
                        vals.update(status="resolved", resolved_ts=end)
                        events.append(("resolved", "detector", f"Scan {end:%H:%M} · Advisory #{adv['id']} closed — payment volume back within its normal range.", {"incident_id": adv["id"]}))
                        opened.append(("resolved", adv["id"], None, None))
                    c.execute(update(db.incidents).where(db.incidents.c.id == adv["id"]).values(**vals))

            # stories that belong to an open incident keep it alive
            unmatched = []
            real_open = [i for i in open_inc if i["kind"] in (None, "incident")]
            widened = []
            for st in stories:
                inc = next((i for i in real_open if overlap(json.loads(i["scope"])["segments"], st["segs"]) >= 0.5), None)
                grow = None if inc else next((i for i in real_open if set(json.loads(i["scope"])["segments"]) < set(st["segs"])), None)
                if inc:
                    inc["_seen"] = True
                elif grow:   # the same problem has spread: widen the incident rather than opening another
                    grow["_seen"] = True
                    old = json.loads(grow["scope"])
                    c.execute(update(db.incidents).where(db.incidents.c.id == grow["id"]).values(
                        scope=json.dumps({"filter": st["scope"], "segments": st["segs"]}), signature=tools.scope_label(st["scope"]), metric=st["metric"]))
                    events.append(("detected", "detector", f"Scan {end:%H:%M} · Incident #{grow['id']} has spread from {tools.scope_label(old['filter'])} to {tools.scope_label(st['scope'])} ({len(st['segs'])} segments) — re-investigating the wider problem.", {"incident_id": grow["id"]}))
                    widened.append((grow["id"], st))
                else:
                    unmatched.append(st)
            for inc in real_open:
                segs = json.loads(inc["scope"])["segments"]
                if inc.get("_seen"):
                    c.execute(update(db.incidents).where(db.incidents.c.id == inc["id"]).values(last_ts=end, normal_scans=0))
                elif all(s in hold for s in segs):
                    pass
                else:
                    n = (inc["normal_scans"] or 0) + 1
                    vals = {"normal_scans": n}
                    if n >= config.RESOLVE_SCANS:
                        vals.update(status="resolved", resolved_ts=end)
                        events.append(("resolved", "detector", f"Scan {end:%H:%M} · Incident #{inc['id']} resolved — {inc['headline'] or 'metrics'} back to normal for two scans.", {"incident_id": inc["id"]}))
                        opened.append(("resolved", inc["id"], None, None))
                    c.execute(update(db.incidents).where(db.incidents.c.id == inc["id"]).values(**vals))

            # new stories need confirmation before anyone is paged
            new_cands = []
            for st in unmatched:
                prev = next((p for p in self.candidates if overlap(p["segs"], st["segs"]) >= 0.5 and p["last"] == start), None)
                count = prev["count"] + 1 if prev else 1
                if count >= confirm:
                    res = c.execute(insert(db.incidents).values(kind="incident", signature=tools.scope_label(st["scope"]), status="open", opened_ts=end,
                                    last_ts=end, normal_scans=0, metric=st["metric"], token=secrets.token_urlsafe(12),
                                    scope=json.dumps({"filter": st["scope"], "segments": st["segs"]}), headline=None, severity="pending"))
                    iid = res.inserted_primary_key[0]
                    opened.append(("incident", iid, st, None))
                    events.append(("detected", "detector", f"Scan {end:%H:%M} · {tools.scope_label(st['scope'])} still abnormal on check {count} of {confirm} — Incident #{iid} opened, investigating…", {"incident_id": iid}))
                else:
                    new_cands.append({"scope": st["scope"], "segs": st["segs"], "count": count, "last": end})
                    what = st["metric"].replace("+", " and ")
                    events.append(("watching", "detector", f"Scan {end:%H:%M} · {tools.scope_label(st['scope'])} looks abnormal ({what}, {len(st['segs'])} segment{'s' if len(st['segs']) > 1 else ''}) — check {count} of {confirm}, watching.", {"segs": st["segs"]}))
            self.candidates = new_cands
            c.execute(insert(db.scans).values(ts=end, scored=len(scored), abnormal=len(set(perf) | set(vol_abn)), dq_hold=len(hold),
                                              naive_alerts=naive_n, suppressed=naive_n, duration_ms=round((time.time() - t0) * 1000, 1),
                                              note=f"{len(stories)} stories"))
            for kind, src, msg, meta in events:
                db.log_event(c, end, kind, src, msg, meta)

        from . import mailer
        for iid, st in widened:
            try:
                with db.engine().begin() as c:
                    c.execute(db.investigation_steps.delete().where(db.investigation_steps.c.incident_id == iid))
                investigator.investigate(iid, st["scope"], st["segs"], end, st["metric"], is_update=True)
            except Exception:
                log.exception("re-investigation failed")
        for kind, iid, st, extra in opened:
            try:
                if kind == "incident":
                    investigator.investigate(iid, st["scope"], st["segs"], end, st["metric"])
                elif kind == "advisory":
                    investigator.advisory(iid, end, st)
                elif kind == "dq":
                    investigator.data_ticket(iid, st, extra, end)
                elif kind == "resolved":
                    mailer.queue_resolved(iid, end)
            except Exception as e:
                log.exception("follow-up failed")
                if kind == "incident":
                    with db.engine().begin() as c:
                        c.execute(update(db.incidents).where(db.incidents.c.id == iid).values(headline=f"{tools.scope_label(st['scope'])} abnormal — investigation error", severity="medium"))
                        db.log_event(c, end, "system", "investigator", f"Investigation for #{iid} failed: {e}")
        self._refresh_open(end, {o[1] for o in opened} | {w[0] for w in widened})
        self._escalate(end, rules)
        if end.weekday() < 5 and end.hour == 18 and end.minute == 0 and rules.get("daily_report"):
            mailer.queue_daily_report(end.date(), end)
        return {"perf": len(perf), "vol": len(vol_abn), "hold": len(hold), "stories": len(stories), "advisory": advisory,
                "opened": [o[1] for o in opened if o[0] == "incident"], "naive": naive_n}

    def _refresh_open(self, end, skip):
        with db.engine().connect() as c:
            open_inc = [dict(r) for r in c.execute(select(db.incidents).where(and_(db.incidents.c.status == "open", db.incidents.c.kind == "incident",
                                                                                     db.incidents.c.alert.isnot(None), db.incidents.c.normal_scans == 0))).mappings()]
        for inc in open_inc:
            if inc["id"] in skip:
                continue
            try:
                investigator.refresh_alert(inc, end)
            except Exception:
                log.exception("refresh failed")

    def _escalate(self, end, rules):
        if not rules.get("email_escalation"):
            return
        from . import mailer
        with db.engine().connect() as c:
            due = [dict(r) for r in c.execute(select(db.incidents).where(and_(
                db.incidents.c.status == "open", db.incidents.c.kind == "incident", db.incidents.c.severity == "high",
                db.incidents.c.acked_ts.is_(None), db.incidents.c.escalated.isnot(True),
                db.incidents.c.opened_ts <= end - timedelta(minutes=int(rules["escalate_after_min"]))))).mappings()]
        for inc in due:
            with db.engine().begin() as c:
                c.execute(update(db.incidents).where(db.incidents.c.id == inc["id"]).values(escalated=True))
                db.log_event(c, end, "escalation", "notifier", f"Incident #{inc['id']} not acknowledged after {int(rules['escalate_after_min'])} min — escalated to the Duty Manager.", {"incident_id": inc["id"]})
            mailer.queue_escalation(inc["id"], end)
