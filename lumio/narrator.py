"""Narrator: turns computed facts into a plain-English alert a manager can act on in thirty seconds.

Numbers come from code that ran. If Claude is enabled it may reword the sentences, but any sentence
containing a number that is not in the facts is rejected and the template wording is kept.
"""
import json, re, time, logging
from . import config, tools

log = logging.getLogger("lumio.narrator")

def _s(ms):
    return f"{ms/1000:.1f} s"

def _money(v):
    return f"${v/1000:,.0f}K" if v >= 10000 else f"${v:,.0f}"

def _round_aff(n):
    return f"~{round(n, -2):,.0f}" if n >= 1000 else f"{n:,}"

def _who(scope):
    bank, ch, pl = scope.get("bank"), scope.get("channel"), scope.get("platform")
    what = f"{ch} payments" if ch else "Payments"
    where = []
    if bank: where.append(f"at {bank}")
    if pl: where.append(f"on {pl}")
    if scope.get("region"): where.append(f"in the {scope['region']} region")
    return what, " ".join(where)

def compose(f):
    from . import settings
    rules = settings.get("rules")
    scope, cause, imp = f["scope"], f["cause"], f["impact"]
    now = f.get("now") or {}
    label = tools.scope_label(scope)
    bank, ch, pl = scope.get("bank"), scope.get("channel", "Payment"), scope.get("platform")
    what, where = _who(scope)
    failing = imp["success"] is not None and imp["normal_success"] and (imp["normal_success"] - imp["success"]) > 0.02
    slowing = imp["normal_p95_ms"] and imp["p95_ms"] > 1.4 * imp["normal_p95_ms"]
    vol_x = (imp["per_min"] / imp["normal_per_min"]) if imp.get("normal_per_min") else 1.0
    vanishing = cause.get("metric") == "volume" and vol_x < 0.8
    verb = "slowing and failing" if (failing and slowing) else ("failing" if failing else "slowing")
    start = f["start"]
    t = cause["type"]
    if t == "release":
        v = cause["version"]
        mins = (f.get("release") or {}).get("minutes_before_start", 0)
        if vanishing:
            headline = f"Payments vanishing for {bank} customers on {pl} app v{v}"
            likely = f"Customers on v{v} have almost stopped paying ({(cause.get('vol_ratio') or 0)*100:.0f}% of the payments made on v{cause['other']}): they are likely blocked before reaching payment, e.g. at login."
            nxt = f"Roll back v{v} now and watch the next scan."
        else:
            headline = f"{ch} payments {verb} for {bank} customers on {pl} app v{v}"
            likely = f"The v{v} release changed how {ch} payments are processed in the {pl} app: v{v} is {cause['lat_x']}× slower than v{cause['other']} on the same bank, channel and phones."
            nxt = f"Pause the v{v} rollout and watch the next scan."
        summary = f"Started {start:%H:%M} — {mins} minutes after v{v} began rolling out to {pl} users. Other app versions, channels and banks are unaffected."
    elif t == "dependency":
        s = cause["service"]
        headline = f"{what} {verb} {where} — the {s} service is the bottleneck".replace("  ", " ")
        summary = f"Started {start:%H:%M}. Everything that depends on {s} is affected together, which points upstream rather than at any app or region."
        likely = f"{s} is running at {cause['ratio']}× its normal response time" + (f" with {cause['error_pct']}% errors" if cause.get("error_pct") else "") + ", and these payments wait for it."
        nxt = f"Page the {s} owner and watch the next scan."
    elif t == "regional":
        headline = f"Payments {verb} in the {cause['detail']} region — every bank at once"
        summary = f"Started {start:%H:%M}. All banks, channels and apps in the {cause['detail']} region are affected; other regions are normal."
        likely = f"A network problem between customers in the {cause['detail']} region and the payment systems (internet provider or peering)."
        nxt = "Open a bridge with Network Operations and the internet providers for the region."
    elif t == "fraud":
        headline = f"Card-testing attack on {bank} cards — burst of tiny, mostly declined payments"
        summary = f"Started {start:%H:%M}. Card payments at {bank} jumped to {vol_x:.1f}× normal while the success rate fell."
        likely = f"Bots are testing stolen card numbers with payments under $5: {cause['decline_pct']}% of them are declined."
        nxt = "Block the pattern with a fraud rule and alert the card scheme."
    elif t == "novel":
        g = cause["group"]; gb = cause["group_by"].replace("_", " ")
        headline = f"{ch} payments {verb} at {bank} — concentrated in {gb} {g}"
        summary = f"Started {start:%H:%M}. Not explained by app version, region or upstream services; the AI agent traced it in transaction samples."
        if cause.get("measure") == "latency":
            likely = f"Payments in {gb} '{g}' take {cause['lat_ratio']}× as long as everything else in this scope."
        else:
            to = ", mostly time-outs," if cause.get("timeouts", 0) > 0.5 * max(1, cause.get("failures", 0)) else ""
            likely = f"Payments in {gb} '{g}' fail {cause['failure_rate']*100:.0f}% of the time{to} vs {cause['rest_failure_rate']*100:.1f}% for everything else."
        nxt = f"Check recent limit or fraud-rule changes for {ch.lower()} payments in {gb} {g} at {bank}."
    else:
        headline = f"{label} payments " + ("vanishing" if vanishing else verb)
        summary = f"Started {start:%H:%M}. Scripted checks and the agent could not isolate a cause."
        likely = "Unknown — evidence attached for a human to review."
        nxt = "Assign to the on-call analyst with the evidence below."
    if vanishing:
        aff, aff_sub = imp["missing"], f"missing payments since {start:%H:%M}"
        var = imp["missing"] * imp["avg_amount"]
    elif t == "fraud":
        aff, aff_sub = cause.get("attempts", imp["surplus"]), f"suspicious attempts since {start:%H:%M}"
        var = imp["extra_failures"] * imp["avg_amount"]
    elif failing and not slowing:
        aff, aff_sub = imp["extra_failures"], f"extra failures since {start:%H:%M}"
        var = imp["extra_failures"] * imp["avg_amount"]
    else:
        aff, aff_sub, var = imp["affected"], f"since {start:%H:%M}", imp["value_at_risk"]
    if vanishing or t == "fraud":
        first = {"label": "Payments / min", "value": f"{imp['per_min']:,.0f}", "sub": f"normal {imp['normal_per_min']:,.0f}"}
    else:
        first = {"label": "Payment time", "value": _s(imp["p95_ms"]), "sub": f"normal {_s(imp['normal_p95_ms'])}" if imp["normal_p95_ms"] else ""}
    tiles = [first,
        {"label": "Success rate", "value": f"{imp['success']*100:.1f}%", "sub": f"normal {imp['normal_success']*100:.1f}%" if imp["normal_success"] else ""},
        {"label": "Affected", "value": _round_aff(aff), "sub": aff_sub},
        {"label": "Value at risk", "value": "~" + _money(var), "sub": f"at ${imp['avg_amount']:,.0f} average"},
    ]
    sev = "high" if var >= rules["high_value_at_risk"] or aff >= 2000 else "medium"
    return {"headline": headline, "summary": summary, "likely_cause": likely, "next_step": nxt, "tiles": tiles,
            "severity": sev, "scope_label": label, "started": start.strftime("%H:%M"), "value_at_risk": var, "affected": aff}

