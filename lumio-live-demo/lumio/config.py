"""Central configuration. Every value can be overridden with an environment variable."""
import os
from datetime import datetime

def _env(name, default):
    return os.environ.get(name, default)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

# Database: SQLite by default (zero setup). Docker Compose sets a PostgreSQL URL.
DATABASE_URL = _env("LUMIO_DATABASE_URL", "sqlite:///" + os.path.join(ROOT, "lumio.db"))

# The demo dataset: 10 calendar days = 6 business days (Fri 7, Mon 10–Fri 14 Feb) + 2 weekends
BUCKET_MIN = 5                     # metrics arrive in 5-minute buckets
SCAN_MIN = 15                      # Lumio scans every 15 minutes
HISTORY_START = datetime(2025, 1, 17, 0, 0)    # 3 weeks of history the model learns 'normal' from
LIVE_START = datetime(2025, 2, 7, 0, 0)        # Friday 7 February 2025
LIVE_END = datetime(2025, 2, 16, 23, 55)       # Sunday 16 February 2025
DAYS = 10
SECONDS_PER_BUCKET = float(_env("LUMIO_SECONDS_PER_BUCKET", "5.0"))   # at 1x: 5 simulated minutes every 5 seconds
SPEEDS = [1, 2, 4, 6, 8, 10]

# Detection defaults (editable live in Settings → Alert rules)
DEFAULT_RULES = {
    "sensitivity": 4.5,          # how many 'normal spreads' a segment must move before it is abnormal
    "volume_sensitivity": 6.0,   # volume is naturally noisier
    "confirm_scans": 2,          # must stay abnormal this many scans before alerting
    "success_floor": 94.0,       # business guardrail: success rate below this always alerts (%)
    "latency_ceiling": 8.0,      # business guardrail: payment time above this always alerts (s)
    "high_value_at_risk": 50000, # value at risk that makes an incident 'high' severity ($)
    "email_min_severity": "medium",
    "email_resolved": True,
    "email_escalation": True,
    "escalate_after_min": 30,    # simulated minutes without acknowledgement before escalating
    "daily_report": True,        # end-of-business-day report email at 18:00
    "email_tickets": True,       # data-quality tickets to Monitoring & Data Engineering
    "email_advisories": True,    # system-wide demand advisories to Business Operations
}
RESOLVE_SCANS = 2
DQ_MIN_COMPLETENESS = 0.67

# AI agent (optional). Without a key Lumio runs a deterministic offline planner with the same tools.
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
LLM_MODEL = _env("LUMIO_LLM_MODEL", "claude-sonnet-5-5")
LLM_PRICE_IN = float(_env("LUMIO_LLM_PRICE_IN", "3.0"))
LLM_PRICE_OUT = float(_env("LUMIO_LLM_PRICE_OUT", "15.0"))

INGEST_URL = _env("LUMIO_INGEST_URL", "")
MODEL_DIR = _env("LUMIO_MODEL_DIR", os.path.join(ROOT, "models"))

# The payment world
BANKS = ["Bank X", "Bank Y", "Bank Z", "Bank W"]
CHANNELS = ["UPI", "Card"]
PLATFORMS = ["Android", "iOS"]
REGIONS = ["North", "South", "West"]
DIMS = ["bank", "channel", "platform", "region"]
CORE = {b: "core-banking-" + b.split()[-1] for b in BANKS}          # each bank has its own core banking system
SERVICES = {"UPI": ["upi-switch"], "Card": ["card-network", "fraud-check"]}
ALL_SERVICES = ["upi-switch", "card-network", "fraud-check"] + list(CORE.values())

def services_for(channel, bank):
    return SERVICES[channel] + [CORE[bank]]
