"""The AI agent: only called when the scripted investigation cannot explain an incident.

Two modes, same tools, same trace format:
  * Claude mode   — when ANTHROPIC_API_KEY is set. Claude chooses which tool to call next and states a hypothesis.
  * Offline mode  — a deterministic planner walks the same tools in a fixed order. Free, and works with no internet.
Every token and dollar is written to the cost log, so 'is the agent worth it?' is a number, not an opinion.
"""
import json, time, logging
from . import config, tools

log = logging.getLogger("lumio.agent")
GROUPS = ["amount_band", "merchant_category", "platform", "region", "app_version"]

def llm_available():
    return bool(config.ANTHROPIC_API_KEY)

def _pick(res):
    g = res["groups"][0] if res["groups"] else None
    return g if g and tools.explains(g, res["measure"]) else None

def _strength(g, measure):
    return g["lift"] * g["share_of_failures"] if measure == "failures" else g["lat_ratio"] * (1 - g["share_of_samples"])

def investigate_offline(conn, scope, start, now, record, measure="failures"):
    t0 = time.time()
    what = "failures" if measure == "failures" else "slow payments"
    record("agent", "plan", {"scope": scope, "measure": measure},
           "Pre-built slices (bank, channel, platform, region, app version) and upstream dependencies did not explain this. "
           f"Plan: look at transaction-level samples and test whether any group explains the {what}.", {})
    best = None
    for gb in GROUPS[:3]:
        t1 = time.time()
        finding, out = tools.query_samples(conn, scope, start, now, gb, measure)
        record("agent", "query_samples", {"group_by": gb, "measure": measure}, finding, out, (time.time() - t1) * 1000)
        g = _pick(out)
        if g and (best is None or _strength(g, measure) > _strength(best[1], measure)):
            best = (gb, g)
    usage = {"llm_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "duration_ms": (time.time() - t0) * 1000}
    if not best:
        record("agent", "report_finding", {}, "No group passes the significance test — the problem is spread evenly across the scope. "
               "Cause unknown: escalating to a human with the evidence gathered rather than guessing.", {})
        return {"type": "unknown", "detail": "no stand-out group"}, usage
    gb, g = best
    if measure == "failures":
        hyp = (f"Failures are concentrated in {gb.replace('_', ' ')} '{g['group']}': {g['failure_rate']*100:.0f}% fail "
               f"vs {g['rest_failure_rate']*100:.1f}% elsewhere — a {g['lift']}× lift.")
    else:
        hyp = f"Slow payments are concentrated in {gb.replace('_', ' ')} '{g['group']}': {g['lat_ratio']}× the payment time of the rest."
    record("agent", "report_finding", {"group_by": gb, "group": g["group"]}, hyp, g)
    return _cause(gb, g, measure, hyp), usage

def _cause(gb, g, measure, hyp):
    return {"type": "novel", "detail": f"{gb}:{g['group']}", "group_by": gb, "group": g["group"], "measure": measure,
            "lift": g["lift"], "lat_ratio": g["lat_ratio"], "failure_rate": g["failure_rate"],
            "rest_failure_rate": g["rest_failure_rate"], "timeouts": g["timeouts"], "failures": g["failures"], "hypothesis": hyp}

TOOL_SPECS = [
    {"name": "query_samples", "description": "Group transaction-level samples in the incident scope since it started, and get failure rate and lift per group.",
     "input_schema": {"type": "object", "properties": {"group_by": {"type": "string", "enum": GROUPS},
         "measure": {"type": "string", "enum": ["failures", "latency"]}}, "required": ["group_by"]}},
    {"name": "dependencies", "description": "Latency of the upstream services this scope depends on, versus the same time on previous days.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "report_finding", "description": "Report your conclusion. Call exactly once, at the end.",
     "input_schema": {"type": "object", "properties": {
         "group_by": {"type": "string", "description": "the field that explains the incident, or 'none'"},
         "group": {"type": "string", "description": "the value of that field, or 'none'"},
         "hypothesis": {"type": "string", "description": "one or two plain-English sentences; quote only numbers returned by tools"},
         "confidence": {"type": "number"}}, "required": ["group_by", "group", "hypothesis", "confidence"]}},
]

def investigate_llm(conn, scope, start, now, record, context, measure="failures"):
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    t0 = time.time(); tin = tout = calls = 0
    system = ("You are Lumio's investigator for a payments monitoring team. A payment incident was detected and scripted checks "
              "(release/version split and upstream dependencies) did not explain it. Use the tools to find which group of payments "
              "explains the failures. Never invent numbers: only quote numbers that tools returned. Be brief. Finish by calling report_finding.")
    msgs = [{"role": "user", "content": "Incident context (computed by code):\n" + json.dumps(context, default=str)}]
    record("agent", "plan", {"model": config.LLM_MODEL}, "Scripted checks did not explain this incident — handing it to Claude with the sample-query tools.", {})
    result = None
    for _ in range(6):
        resp = client.messages.create(model=config.LLM_MODEL, max_tokens=800, system=system, tools=TOOL_SPECS, messages=msgs)
        calls += 1; tin += resp.usage.input_tokens; tout += resp.usage.output_tokens
        msgs.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            break
        results = []
        for u in uses:
            t1 = time.time()
            if u.name == "query_samples":
                finding, out = tools.query_samples(conn, scope, start, now, u.input.get("group_by", "amount_band"), u.input.get("measure", measure))
            elif u.name == "dependencies":
                allsegs = [f"{b}|{c}|{p}|{r}" for b in config.BANKS for c in config.CHANNELS for p in config.PLATFORMS for r in config.REGIONS]
                finding, out = tools.dependencies(conn, tools.segs_matching(scope, allsegs), start, now)
            else:
                result = u.input; finding, out = u.input.get("hypothesis", ""), u.input
            record("agent", u.name, u.input, finding, out, (time.time() - t1) * 1000)
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(out, default=str)[:6000]})
        if result:
            break
        msgs.append({"role": "user", "content": results})
    cost = tin / 1e6 * config.LLM_PRICE_IN + tout / 1e6 * config.LLM_PRICE_OUT
    usage = {"llm_calls": calls, "tokens_in": tin, "tokens_out": tout, "cost_usd": round(cost, 5), "duration_ms": (time.time() - t0) * 1000}
    if not result or result.get("group_by") in (None, "none"):
        return {"type": "unknown", "detail": "agent found no stand-out group",
                "hypothesis": (result or {}).get("hypothesis", "")}, usage
    # verify the agent's claim with code before trusting it
    if result.get("group_by") not in GROUPS:
        return {"type": "unknown", "detail": "agent named an unknown field"}, usage
    _, check = tools.query_samples(conn, scope, start, now, result["group_by"], measure)
    g = next((x for x in check["groups"] if x["group"] == result["group"]), None)
    if not g or not tools.explains(g, measure):
        record("agent", "verify", result, "Code re-checked the agent's claim and it did not pass the significance test — reported as unknown.", check)
        return {"type": "unknown", "detail": "unverified agent hypothesis"}, usage
    record("agent", "verify", result, "Code re-checked the agent's claim: confirmed.", g)
    return _cause(result["group_by"], g, measure, result["hypothesis"]), usage

def investigate(conn, scope, start, now, record, context):
    measure = context.get("measure", "failures")
    if llm_available():
        try:
            return investigate_llm(conn, scope, start, now, record, context, measure) + ("claude",)
        except Exception as e:   # no network, bad key, rate limit… fall back, never fail the incident
            log.warning("Claude agent unavailable (%s) — using offline planner", e)
            record("agent", "fallback", {}, f"Claude unavailable ({type(e).__name__}) — offline planner used instead.", {})
    return investigate_offline(conn, scope, start, now, record, measure) + ("offline",)
