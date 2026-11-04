"""Settings stored in the database and editable in the dashboard: alert rules, email server, departments."""
import json, copy
from sqlalchemy import select, insert, update
from . import config, db

DEPARTMENTS = {
    "duty":     {"name": "Duty Manager (Incident Commander)", "lead": "Priya Nair",      "tag": "duty"},
    "support":  {"name": "Customer Support",                  "lead": "Marcus Lee",      "tag": "support"},
    "mobile":   {"name": "Mobile App Engineering",            "lead": "Aisha Khan",      "tag": "mobile"},
    "release":  {"name": "Release Management",                "lead": "Tom Becker",      "tag": "release"},
    "sre":      {"name": "Site Reliability Engineering",      "lead": "Diego Alvarez",   "tag": "sre"},
    "fraud":    {"name": "Fraud & Risk Operations",           "lead": "Hannah Cho",      "tag": "fraud"},
    "cards":    {"name": "Card Operations & Partners",        "lead": "Ravi Menon",      "tag": "cards"},
    "corebank": {"name": "Core Banking Platform",             "lead": "Olivia Grant",    "tag": "corebank"},
    "network":  {"name": "Network Operations",                "lead": "Samuel Osei",     "tag": "network"},
    "product":  {"name": "Payments Product",                  "lead": "Leah Fischer",    "tag": "product"},
    "data":     {"name": "Monitoring & Data Engineering",     "lead": "Kenji Watanabe",  "tag": "data"},
    "bizops":   {"name": "Business Operations",               "lead": "Fatima Rahman",   "tag": "bizops"},
}

PRESETS = {
    "gmail":   {"host": "smtp.gmail.com", "port": 587, "security": "starttls", "help": "Use a Google App Password (Google Account → Security → 2-Step Verification → App passwords)."},
    "outlook": {"host": "smtp-mail.outlook.com", "port": 587, "security": "starttls", "help": "Outlook.com / Hotmail. Some accounts need an app password."},
    "office365": {"host": "smtp.office365.com", "port": 587, "security": "starttls", "help": "Microsoft 365 work account; SMTP AUTH must be enabled by your admin."},
    "custom":  {"host": "", "port": 587, "security": "starttls", "help": "Any SMTP server, e.g. Mailtrap, SendGrid, Brevo."},
}

DEFAULTS = {
    "rules": dict(config.DEFAULT_RULES),
    "email": {"enabled": False, "preset": "gmail", "host": "smtp.gmail.com", "port": 587, "security": "starttls",
              "username": "", "password": "", "from_name": "Lumio Alerts", "from_addr": "",
              "demo_mode": True, "demo_inbox": "", "plus_tags": True, "dashboard_url": "http://localhost:8000"},
    "departments": {k: {"name": v["name"], "lead": v["lead"], "email": ""} for k, v in DEPARTMENTS.items()},
}

def get(section):
    with db.engine().connect() as c:
        r = c.execute(select(db.settings.c.value).where(db.settings.c.key == section)).first()
    val = copy.deepcopy(DEFAULTS[section])
    if r:
        stored = json.loads(r[0])
        if section == "departments":
            for k, v in stored.items():
                if k in val:
                    val[k].update(v)
        else:
            val.update(stored)
    return val

def put(section, value):
    cur = get(section)
    if section == "departments":
        for k, v in value.items():
            if k in cur:
                cur[k].update({kk: vv for kk, vv in v.items() if kk in ("email", "lead")})
    else:
        cur.update({k: v for k, v in value.items() if k in DEFAULTS[section]})
        if section == "rules":
            for k, v in DEFAULTS["rules"].items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    cur[k] = type(v)(cur[k])
    with db.engine().begin() as c:
        if c.execute(select(db.settings.c.key).where(db.settings.c.key == section)).first():
            c.execute(update(db.settings).where(db.settings.c.key == section).values(value=json.dumps(cur)))
        else:
            c.execute(insert(db.settings).values(key=section, value=json.dumps(cur)))
    return cur

def public_email():
    e = get("email")
    e = dict(e); e["password_set"] = bool(e.get("password")); e["password"] = ""
    return e

def address_for(dept_key):
    """Where a department's email goes. In demo mode everything lands in one inbox, tagged by department
    (you+sre@gmail.com), so the audience can see the routing in a single mailbox."""
    e = get("email"); d = get("departments")[dept_key]
    if e.get("demo_mode") and e.get("demo_inbox"):
        inbox = e["demo_inbox"].strip()
        if e.get("plus_tags") and "@" in inbox:
            local, dom = inbox.split("@", 1)
            return f"{local}+{DEPARTMENTS[dept_key]['tag']}@{dom}"
        return inbox
    return (d.get("email") or "").strip()
