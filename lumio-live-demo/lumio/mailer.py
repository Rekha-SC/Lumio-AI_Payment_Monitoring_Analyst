"""Email alerts: compose (HTML + plain text + embedded chart), queue in the outbox, send over SMTP.

Every email is also kept in the outbox, so the presenter can show exactly what was sent even if the venue's
network blocks email. Buttons in the email call back into the running app (/act/<token>/<action>).
"""
import io, json, base64, smtplib, ssl, threading, time, logging
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from sqlalchemy import select, insert, update, and_
from . import config, db, settings, routing, reports, tools
from .settings import DEPARTMENTS

log = logging.getLogger("lumio.mailer")
SEV = {"high": ("#D97706", "High severity"), "medium": ("#B7791F", "Medium severity"), "low": ("#5F6670", "Data-quality ticket"),
       "info": ("#0F766E", "Advisory"), "resolved": ("#0F766E", "Resolved"), "escalation": ("#B42318", "Escalation"),
       "report": ("#14161A", "End-of-day report"), "test": ("#0F766E", "Test")}
RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
LOGO = None

def _logo():
    global LOGO
    if LOGO is None:
        import os
        with open(os.path.join(config.ROOT, "ui", "logo.png"), "rb") as f:
            LOGO = f.read()
    return LOGO

