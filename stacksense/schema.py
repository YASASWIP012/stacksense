"""Input schema, defaults and field-level validation.

The schema below mirrors Section 3 of the StackSense problem statement, plus a
small set of optional "extras" that StackSense collects to sharpen decisions.
Every default that gets applied is recorded as an assumption, so the output
always says what the engine filled in on the user's behalf.
"""

PROJECT_TYPES = [
    "content_site", "web_app", "saas_b2b", "ecommerce", "marketplace", "social",
    "realtime_collab", "fintech", "healthtech", "data_analytics", "iot",
    "ai_ml_product", "internal_tool", "gaming", "api_platform", "other",
]
PLATFORMS = ["web", "android", "ios", "desktop", "api_only"]
TRAFFIC = ["steady", "spiky", "seasonal"]
WORKLOAD = ["read_heavy", "balanced", "write_heavy"]
EXPERIENCE = ["beginner", "intermediate", "senior", "mixed"]
CLOUD = ["any", "aws", "gcp", "azure", "none"]
DEPLOYMENT = ["managed_cloud", "self_hosted_cloud_vm", "on_premise", "air_gapped"]
RESIDENCY = ["none", "india", "eu", "us", "other"]
AUTH = ["none", "email_password", "social", "phone_otp", "enterprise_sso"]
ML = ["none", "inference_api", "llm_rag", "training"]
AVAILABILITY = ["99", "99.9", "99.95", "99.99"]
COMPLIANCE = ["gdpr", "dpdp", "hipaa", "pci_dss", "rbi_localisation", "soc2", "iso27001"]
PRIORITY_KEYS = ["cost", "time_to_market", "scalability", "performance",
                 "maintainability", "security"]

# Extra (optional) inputs StackSense collects beyond the mandatory schema.
GEO_SPREAD = ["single_city", "single_country", "multi_region", "global"]
LOCK_IN = ["low", "medium", "high"]
CONSISTENCY = ["strong", "eventual"]
DATA_SHAPE = ["relational", "document", "key_value", "time_series", "graph", "mixed"]

# (path, type, required, default, allowed/limits, help)
# type: str, enum, enum_list, int, num, bool, str_list, avail
FIELDS = [
    ("project.name", "str", False, None, None, "Project name (free text)"),
    ("project.type", "enum", True, None, PROJECT_TYPES, "Kind of product"),
    ("project.description", "str", False, None, None,
     "Free text; StackSense extracts keyword signals from it (no LLM)"),
    ("project.platforms", "enum_list", True, None, PLATFORMS, "Client platforms (non-empty)"),
    ("scale.users_at_launch", "int", True, None, (0, None), "Users at launch"),
    ("scale.users_12_months", "int", True, None, (0, None), "Users after 12 months"),
    ("scale.peak_concurrent_users", "int", False, None, (0, None), "Simultaneous users at peak"),
    ("scale.traffic_pattern", "enum", False, "steady", TRAFFIC, "Traffic shape"),
    ("scale.workload", "enum", False, "balanced", WORKLOAD, "Read/write mix"),
    ("scale.data_volume_gb_12_months", "num", False, None, (0, None), "Stored data after 12 months (GB)"),
    ("team.size", "int", True, None, (1, None), "Number of engineers"),
    ("team.experience", "enum", True, None, EXPERIENCE, "Team experience"),
    ("team.languages", "str_list", True, None, None, "Languages the team knows (may be empty)"),
    ("team.has_devops", "bool", False, False, None, "Someone can operate infrastructure"),
    ("budget.monthly_infra_usd", "num", True, None, (0, None), "Monthly infra + SaaS budget (USD)"),
    ("timeline.mvp_weeks", "int", True, None, (1, None), "Weeks to first usable release"),
    ("infrastructure.cloud_preference", "enum", False, "any", CLOUD, "Cloud provider constraint"),
    ("infrastructure.deployment_model", "enum", False, "managed_cloud", DEPLOYMENT, "Where it runs"),
    ("infrastructure.data_residency", "enum", False, "none", RESIDENCY, "Where data must stay"),
    ("infrastructure.existing_systems", "str_list", False, [], None, "Systems to integrate with or reuse"),
    ("requirements.realtime", "bool", False, False, None, "Live updates, chat, presence"),
    ("requirements.offline_support", "bool", False, False, None, "Works without connectivity"),
    ("requirements.full_text_search", "bool", False, False, None, "Search over text"),
    ("requirements.file_media_storage", "bool", False, False, None, "User uploads"),
    ("requirements.payments", "bool", False, False, None, "Takes money from users"),
    ("requirements.auth", "enum", False, "none", AUTH, "Authentication method"),
    ("requirements.multi_tenant", "bool", False, False, None, "Several customer orgs share the system"),
    ("requirements.ml_features", "enum", False, "none", ML, "ML needs"),
    ("requirements.seo_critical", "bool", False, False, None, "Public pages must rank"),
    ("requirements.availability", "avail", False, "99.9", AVAILABILITY, "Availability target (%)"),
    ("requirements.p95_latency_ms", "int_or_null", False, None, (1, None), "Target p95 latency (ms)"),
    ("compliance", "enum_list", False, [], COMPLIANCE, "Compliance regimes"),
    # --- StackSense extras (all optional) ---
    ("extras.geo_spread", "enum", False, None, GEO_SPREAD,
     "Where users are: drives CDN/edge and multi-region choices"),
    ("extras.vendor_lock_in_tolerance", "enum", False, "medium", LOCK_IN,
     "How acceptable proprietary services (DynamoDB, Firebase) are"),
    ("extras.consistency", "enum", False, None, CONSISTENCY,
     "Strong (money, inventory) vs eventual consistency"),
    ("extras.data_shape", "enum", False, None, DATA_SHAPE, "Dominant data model"),
    ("extras.team_growth_12_months", "int", False, None, (0, None),
     "Engineers expected to join within 12 months (hiring pool matters)"),
    ("extras.admin_panel_needed", "bool", False, None, None,
     "Staff need a back-office/admin UI"),
    ("extras.peak_requests_per_sec", "num", False, None, (0, None),
     "Peak API requests per second (otherwise estimated)"),
]