def compose_advisory(imp, now_stats, info, now):
    drop = abs(1 - info["vol_ratio"]) * 100
    word = "below" if info["vol_ratio"] < 1 else "above"
    ctx = (info.get("context") or [None])[0]
    tiles = [{"label": "Payments / min", "value": f"{imp['per_min']:,.0f}", "sub": f"normal {imp['normal_per_min']:,.0f}"},
             {"label": "Success rate", "value": f"{now_stats['success']*100:.1f}%", "sub": f"normal {now_stats['exp_success']*100:.1f}%"},
             {"label": "Payment time", "value": _s(now_stats["p95_ms"]), "sub": f"normal {_s(now_stats['exp_p95_ms'])}"},
             {"label": "Missing payments", "value": _round_aff(imp["missing"]), "sub": f"since {info['start']:%H:%M}"}]
    return {"headline": f"Payment volume {drop:.0f}% {word} normal at every bank — a demand change, not a fault",
            "summary": f"Since {info['start']:%H:%M}, every bank, channel and region is {'quieter' if word == 'below' else 'busier'} at once, while success rates and payment times stay healthy." + (f" {ctx}." if ctx else ""),
            "likely_cause": f"Customers are paying {'less' if word == 'below' else 'more'} (weather, holiday or a city-wide event). No system is failing, so nobody is paged.",
            "next_step": "Business Operations: adjust today's forecasts; no technical action.", "tiles": tiles, "severity": "info",
            "scope_label": "All payments", "started": info["start"].strftime("%H:%M"), "value_at_risk": 0, "affected": imp["missing"]}

def compose_dq(grp, note, now):
    label = tools.scope_label(grp["scope"])
    return {"headline": f"Data-quality ticket: {label}", "summary": f"{note.capitalize()}. Lumio held this data back so it could not raise a false alarm.",
            "likely_cause": "A monitoring collector or pipeline problem, not a payment problem.",
            "next_step": "Restart or fix the collector; Lumio closes this ticket automatically when complete data flows again.",
            "tiles": [{"label": "Segments", "value": str(len(grp["segs"])), "sub": label},
                      {"label": "Detected", "value": now.strftime("%H:%M"), "sub": now.strftime("%a %d %b")},
                      {"label": "False alarms", "value": "0", "sub": "held, not scored"},
                      {"label": "Customer impact", "value": "None", "sub": "data problem only"}],
            "severity": "low", "scope_label": label, "started": now.strftime("%H:%M"), "value_at_risk": 0, "affected": 0}

_NUM = re.compile(r"\d+(?:[.,]\d+)?")

def polish_with_llm(alert, facts):
    """Optional: let Claude reword the sentences. Returns (alert, usage) — unchanged if anything looks off."""
    if not config.ANTHROPIC_API_KEY:
        return alert, None
    t0 = time.time()
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        allowed = set(_NUM.findall(json.dumps(alert)))
        prompt = ("Rewrite these alert sentences for a busy payments manager: clear, calm, specific. Keep every number exactly as given "
                  "and add no new numbers. Return JSON with keys headline, summary, likely_cause, next_step.\n" +
                  json.dumps({k: alert[k] for k in ("headline", "summary", "likely_cause", "next_step")}))
        r = client.messages.create(model=config.LLM_MODEL, max_tokens=500, messages=[{"role": "user", "content": prompt}])
        txt = "".join(b.text for b in r.content if b.type == "text")
        new = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        usage = {"llm_calls": 1, "tokens_in": r.usage.input_tokens, "tokens_out": r.usage.output_tokens,
                 "cost_usd": round(r.usage.input_tokens / 1e6 * config.LLM_PRICE_IN + r.usage.output_tokens / 1e6 * config.LLM_PRICE_OUT, 5),
                 "duration_ms": (time.time() - t0) * 1000}
        out = dict(alert)
        for k in ("headline", "summary", "likely_cause", "next_step"):
            v = new.get(k)
            if isinstance(v, str) and v and set(_NUM.findall(v)) <= allowed:
                out[k] = v
        out["worded_by"] = "claude"
        return out, usage
    except Exception as e:
        log.warning("narrator LLM skipped: %s", e)
        return alert, None