# ----------------------------------------------------------------------------------------------- chart image
def chart_png(scope, segs, start, now, metric, title):
    """Actual vs Lumio's expected for the incident scope — rendered with matplotlib for the email."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd, numpy as np
    from .model import mix_p95
    since = (start or now) - timedelta(hours=3)
    with db.engine().connect() as c:
        bs = pd.read_sql(select(db.bucket_scores).where(and_(db.bucket_scores.c.ts >= since, db.bucket_scores.c.ts < now + timedelta(minutes=5),
                                                             db.bucket_scores.c.seg.in_(segs))), c)
    if not len(bs):
        return None
    pts = []
    for ts, d in bs.groupby("ts"):
        v = d.volume.values[None]
        pts.append((ts, float(mix_p95(d.p95_ms.values[None], v)[0]) / 1000, float(mix_p95(d.exp_p95_ms.values[None], v)[0]) / 1000,
                    float(mix_p95(d.alert_hi.values[None], v)[0]) / 1000 if d.alert_hi.notna().all() else np.nan,
                    float(d.volume.sum()) / 5, float(d.exp_volume.sum()) / 5,
                    float(np.average(d.success, weights=d.volume)) * 100, float(np.average(d.exp_success, weights=d.volume)) * 100))
    df = pd.DataFrame(pts, columns=["ts", "p95", "exp", "alert", "vol", "exp_vol", "succ", "exp_succ"])
    fig, ax = plt.subplots(figsize=(6.4, 2.3), dpi=150)
    ink, teal, amb, grey = "#14161A", "#0F766E", "#D97706", "#8A9098"
    if metric == "volume":
        y, e, unit = df.vol, df.exp_vol, "payments / min"
        ax.fill_between(df.ts, e * 0.75, e * 1.25, color=teal, alpha=0.12, lw=0)
    elif metric == "success":
        y, e, unit = df.succ, df.exp_succ, "success rate %"
    else:
        y, e, unit = df.p95, df.exp, "payment time (s)"
        ax.plot(df.ts, df.alert, color=amb, lw=1.1, ls=(0, (2, 2)), label="alert threshold")
    ax.plot(df.ts, e, color=grey, lw=1.3, ls="--", label="Lumio's expected")
    ax.plot(df.ts, y, color=ink, lw=2, label="actual")
    if start is not None:
        ax.axvspan(start, df.ts.max(), color=amb, alpha=0.08, lw=0)
        ax.axvline(start, color=amb, lw=1.2)
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    for s in ("left", "bottom"): ax.spines[s].set_color("#C9CDD1")
    ax.tick_params(labelsize=7, colors="#5A6068")
    import matplotlib.dates as mdates
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_ylabel(unit, fontsize=7, color="#5A6068")
    ax.set_title(title, fontsize=8, loc="left", color=ink, fontweight="bold")
    ax.legend(fontsize=6.5, frameon=False, loc="upper left", ncol=3)
    ax.grid(axis="y", color="#EEF0F1", lw=0.8)
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor="white"); plt.close(fig)
    return buf.getvalue()

def day_chart_png(rep):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    h = rep.get("hourly") or []
    if not h:
        return None
    fig, ax = plt.subplots(figsize=(6.4, 2.1), dpi=150)
    xs = [x["h"] for x in h]
    ax.bar(xs, [x["payments"] for x in h], color="#0F766E", alpha=0.85, width=0.75, label="payments")
    ax.plot(xs, [x["expected"] for x in h], color="#8A9098", ls="--", lw=1.3, label="expected")
    for i in rep["incidents"]:
        if i["kind"] == "incident" and i["started"]:
            hh = int(i["started"][:2]) + int(i["started"][3:]) / 60
            ax.axvline(hh, color="#D97706", lw=1.2); ax.text(hh + 0.15, ax.get_ylim()[1] * 0.92, f"#{i['id']}", fontsize=6.5, color="#D97706")
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=7, colors="#5A6068"); ax.set_xticks(range(0, 24, 3)); ax.set_xticklabels([f"{x:02d}:00" for x in range(0, 24, 3)])
    ax.legend(fontsize=6.5, frameon=False, loc="upper left"); ax.set_title("Payments per hour vs Lumio's expectation", fontsize=8, loc="left", fontweight="bold")
    fig.tight_layout(); buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor="white"); plt.close(fig)
    return buf.getvalue()

# ----------------------------------------------------------------------------------------------- HTML building blocks
F = "font-family:Inter,'Segoe UI',Helvetica,Arial,sans-serif;"

def _btn(href, label, style="primary"):
    bg = {"primary": "#0F766E", "danger": "#D97706", "ghost": "#FFFFFF"}[style]
    fg = "#FFFFFF" if style != "ghost" else "#14161A"
    bd = "#0F766E" if style == "primary" else ("#D97706" if style == "danger" else "#C9CDD1")
    return (f'<a href="{href}" style="{F}display:inline-block;background:{bg};color:{fg};border:1px solid {bd};border-radius:8px;'
            f'padding:11px 18px;margin:0 8px 8px 0;font-size:14px;font-weight:600;text-decoration:none">{escape(label)}</a>')

def _tiles(tiles):
    tds = "".join(f'<td width="25%" style="padding:4px"><div style="background:#F6F7F7;border:1px solid #ECEDEE;border-radius:8px;padding:10px 12px">'
                  f'<div style="{F}font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#D97706">{escape(t["label"])}</div>'
                  f'<div style="{F}font-size:20px;font-weight:700;color:#14161A;margin-top:2px">{escape(str(t["value"]))}</div>'
                  f'<div style="{F}font-size:11px;color:#5A6068">{escape(t.get("sub") or "")}</div></div></td>' for t in tiles)
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:6px 0 4px"><tr>{tds}</tr></table>'

def _section(title, body):
    return (f'<div style="{F}font-size:11px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase;color:#0F766E;margin:22px 0 8px">{escape(title)}</div>{body}')

def _kv(rows):
    trs = "".join(f'<tr><td style="{F}font-size:13px;color:#5A6068;padding:6px 12px 6px 0;vertical-align:top;white-space:nowrap">{escape(k)}</td>'
                  f'<td style="{F}font-size:13px;color:#14161A;padding:6px 0;vertical-align:top">{v}</td></tr>' for k, v in rows)
    return f'<table role="presentation" cellspacing="0" cellpadding="0">{trs}</table>'

def _wrap(color, label, right, inner, footer_note=""):
    return f'''<!doctype html><html><body style="margin:0;padding:0;background:#E6E7E8">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#E6E7E8"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px;width:100%;background:#FFFFFF;border-radius:12px;overflow:hidden;border:1px solid #D6D8DA">
<tr><td style="background:{color};padding:12px 22px"><table role="presentation" width="100%"><tr>
<td style="{F}color:#FFFFFF;font-size:12px;font-weight:700;letter-spacing:1.6px;text-transform:uppercase"><img src="cid:logo" width="18" height="18" style="vertical-align:-4px;margin-right:8px;border-radius:4px" alt="">Lumio · {escape(label)}</td>
<td align="right" style="{F}color:#FFFFFF;font-size:12px;font-weight:700;letter-spacing:1px">{escape(right)}</td></tr></table></td></tr>
<tr><td style="padding:22px 24px 26px">{inner}</td></tr>
<tr><td style="background:#F3F4F5;padding:14px 24px;border-top:1px solid #ECEDEE;{F}font-size:11px;line-height:1.5;color:#5A6068">
Every number in this email was computed by Lumio's code from the live data; the AI only chooses queries and words.{footer_note}<br>
Lumio · data analytics capstone POC · simulated payment data built on the Numenta Anomaly Benchmark · times shown are simulated.</td></tr>
</table></td></tr></table></body></html>'''

def _url(token, action):
    base = settings.get("email")["dashboard_url"].rstrip("/")
    return f"{base}/act/{token}/{action}"

# ----------------------------------------------------------------------------------------------- composers
def _incident(iid):
    with db.engine().connect() as c:
        r = c.execute(select(db.incidents).where(db.incidents.c.id == iid)).mappings().first()
    return dict(r) if r else None

def _recipients(route):
    def addr(d):
        a = settings.address_for(d)
        return formataddr((DEPARTMENTS[d]["name"], a)) if a else None
    to = [addr(d) for d in route["to"]]; cc = [addr(d) for d in route.get("cc", [])]
    return [a for a in to if a], [a for a in cc if a]

def compose_alert(inc, now):
    a = json.loads(inc["alert"]); route = json.loads(inc["routing"]); sc = json.loads(inc["scope"]); cause = json.loads(inc["cause"] or "{}")
    kind = inc["kind"]
    sev = inc["severity"] if kind == "incident" else ("info" if kind == "advisory" else "low")
    color, label = SEV[sev]
    when = f"{inc['opened_ts']:%a %d %b · %H:%M}"
    metric = "volume" if (cause.get("metric") == "volume" or kind == "advisory" or cause.get("type") == "fraud") else \
             ("success" if a["tiles"][0]["label"] == "Payment time" and "fail" in a["headline"] and "slow" not in a["headline"] else "latency")
    png = None if kind == "data_quality" else chart_png(sc["filter"], sc["segments"], inc["started_ts"], now, metric, f"{a['scope_label']} — actual vs expected")
    tok = inc["token"]
    ctas = "".join(_btn(_url(tok, c["action"]), c["label"], c["style"]) for c in route["ctas"])
    if kind == "incident":
        ctas += _btn(_url(tok, "ack"), "Acknowledge", "primary" if not route["ctas"] or route["ctas"][0]["action"] != "ack" else "ghost")
    ctas += _btn(_url(tok, "open"), "Open in Lumio", "ghost")
    if kind == "incident":
        ctas += _btn(_url(tok, "dismiss"), "Not useful", "ghost")
    by_team = {}
    for x in route["actions"]:
        by_team.setdefault(x["team"], []).append(x["text"])
    acts = "".join(f'<tr><td style="padding:6px 0;vertical-align:top;width:34px"><div style="{F}width:22px;height:22px;border-radius:11px;background:#0F766E;color:#fff;font-size:11px;font-weight:700;text-align:center;line-height:22px">{i+1}</div></td>'
                   f'<td style="padding:6px 0;{F}font-size:13.5px;color:#14161A;line-height:1.45"><b style="color:#0F766E">{escape(team)}</b><br>{"<br>".join(escape(t) for t in texts)}</td></tr>'
                   for i, (team, texts) in enumerate(by_team.items()))
    explained = {"script": "Scripted checks (no AI needed)", "agent": "AI agent, verified by code"}.get(inc["explained_by"], "—")
    rows = [("Where", escape(a["scope_label"])), ("Since", escape(f"{inc['started_ts']:%a %d %b %H:%M}" if inc["started_ts"] else a["started"])),
            ("Likely cause", escape(a["likely_cause"]))]
    if kind == "incident":
        rows += [("Confidence", f"{(inc['confidence'] or 0)*100:.0f}% · {escape(explained)}")]
    ev = "".join(f"<li style='margin:0 0 4px'>{escape(e)}</li>" for e in (a.get("evidence") or [])[1:4])
    teams_to = ", ".join(DEPARTMENTS[d]["name"] for d in route["to"]); teams_cc = ", ".join(DEPARTMENTS[d]["name"] for d in route.get("cc", []))
    inner = (f'<div style="{F}font-size:22px;line-height:1.3;font-weight:700;color:#14161A;margin:0 0 8px">{escape(a["headline"])}</div>'
             f'<div style="{F}font-size:14px;line-height:1.5;color:#2C3036;margin:0 0 12px">{escape(a["summary"])}</div>'
             + _tiles(a["tiles"]) +
             (f'<img src="cid:chart" width="592" style="width:100%;max-width:592px;display:block;margin:14px 0 4px;border:1px solid #ECEDEE;border-radius:8px" alt="Chart: actual vs expected">' if png else "")
             + _section("What we know", _kv(rows))
             + (_section("Evidence (from database queries)", f'<ul style="{F}font-size:12.5px;color:#2C3036;padding-left:18px;margin:0;line-height:1.45">{ev}</ul>') if ev else "")
             + _section("Action items", f'<table role="presentation" cellspacing="0" cellpadding="0" width="100%">{acts}</table>')
             + _section("Do it from here", f'<div>{ctas}</div>')
             + f'<div style="{F}font-size:12px;color:#5A6068;margin-top:14px;padding:10px 12px;background:#F6F7F7;border-radius:8px">'
               f'<b>Routed to:</b> {escape(teams_to)}' + (f' · <b>Copied:</b> {escape(teams_cc)}' if teams_cc else "") +
               f'<br>{escape(route["why"])}</div>')
    pre = {"incident": f"[{label.split()[0].upper()}]", "advisory": "[ADVISORY]", "data_quality": "[DATA TICKET]"}[kind]
    subject = f"{pre} INC-{inc['id']:04d} · {a['headline']}"
    text = (f"{subject}\n\n{a['summary']}\n\n" + "\n".join(f"{t['label']}: {t['value']} ({t.get('sub','')})" for t in a["tiles"]) +
            f"\n\nLikely cause: {a['likely_cause']}\n\nAction items:\n" + "\n".join(f"- {x['team']}: {x['text']}" for x in route["actions"]) +
            f"\n\nAcknowledge: {_url(tok, 'ack')}\nOpen in Lumio: {_url(tok, 'open')}\n")
    html = _wrap(color, label, f"{when} · INC-{inc['id']:04d}", inner)
    return subject, html, text, png, route

def compose_resolved(inc, now):
    a = json.loads(inc["alert"] or "{}"); route = json.loads(inc["routing"] or "{}")
    if not a or not route:
        return None
    dur = (inc["resolved_ts"] or now) - (inc["started_ts"] or inc["opened_ts"])
    mins = int(dur.total_seconds() // 60)
    acts = json.loads(inc["actions"] or "[]")
    fixed = "; ".join(f"{x['label']} by {x['by']} at {x['at']}" for x in acts) or "No manual action was recorded — it recovered on its own or was fixed outside Lumio."
    inner = (f'<div style="{F}font-size:22px;line-height:1.3;font-weight:700;color:#14161A;margin:0 0 8px">Resolved: {escape(a["headline"])}</div>'
             f'<div style="{F}font-size:14px;line-height:1.5;color:#2C3036;margin:0 0 12px">Back to normal for two scans at {inc["resolved_ts"] or now:%H:%M}, '
             f'{mins // 60}h {mins % 60:02d}m after it began. Final numbers:</div>' + _tiles(a.get("tiles", []))
             + _section("What happened", _kv([("Cause", escape(a.get("likely_cause", ""))), ("Actions taken", escape(fixed)),
                                              ("Acknowledged", escape(f"{inc['acked_by']} at {inc['acked_ts']:%H:%M}" if inc["acked_ts"] else "Not acknowledged"))]))
             + _section("Next", f'<div>{_btn(_url(inc["token"], "open"), "Open the incident timeline", "primary")}</div>'))
    subject = f"[RESOLVED] INC-{inc['id']:04d} · {a['headline']}"
    text = f"{subject}\nResolved after {mins} minutes.\nCause: {a.get('likely_cause','')}\nActions: {fixed}\n{_url(inc['token'], 'open')}\n"
    return subject, _wrap(SEV["resolved"][0], "Resolved", f"{(inc['resolved_ts'] or now):%a %d %b · %H:%M} · INC-{inc['id']:04d}", inner), text, None, route

def compose_escalation(inc, now):
    a = json.loads(inc["alert"]); route = json.loads(inc["routing"])
    route = dict(route, to=["duty"] + [d for d in route["to"] if d != "duty"], cc=[d for d in route.get("cc", []) if d != "duty"])
    mins = int((now - inc["opened_ts"]).total_seconds() // 60)
    inner = (f'<div style="{F}font-size:13px;font-weight:700;color:#B42318;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px">Not acknowledged for {mins} minutes</div>'
             f'<div style="{F}font-size:22px;line-height:1.3;font-weight:700;color:#14161A;margin:0 0 8px">{escape(a["headline"])}</div>'
             f'<div style="{F}font-size:14px;line-height:1.5;color:#2C3036;margin:0 0 12px">Duty Manager: nobody has taken ownership yet. Customers are still affected.</div>'
             + _tiles(a["tiles"]) + _section("Take ownership", f'<div>{_btn(_url(inc["token"], "ack"), "Acknowledge now", "danger")}{_btn(_url(inc["token"], "open"), "Open in Lumio", "ghost")}</div>'))
    subject = f"[ESCALATION] INC-{inc['id']:04d} not acknowledged · {a['headline']}"
    text = f"{subject}\nNot acknowledged for {mins} minutes.\nAcknowledge: {_url(inc['token'], 'ack')}\n"
    return subject, _wrap(SEV["escalation"][0], "Escalation", f"{now:%a %d %b · %H:%M} · INC-{inc['id']:04d}", inner), text, None, route

def compose_report(d, now):
    rep = reports.day_report(d)
    png = day_chart_png(rep)
    tiles = [{"label": "Payments", "value": f"{rep['payments']:,}", "sub": "processed so far today"},
             {"label": "Success rate", "value": f"{rep['success']:.2f}%" if rep["success"] else "—", "sub": "all banks"},
             {"label": "Incidents", "value": str(rep["n_incidents"]), "sub": f"median detect {rep['mttd']} min" if rep["mttd"] is not None else "none"},
             {"label": "Pages avoided", "value": f"{rep['naive_pages']:,}", "sub": "vs a fixed-threshold rule"}]
    rows = "".join(f'<tr><td style="{F}font-size:12.5px;padding:7px 6px;border-bottom:1px solid #ECEDEE">#{i["id"]}</td>'
                   f'<td style="{F}font-size:12.5px;padding:7px 6px;border-bottom:1px solid #ECEDEE">{escape(i["headline"] or "")}</td>'
                   f'<td style="{F}font-size:12.5px;padding:7px 6px;border-bottom:1px solid #ECEDEE;white-space:nowrap">{i["opened"]}</td>'
                   f'<td style="{F}font-size:12.5px;padding:7px 6px;border-bottom:1px solid #ECEDEE">{escape(i["status"])}</td></tr>' for i in rep["incidents"])
    table = (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>' +
             "".join(f'<th align="left" style="{F}font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#5A6068;padding:6px;border-bottom:2px solid #14161A">{h}</th>' for h in ("ID", "What", "Alerted", "Status")) +
             f'</tr>{rows or "<tr><td colspan=4 style=padding:8px>No incidents today.</td></tr>"}</table>')
    inner = (f'<div style="{F}font-size:22px;line-height:1.3;font-weight:700;color:#14161A;margin:0 0 6px">{escape(rep["label"])}</div>'
             f'<div style="{F}font-size:14px;color:#2C3036;margin:0 0 10px">Business day summary at {now:%H:%M}' + (" · payday" if rep["payday"] else "") + '</div>'
             + _tiles(tiles) + (f'<img src="cid:chart" width="592" style="width:100%;max-width:592px;display:block;margin:14px 0 4px;border:1px solid #ECEDEE;border-radius:8px" alt="Payments per hour">' if png else "")
             + _section("Today's incidents, tickets and advisories", table)
             + _section("Open the full report", f'<div>{_btn(settings.get("email")["dashboard_url"].rstrip("/") + "/#reports=" + rep["date"], "Open report in Lumio", "primary")}</div>'))
    subject = f"[DAILY REPORT] {rep['short']} · {rep['n_incidents']} incident{'s' if rep['n_incidents'] != 1 else ''} · {rep['success']:.2f}% success" if rep["success"] else f"[DAILY REPORT] {rep['short']}"
    route = {"to": ["duty", "bizops"], "cc": ["product"], "actions": [], "ctas": [], "why": "End-of-day report"}
    text = f"{subject}\nPayments: {rep['payments']:,}\nIncidents: {rep['n_incidents']}\n"
    return subject, _wrap(SEV["report"][0], "End-of-day report", f"{now:%a %d %b · %H:%M}", inner), text, png, route

# ----------------------------------------------------------------------------------------------- outbox
def _enqueue(kind, iid, now, composed, force_preview=False):
    if not composed:
        return None
    subject, html, text, png, route = composed
    to, cc = _recipients(route)
    e = settings.get("email")
    ready = e.get("enabled") and e.get("host") and (e.get("from_addr") or e.get("username")) and to
    status = "queued" if ready and not force_preview else "preview"
    err = None if ready else ("Email sending is off — turn it on in Settings → Email" if not e.get("enabled") else
                              "No recipient address — set your inbox in Settings → Email" if not to else "Mail server not set")
    images = {"logo": base64.b64encode(_logo()).decode()}
    if png:
        images["chart"] = base64.b64encode(png).decode()
    with db.engine().begin() as c:
        res = c.execute(insert(db.outbox).values(ts=now, incident_id=iid, kind=kind, to_addrs=json.dumps(to), cc_addrs=json.dumps(cc),
                        subject=subject, html=html, text=text, status=status, error=err if status == "preview" else None,
                        departments=json.dumps({"to": route["to"], "cc": route.get("cc", [])}), images=json.dumps(images)))
        oid = res.inserted_primary_key[0]
        if iid:
            db.log_event(c, now, "email", "notifier", f"✉ {subject} → " + ", ".join(DEPARTMENTS[d]["name"] for d in route["to"]) +
                         (" (preview — sending is off)" if status == "preview" else ""), {"incident_id": iid, "outbox_id": oid})
    _wake.set()
    return oid

def queue_alert(iid, now, update=False):
    inc = _incident(iid)
    rules = settings.get("rules")
    if inc["kind"] == "incident" and RANK.get(inc["severity"], 2) < RANK.get(rules["email_min_severity"], 2):
        return None
    if inc["kind"] == "data_quality" and not rules.get("email_tickets", True):
        return None
    if inc["kind"] == "advisory" and not rules.get("email_advisories", True):
        return None
    comp = compose_alert(inc, now)
    if update and comp:
        comp = ("[UPDATE] " + comp[0],) + comp[1:]
    return _enqueue("alert", iid, now, comp)

def queue_resolved(iid, now):
    if not settings.get("rules").get("email_resolved"):
        return None
    inc = _incident(iid)
    with db.engine().connect() as c:
        sent = c.execute(select(db.outbox.c.id).where(and_(db.outbox.c.incident_id == iid, db.outbox.c.kind == "alert"))).first()
    if not sent:
        return None
    return _enqueue("resolved", iid, now, compose_resolved(inc, now))

def queue_escalation(iid, now):
    return _enqueue("escalation", iid, now, compose_escalation(_incident(iid), now))

def queue_daily_report(d, now):
    oid = _enqueue("report", None, now, compose_report(d, now))
    with db.engine().begin() as c:
        db.log_event(c, now, "email", "notifier", f"✉ End-of-day report for {d:%a %d %b} sent to the Duty Manager and Business Operations.", {"outbox_id": oid})
    return oid

def send_test(to_addr):
    now = datetime.utcnow()
    inner = (f'<div style="{F}font-size:22px;font-weight:700;color:#14161A;margin:0 0 8px">Lumio email is working ✓</div>'
             f'<div style="{F}font-size:14px;color:#2C3036;line-height:1.5">This is how Lumio alerts will look. Each department is addressed separately; '
             f'in demo mode they all arrive here, tagged by team.</div>' + _tiles([
                 {"label": "Server", "value": settings.get("email")["host"], "sub": "SMTP"}, {"label": "Departments", "value": str(len(DEPARTMENTS)), "sub": "routing rules ready"},
                 {"label": "Template", "value": "HTML", "sub": "with chart and buttons"}, {"label": "Status", "value": "OK", "sub": "connected"}]))
    html = _wrap(SEV["test"][0], "Test email", now.strftime("%H:%M"), inner)
    msg = _build(f"Lumio test email ✓", html, "Lumio email is working.", [to_addr], [], {"logo": base64.b64encode(_logo()).decode()})
    _smtp_send(msg)

# ----------------------------------------------------------------------------------------------- SMTP
def _build(subject, html, text, to, cc, images):
    e = settings.get("email")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((e.get("from_name") or "Lumio Alerts", e.get("from_addr") or e.get("username")))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Message-ID"] = make_msgid(domain="lumio.local")
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    html_part = msg.get_payload()[-1]
    for cid, b64 in images.items():
        html_part.add_related(base64.b64decode(b64), maintype="image", subtype="png", cid=f"<{cid}>", filename=f"{cid}.png")
    return msg

def _smtp_send(msg):
    e = settings.get("email")
    host, port, sec = e["host"], int(e.get("port") or 587), e.get("security", "starttls")
    ctx = ssl.create_default_context()
    if sec == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
    else:
        s = smtplib.SMTP(host, port, timeout=20)
        s.ehlo()
        if sec == "starttls":
            s.starttls(context=ctx); s.ehlo()
    try:
        if e.get("username"):
            s.login(e["username"], e.get("password", ""))
        s.send_message(msg)
    finally:
        try: s.quit()
        except Exception: pass

_wake = threading.Event()

def send_pending():
    with db.engine().connect() as c:
        rows = [dict(r) for r in c.execute(select(db.outbox).where(db.outbox.c.status == "queued").order_by(db.outbox.c.id).limit(10)).mappings()]
    for r in rows:
        try:
            msg = _build(r["subject"], r["html"], r["text"], json.loads(r["to_addrs"]), json.loads(r["cc_addrs"] or "[]"), json.loads(r["images"] or "{}"))
            _smtp_send(msg)
            vals = {"status": "sent", "sent_at": datetime.utcnow(), "error": None}
        except Exception as ex:
            vals = {"status": "failed", "error": f"{type(ex).__name__}: {ex}"[:400]}
            log.warning("email failed: %s", ex)
        with db.engine().begin() as c:
            c.execute(update(db.outbox).where(db.outbox.c.id == r["id"]).values(**vals))
    return len(rows)

def run_sender(stop=None):
    while not (stop and stop.is_set()):
        try:
            n = send_pending()
        except Exception:
            log.exception("sender loop"); n = 0
        if not n:
            _wake.wait(2.0); _wake.clear()
