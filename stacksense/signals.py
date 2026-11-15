"""Deterministic keyword signals from project.description (no LLM).

Signals never override structured fields. They (a) add small, explained
tie-break bonuses for things the schema cannot express (GPS data, a ledger,
low-end devices), and (b) raise an info warning when the description implies
a requirement the structured fields left switched off.
"""
import re

# signal -> (regexes, human label)
SIGNALS = {
    "realtime": ([r"\bchat", r"real[- ]?time", r"\blive\b", r"multiplayer", r"collaborat", r"presence",
                  r"live cursor"], "live updates/chat"),
    "media": ([r"upload", r"photo", r"image", r"video", r"\bdocuments?\b", r"\bkyc\b", r"attachment"],
              "file/photo uploads"),
    "search": ([r"\bsearch", r"\bfind\b", r"catalog"], "search"),
    "offline": ([r"offline", r"intermittent", r"\b[23]g\b", r"no (network|signal|internet)",
                 r"syncs? when"], "offline use"),
    "low_bandwidth": ([r"intermittent", r"\b2g\b", r"\b3g\b", r"low bandwidth", r"rural|village"],
                      "poor connectivity"),
    "lowend_device": ([r"low[- ]end", r"\b[12] ?gb ram", r"budget (phone|device)s?", r"entry[- ]level"],
                      "low-end devices"),
    "geo": ([r"\bgps\b", r"geo[- ]?tag", r"\blocations?\b", r"(?<![-\w])maps?\b(?!-)", r"nearby", r"geofenc", r"live tracking"], "location data"),
    "ledger": ([r"ledger", r"double[- ]entry", r"accounting", r"\bwallet", r"reconcil"],
               "money ledger (strong consistency)"),
    "payments": ([r"\bpaid\b", r"get paid", r"payment", r"\bupi\b", r"\bemi\b", r"disburs", r"checkout",
                  r"pay per seat|per[- ]seat"], "payments"),
    "subscription": ([r"per[- ]seat", r"subscription", r"recurring", r"\bplans?\b", r"\bemi\b"],
                     "recurring billing"),
    "ml": ([r"scor(e|ing) model", r"credit scor", r"recommend", r"fraud", r"predict", r"\bml\b",
            r"machine learning", r"\bllm\b", r"\bai\b"], "ML model"),
    "timeseries": ([r"sensor", r"telemetry", r"time[- ]series", r"metrics"], "time-series data"),
    "workflow": ([r"approval", r"workflow", r"assign"], "approval workflow"),
    "internal": ([r"employees?", r"internal", r"staff only", r"used only by"], "internal users"),
    "markdown": ([r"markdown", r"\bblog", r"portfolio"], "Markdown content"),
    "booking": ([r"\bbook(ing|s)?\b", r"appointment", r"schedul"], "bookings/scheduling"),
    "notifications": ([r"notif", r"\bsms\b", r"\botp\b", r"push"], "notifications"),
    "multi_tenant": ([r"teams pay", r"organi[sz]ations", r"workspaces", r"tenants?"], "multiple customer orgs"),
}

# signal -> structured field it implies (for mismatch info warnings)
IMPLIES = {
    "realtime": ("requirements.realtime", True),
    "media": ("requirements.file_media_storage", True),
    "offline": ("requirements.offline_support", True),
    "payments": ("requirements.payments", True),
    "search": ("requirements.full_text_search", True),
}


def extract(description):
    """Return {signal: matched_text} in a stable order."""
    out = {}
    if not description:
        return out
    text = description.lower()
    for name in sorted(SIGNALS):
        for pattern in SIGNALS[name][0]:
            m = re.search(pattern, text)
            if m:
                out[name] = m.group(0)
                break
    return out


def label(signal):
    return SIGNALS[signal][1]