FIELD_INDEX = {f[0]: f for f in FIELDS}
KNOWN_SECTIONS = {"scenario_id", "project", "scale", "team", "budget", "timeline",
                  "infrastructure", "requirements", "compliance", "priorities", "extras"}


def _get(d, path):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _is_int(v):
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return isinstance(v, float) and v.is_integer()


def _is_num(v):
    return not isinstance(v, bool) and isinstance(v, (int, float))


def validate(raw):
    """Validate and normalise a scenario.

    Returns (spec, errors, assumptions, notes). `spec` is a flat dict keyed by
    field path with defaults applied. `notes` are non-fatal warnings
    (unknown fields, coercions) as warning dicts.
    """
    errors, assumptions, notes = [], [], []
    if not isinstance(raw, dict):
        return None, [{"field": "$", "message": "Scenario must be a JSON object."}], [], []

    for key in raw:
        if key not in KNOWN_SECTIONS:
            notes.append({"code": "UNKNOWN_FIELD", "severity": "info",
                          "message": "Ignored unknown top-level field '%s'." % key,
                          "fields": [key]})
    for section in ("project", "scale", "team", "budget", "timeline", "infrastructure",
                    "requirements", "priorities", "extras"):
        if section in raw and not isinstance(raw[section], dict):
            errors.append({"field": section, "message": "'%s' must be an object." % section})
            continue
        for sub in (raw.get(section) or {}):
            path = "%s.%s" % (section, sub)
            if section == "priorities":
                continue
            if path not in FIELD_INDEX:
                notes.append({"code": "UNKNOWN_FIELD", "severity": "info",
                              "message": "Ignored unknown field '%s'." % path, "fields": [path]})

    spec = {}
    for path, ftype, required, default, allowed, _help in FIELDS:
        present, val = _get(raw, path)
        if present and val is None and ftype not in ("int_or_null", "str"):
            present = False
        if not present:
            if required:
                errors.append({"field": path, "message": "Required field is missing."})
                continue
            if default is not None or ftype == "int_or_null":
                spec[path] = list(default) if isinstance(default, list) else default
                if not path.startswith("extras.") and path != "requirements.p95_latency_ms":
                    assumptions.append("%s not given; defaulted to %s." % (path, _fmt(default)))
            else:
                spec[path] = None
            continue
        err, val = _check(path, ftype, val, allowed, notes)
        if err:
            errors.append({"field": path, "message": err})
        else:
            spec[path] = val

    # priorities
    pri = raw.get("priorities", {}) if isinstance(raw.get("priorities", {}), dict) else {}
    for key in pri:
        if key not in PRIORITY_KEYS:
            errors.append({"field": "priorities.%s" % key,
                           "message": "Unknown priority key. Allowed: %s." % ", ".join(PRIORITY_KEYS)})
    missing_pri = []
    for key in PRIORITY_KEYS:
        path = "priorities.%s" % key
        if key not in pri or pri[key] is None:
            spec[path] = 3
            missing_pri.append(key)
            continue
        v = pri[key]
        if not _is_int(v):
            errors.append({"field": path, "message": "Priority must be an integer from 1 to 5."})
        elif not 1 <= int(v) <= 5:
            errors.append({"field": path, "message": "Priority %s is out of range 1-5." % v})
        else:
            spec[path] = int(v)
    if missing_pri:
        assumptions.append("Priorities not given for %s; defaulted to 3." % ", ".join(missing_pri))

    # cross-field consistency checks that make the input invalid
    if not errors:
        if spec["scale.users_12_months"] is not None and spec.get("scale.peak_concurrent_users"):
            top = max(spec["scale.users_12_months"], spec["scale.users_at_launch"])
            if spec["scale.peak_concurrent_users"] > top and top > 0:
                notes.append({"code": "PEAK_EXCEEDS_USERS", "severity": "warning",
                              "message": "Peak concurrent users (%d) exceed total users (%d); treating the "
                                         "peak as the larger audience figure." % (
                                             spec["scale.peak_concurrent_users"], top),
                              "fields": ["scale.peak_concurrent_users", "scale.users_12_months"]})
        plats = spec["project.platforms"]
        if "api_only" in plats and len(plats) > 1:
            notes.append({"code": "API_ONLY_WITH_CLIENTS", "severity": "warning",
                          "message": "'api_only' was combined with client platforms; the client platforms "
                                     "win and the API is treated as public as well.",
                          "fields": ["project.platforms"]})
    return spec, errors, assumptions, notes


def _fmt(v):
    if isinstance(v, str):
        return '"%s"' % v
    if isinstance(v, bool):
        return "true" if v else "false"
    if v == []:
        return "[]"
    return str(v)


def _check(path, ftype, val, allowed, notes):
    if ftype == "str":
        if val is None:
            return None, None
        if not isinstance(val, str):
            return "Must be a string.", None
        return None, val.strip()
    if ftype == "enum":
        if not isinstance(val, str):
            return "Must be one of: %s." % ", ".join(allowed), None
        v = val.strip().lower()
        if v not in allowed:
            return "Unknown value '%s'. Allowed: %s." % (val, ", ".join(allowed)), None
        return None, v
    if ftype == "enum_list":
        if not isinstance(val, list):
            return "Must be a list of: %s." % ", ".join(allowed), None
        out = []
        for item in val:
            if not isinstance(item, str) or item.strip().lower() not in allowed:
                return "Unknown value %r. Allowed: %s." % (item, ", ".join(allowed)), None
            v = item.strip().lower()
            if v not in out:
                out.append(v)
        if path == "project.platforms" and not out:
            return "Must contain at least one platform.", None
        return None, out
    if ftype == "str_list":
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            return "Must be a list of strings.", None
        out = []
        for x in val:
            v = x.strip() if path == "infrastructure.existing_systems" else x.strip().lower()
            if v and v not in out:
                out.append(v)
        return None, out
    if ftype == "bool":
        if not isinstance(val, bool):
            return "Must be true or false (JSON boolean).", None
        return None, val
    if ftype in ("int", "int_or_null"):
        if val is None and ftype == "int_or_null":
            return None, None
        if not _is_int(val):
            return "Must be an integer.", None
        lo, hi = allowed
        v = int(val)
        if lo is not None and v < lo:
            return "Must be >= %d (got %d)." % (lo, v), None
        if hi is not None and v > hi:
            return "Must be <= %d (got %d)." % (hi, v), None
        return None, v
    if ftype == "num":
        if not _is_num(val):
            return "Must be a number.", None
        lo, hi = allowed
        if lo is not None and val < lo:
            return "Must be >= %s (got %s)." % (lo, val), None
        return None, float(val) if not float(val).is_integer() else int(val)
    if ftype == "avail":
        if _is_num(val):
            s = ("%g" % val)
            if s in allowed:
                notes.append({"code": "COERCED_VALUE", "severity": "info",
                              "message": "availability given as a number; read as \"%s\"." % s,
                              "fields": [path]})
                return None, s
        if isinstance(val, str) and val.strip() in allowed:
            return None, val.strip()
        return "Must be one of: %s (string)." % ", ".join('"%s"' % a for a in allowed), None
    return "Unsupported field type.", None


def schema_for_ui():
    """Field metadata for the web form."""
    out = []
    for path, ftype, required, default, allowed, help_ in FIELDS:
        out.append({"path": path, "type": ftype, "required": required, "default": default,
                    "allowed": allowed if isinstance(allowed, list) else None,
                    "min": allowed[0] if isinstance(allowed, tuple) else None, "help": help_})
    for key in PRIORITY_KEYS:
        out.append({"path": "priorities.%s" % key, "type": "priority", "required": False,
                    "default": 3, "allowed": None, "min": 1, "help": "1 (low) to 5 (high)"})
    return out
