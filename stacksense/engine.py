"""StackSense decision engine.

Pipeline: validate -> build context -> hard filters -> unary scoring per layer
-> joint search over layer combinations with compatibility rules -> realise
managed services in the right region -> supporting services -> conflicts and
warnings -> cost band -> explained output.

Everything is deterministic: no randomness, no clock, no network, stable
sort keys (score, then id) everywhere.
"""
import hashlib
import itertools
import json
import re

from . import kb, schema
from . import signals as sigmod
from .supporting import choose_supporting
from .rules import conflicts, eol_warnings, existing_hints
from .cost import db_service_cost, estimate

ENGINE_VERSION = "1.0.0"
COMMERCIAL_TYPES = {"saas_b2b", "ecommerce", "marketplace", "fintech", "healthtech", "api_platform",
                    "gaming", "social", "ai_ml_product", "web_app", "realtime_collab"}
SERVER_BACKENDS = {"nestjs", "fastify", "django", "fastapi", "spring_boot", "aspnet", "go_chi", "laravel",
                   "rails", "phoenix"}
PRI_ATTR = [("cost", "cheap"), ("time_to_market", "ttm"), ("scalability", "scale"),
            ("performance", "perf"), ("maintainability", "maint"), ("security", "sec")]
HARD_COMPLIANCE = {"pci_dss", "hipaa", "soc2", "iso27001"}
LANG_LABEL = {"csharp": "C#", "javascript": "JavaScript", "typescript": "TypeScript", "python": "Python",
              "java": "Java", "kotlin": "Kotlin", "go": "Go", "php": "PHP", "ruby": "Ruby", "dart": "Dart",
              "elixir": "Elixir", "swift": "Swift", "rust": "Rust", "sql": "SQL", "html": "HTML"}


class Ctx(dict):
    __getattr__ = dict.get


def scale_level(users, peak):
    if users <= 5000 and peak <= 200:
        return 1
    if users <= 50000 and peak <= 2000:
        return 2
    if users <= 500000 and peak <= 20000:
        return 3
    if users <= 5000000 and peak <= 200000:
        return 4
    return 5


def _norm_lang(x):
    x = x.strip().lower()
    return kb.LANG_ALIASES.get(x, x)


# ------------------------------------------------------------------ context
def build_ctx(spec, raw, assumptions):
    s = spec
    c = Ctx()
    c.spec = s
    c.type = s["project.type"]
    c.platforms = s["project.platforms"]
    c.users = max(s["scale.users_12_months"], s["scale.users_at_launch"])
    peak = s.get("scale.peak_concurrent_users")
    if peak is None:
        ratio = 0.25 if c.type == "internal_tool" else (0.05 if c.type in ("content_site", "social") else 0.1)
        peak = max(1, int(round(c.users * ratio)))
        assumptions.append("scale.peak_concurrent_users not given; estimated as %d (%d%% of %d users)."
                           % (peak, int(ratio * 100), c.users))
    c.peak = peak
    c.users = max(c.users, peak)
    data = s.get("scale.data_volume_gb_12_months")
    if data is None and c.type == "content_site" and not s["requirements.file_media_storage"]:
        data = 1
        assumptions.append("scale.data_volume_gb_12_months not given; a content site is assumed to store ~1 GB.")
    if data is None:
        per_user_mb = 2 + (50 if s["requirements.file_media_storage"] else 0)
        data = round(c.users * per_user_mb / 1024.0, 1)
        assumptions.append("scale.data_volume_gb_12_months not given; estimated %.1f GB (%d MB per user)."
                           % (data, per_user_mb))
    c.data_gb = data
    rps = s.get("extras.peak_requests_per_sec")
    if rps is None:
        rps = max(1, int(round(peak * 0.2)))
    c.rps = rps
    c.L = scale_level(c.users, c.peak)
    c.traffic = s["scale.traffic_pattern"]
    c.workload = s["scale.workload"]
    c.langs = sorted({_norm_lang(x) for x in s["team.languages"]})
    c.exp = s["team.experience"]
    c.size = s["team.size"]
    c.devops = s["team.has_devops"]
    c.budget = s["budget.monthly_infra_usd"]
    c.mvp = s["timeline.mvp_weeks"]
    c.cloud = s["infrastructure.cloud_preference"]
    c.deploy = s["infrastructure.deployment_model"]
    c.residency = s["infrastructure.data_residency"]
    c.existing = s["infrastructure.existing_systems"]
    c.compliance = list(s["compliance"])
    c.req = {k.split(".", 1)[1]: v for k, v in s.items() if k.startswith("requirements.")}
    c.pri = {k: s["priorities.%s" % k] for k in schema.PRIORITY_KEYS}
    c.extras = {k.split(".", 1)[1]: v for k, v in s.items() if k.startswith("extras.")}
    c.signals = sigmod.extract(s.get("project.description"))
    c.hints = existing_hints(c.existing)
    c.commercial = c.type in COMMERCIAL_TYPES or bool(c.req["payments"])
    c.needs_backend = backend_needs(c)
    c.strong = (c.req["payments"] or c.type in ("fintech", "ecommerce", "marketplace", "healthtech")
                or "ledger" in c.signals or c.extras.get("consistency") == "strong")
    if c.extras.get("consistency") == "eventual" and not c.req["payments"]:
        c.strong = False
    c.eff_residency = c.residency
    if "rbi_localisation" in c.compliance and c.residency in ("none",):
        c.eff_residency = "india"
        assumptions.append("rbi_localisation requires payment data to be stored only in India; data "
                           "residency treated as 'india'.")
    c.is_static = (c.type == "content_site" and not c.needs_backend)
    c.lockin = c.extras.get("vendor_lock_in_tolerance") or "medium"
    return c


def backend_needs(c):
    r = c.req
    needs = []
    if r["auth"] != "none":
        needs.append("requirements.auth")
    for k in ("payments", "realtime", "file_media_storage", "multi_tenant", "full_text_search",
              "offline_support"):
        if r[k]:
            needs.append("requirements.%s" % k)
    if r["ml_features"] != "none":
        needs.append("requirements.ml_features")
    if c.type not in ("content_site",):
        needs.append("project.type")
    if any(p in c.platforms for p in ("android", "ios", "api_only")):
        needs.append("project.platforms")
    return needs


# ------------------------------------------------------------------ helpers
def _lang_weight(c):
    w = {"beginner": 3.0, "intermediate": 2.2, "mixed": 2.2, "senior": 1.5}[c.exp]
    if c.size <= 2:
        w *= 1.2
    if c.mvp <= 4:
        w *= 1.2
    return w


def lang_fit(cand, c, contribs, layer):
    langs = set(cand["langs"]) - {"html", "css"}
    if layer != "database":
        langs -= {"sql"}
    if not langs or not c.langs:
        return
    team = set(c.langs)
    w = _lang_weight(c)
    if layer == "database":
        if "sql" in langs and "sql" in team:
            contribs.append(("team.languages", 0.6, "the team already writes SQL"))
        if "csharp" in langs and "csharp" in team:
            contribs.append(("team.languages", 0.5, "first-class .NET tooling for a C# team"))
        return
    hit = sorted(langs & team)
    if hit:
        contribs.append(("team.languages", w, "team.languages includes %s" % "/".join(LANG_LABEL.get(h, h) for h in hit)))
        return
    fam = {kb.LANG_FAMILY.get(x) for x in langs} - {None}
    tfam = {kb.LANG_FAMILY.get(x) for x in team} - {None}
    if fam & tfam:
        contribs.append(("team.languages", 0.6 * w, "close to a language the team knows (%s)" %
                         "/".join(LANG_LABEL.get(x, x) for x in sorted(team) if kb.LANG_FAMILY.get(x) in fam)))
        return
    need = "/".join(LANG_LABEL.get(x, x) for x in sorted(langs))
    if layer in ("web", "mobile") and langs <= {"javascript", "typescript"}:
        w *= 0.5  # JavaScript is the lingua franca of UI work; cheaper to pick up than a niche language
    contribs.append(("team.languages", -w, "the team would have to learn %s" % need))


def provider_check(pid, c, role):
    """Return None if provider pid satisfies hard constraints, else (field, reason)."""
    if pid is None:
        return None
    p = kb.PROVIDERS[pid]
    if c.cloud in ("aws", "gcp", "azure") and pid not in (c.cloud, "self"):
        return ("infrastructure.cloud_preference",
                "%s is not on %s, and cloud_preference=%s is a hard constraint" % (p["name"], c.cloud.upper(), c.cloud))
    if role == "data" and c.eff_residency in ("india", "eu", "us") and c.eff_residency not in p["regions"]:
        return ("infrastructure.data_residency",
                "%s has no %s region for personal data" % (p["name"], c.eff_residency.upper() if c.eff_residency != "india" else "India"))
    if role == "data":
        missing = sorted(x for x in c.compliance if x in HARD_COMPLIANCE and x not in p["compliance"])
        if missing:
            return ("compliance", "%s lacks %s attestation/BAA" % (p["name"], ", ".join(m.upper() for m in missing)))
    return None


def resolve_cloud(c):
    """Cloud used for generic VM/Kubernetes hosting."""
    if c.cloud in ("aws", "gcp", "azure"):
        return c.cloud
    hinted = [v for k, v in c.hints if k == "cloud"]
    if hinted:
        return sorted(hinted)[0]
    if c.compliance and any(x in HARD_COMPLIANCE for x in c.compliance):
        return "aws"
    return "digitalocean"


def host_provider(h, c):
    if h["provider"] == "cloud":
        cl = resolve_cloud(c)
        if h["id"] == "k8s" and cl == "digitalocean":
            return "digitalocean"
        return cl
    return h["provider"]


# ------------------------------------------------------------------ unary scoring
def score(cand, layer, c):
    """Return (score, contribs, exclusion) for one candidate."""
    contribs = []
    excl = None
    cid = cand["id"]
    if c.deploy not in cand["deploy"]:
        if c.deploy in ("on_premise", "air_gapped"):
            msg = "is a managed cloud service and cannot run %s" % c.deploy.replace("_", "-")
        else:
            msg = "is not suited to deployment_model=%s" % c.deploy
        excl = ("infrastructure.deployment_model", "%s %s" % (cand["name"], msg))
    if cand.get("provider") and cand["provider"] not in ("cloud",) and layer in ("backend", "database"):
        bad = provider_check(cand["provider"], c, "data")
        if bad and not excl:
            excl = bad
    if layer == "hosting" and not excl and cand["provider"] not in ("cloud", "self"):
        bad = provider_check(cand["provider"], c, "host")
        if bad:
            excl = bad
    if layer == "database" and cand.get("clouds") and c.cloud in ("aws", "gcp", "azure") and not excl:
        if c.cloud not in cand["clouds"] and "self" not in cand["clouds"]:
            excl = ("infrastructure.cloud_preference", "%s is not available on %s" % (cand["name"], c.cloud.upper()))
    if c.lockin == "low" and cand.get("proprietary"):
        contribs.append(("extras.vendor_lock_in_tolerance", -2.5, "proprietary service and lock-in tolerance is low"))
    elif c.lockin == "medium" and cand.get("proprietary"):
        contribs.append(("extras.vendor_lock_in_tolerance", -0.6, "proprietary API increases lock-in"))
    elif c.lockin == "high" and cand.get("proprietary"):
        contribs.append(("extras.vendor_lock_in_tolerance", 0.4, "lock-in is acceptable here"))

    lang_fit(cand, c, contribs, layer)
    for key, attr in PRI_ATTR:
        v = c.pri[key]
        d = 0.35 * (v - 1) / 2.0 * (cand[attr] - 3)
        if abs(d) >= 0.3:
            word = {"cheap": "cost", "ttm": "time to market", "scale": "scalability", "perf": "performance",
                    "maint": "maintainability", "sec": "security"}[attr]
            contribs.append(("priorities.%s" % key, d, "%s %s for %s (priority %d)" % (
                "strong" if d > 0 else "weaker", word, cand["name"].split(" (")[0], v)))
    if cand["ops"] > 1:
        mult = (0.25 if c.devops else 0.9) * (1.3 if c.size <= 2 else 1.0)
        d = -(cand["ops"] - 1) * mult
        contribs.append(("team.has_devops", d, "operational burden (ops %d/5)%s" % (
            cand["ops"], "" if c.devops else " with no DevOps engineer")))
    lm = {"beginner": 0.8, "mixed": 0.45, "intermediate": 0.3, "senior": 0.1}[c.exp] * (1.3 if c.mvp <= 4 else 1)
    if cand["learn"] > 2:
        contribs.append(("team.experience", -(cand["learn"] - 2) * lm, "learning curve %d/5 for a %s team" % (cand["learn"], c.exp)))
    if cand["maturity"] < 4:
        contribs.append(("priorities.maintainability", -0.5 * (4 - cand["maturity"]), "younger ecosystem and smaller hiring pool"))
    aff = cand["types"].get(c.type, 0)
    if aff:
        contribs.append(("project.type", 0.6 * aff, "well suited to %s projects" % c.type.replace("_", " ")))
    if cand["scale"] < c.L - 1:
        contribs.append(("scale.users_12_months", -1.5 * (c.L - 1 - cand["scale"]),
                         "scale ceiling is low for %d users / %d concurrent" % (c.users, c.peak)))
    team_growth = c.extras.get("team_growth_12_months") or 0
    if team_growth >= 3 and cand["maint"] >= 4 and cand["maturity"] >= 4:
        contribs.append(("extras.team_growth_12_months", 0.5, "large hiring pool for %d planned hires" % team_growth))

    fn = {"web": _web, "mobile": _mobile, "desktop": _desktop, "backend": _backend,
          "database": _database, "hosting": _hosting}[layer]
    e2 = fn(cand, c, contribs)
    if e2 and not excl:
        excl = e2
    if layer in ("hosting", "backend") and cand["cost"] is not kb.FREE:
        _budget_term(cand, c, contribs, 0.6)
    total = sum(d for _, d, _ in contribs)
    return total, contribs, excl


def _budget_term(cand, c, contribs, share_frac):
    mn = cand["cost"][c.L][0]
    if cand["id"] == "vercel" and not c.commercial and c.L == 1:
        mn = 0
    share = c.budget * share_frac
    if mn <= 0:
        if c.budget == 0:
            contribs.append(("budget.monthly_infra_usd", 1.0, "fits a $0 budget on the free tier"))
        return
    if c.budget == 0:
        contribs.append(("budget.monthly_infra_usd", -5.0, "costs at least $%d/month but the budget is $0" % mn))
    elif mn > share:
        d = -min(5.0, 2.5 * (mn / float(share) - 1))
        contribs.append(("budget.monthly_infra_usd", d, "starts around $%d/month against a $%g budget" % (mn, c.budget)))


def _web(cand, c, contribs):
    r = c.req
    if r["seo_critical"]:
        if cand["caps"] & {"ssr", "ssg"}:
            contribs.append(("requirements.seo_critical", 1.2, "seo_critical=true and it serves pre-rendered HTML"))
        else:
            contribs.append(("requirements.seo_critical", -4.0, "client-rendered pages are weak for SEO"))
    app_like = (r["auth"] != "none" or r["realtime"] or r["multi_tenant"] or r["payments"]
                or c.type not in ("content_site",))
    if app_like and "app" not in cand["caps"]:
        if "app_lite" in cand["caps"]:
            if r["realtime"] or c.type in ("realtime_collab", "gaming"):
                contribs.append(("requirements.realtime", -2.5, "server-rendered pages struggle with live, canvas-style UIs"))
        else:
            contribs.append(("project.type", -3.5, "built for content sites, not interactive app screens"))
    if c.type == "content_site" and "content" in cand["caps"]:
        contribs.append(("project.type", 1.0, "content-first: ships zero JavaScript by default"))
        if "markdown" in c.signals:
            contribs.append(("project.description", 0.5, "description mentions Markdown/blog content (native support)"))
    if not app_like and "app" in cand["caps"] and "content" not in cand["caps"]:
        contribs.append(("project.type", -0.8, "app framework is heavier than this content site needs"))
    if c.extras.get("geo_spread") == "global" and "ssg" in cand["caps"]:
        contribs.append(("extras.geo_spread", 0.4, "static pages cache well on a global CDN"))
    return None


def _mobile(cand, c, contribs):
    r = c.req
    need_a, need_i = "android" in c.platforms, "ios" in c.platforms
    caps = cand["caps"]
    if need_a and "android" not in caps:
        return ("project.platforms", "%s does not target Android" % cand["name"])
    if need_i and not (caps & {"ios", "ios_limited"}):
        return ("project.platforms", "%s does not target iOS" % cand["name"])
    if need_i and "ios_limited" in caps:
        contribs.append(("project.platforms", -3.0, "iOS restricts PWAs (no reliable background sync)"))
    if r["offline_support"]:
        if "offline" in caps:
            contribs.append(("requirements.offline_support", 1.0, "on-device SQLite storage with a sync queue"))
        elif "offline_light" in caps:
            d = -3.5 if r["file_media_storage"] else -2.0
            contribs.append(("requirements.offline_support", d,
                             "browser storage can be evicted and background upload of photos is unreliable"))
    if "lowend_device" in c.signals or "low_bandwidth" in c.signals:
        d = (cand["lowend"] - 3) * 0.8
        if d:
            contribs.append(("project.description", d, "description mentions %s (%s)" % (
                "low-end devices" if "lowend_device" in c.signals else "poor connectivity",
                "light on memory" if d > 0 else "heavier runtime")))
    if "web" in c.platforms and cand["id"] in ("pwa", "capacitor"):
        contribs.append(("project.platforms", 0.8, "reuses the web codebase"))
    if (r["file_media_storage"] or "geo" in c.signals) and not (caps & {"camera", "gps"}):
        contribs.append(("requirements.file_media_storage", -1.0, "limited camera/GPS access"))
    return None


def _desktop(cand, c, contribs):
    return None


def _backend(cand, c, contribs):
    r = c.req
    cid = cand["id"]
    caps = cand["caps"]
    if cid == "none":
        if c.needs_backend:
            return ("requirements", "a backend is needed for %s" % ", ".join(sorted(set(c.needs_backend))[:3]))
        contribs.append(("project.type", 3.0, "static content needs no server: nothing to run, patch or pay for"))
        return None
    if c.is_static:
        contribs.append(("project.type", -3.0, "a server is unnecessary for a static content site"))
    if r["realtime"]:
        if "realtime" in caps:
            contribs.append(("requirements.realtime", 1.2, "native WebSocket/real-time support"))
        elif "realtime_partial" in caps:
            contribs.append(("requirements.realtime", 0.2, "real-time needs an add-on (Channels)"))
        else:
            contribs.append(("requirements.realtime", -1.2, "no long-lived connections; a separate real-time service is needed"))
    if c.strong and "transactions" in caps:
        contribs.append(("requirements.payments" if r["payments"] else "project.type", 0.8,
                         "mature transaction management for money/ledger flows"))
    if "ledger" in c.signals and "transactions" in caps:
        contribs.append(("project.description", 0.6, "description mentions a %s" % c.signals["ledger"]))
    admin = c.extras.get("admin_panel_needed")
    if admin is None:
        admin = c.type in ("internal_tool", "marketplace", "ecommerce", "healthtech")
    if admin and "admin" in caps:
        contribs.append(("extras.admin_panel_needed" if c.extras.get("admin_panel_needed") else "project.type", 1.0,
                         "built-in admin/back-office screens"))
    if r["ml_features"] != "none":
        if "python" in cand["langs"]:
            contribs.append(("requirements.ml_features", 1.0, "ml_features=%s fits the Python ML ecosystem" % r["ml_features"]))
        elif cid not in ("supabase", "firebase"):
            contribs.append(("requirements.ml_features", -0.3, "ML model runs as a separate Python service"))
    if r["auth"] == "enterprise_sso" and "entra" in caps and any(v == "entra" for k, v in c.hints if k == "idp"):
        contribs.append(("infrastructure.existing_systems", 1.5, "first-class Microsoft Entra ID integration"))
    if r["auth"] != "none" and caps & {"auth"}:
        contribs.append(("requirements.auth", 0.4, "built-in authentication"))
    if "baas" in caps:
        if c.type in ("fintech", "healthtech") or "pci_dss" in c.compliance or "hipaa" in c.compliance:
            contribs.append(("compliance", -2.5, "audited money/health logic spread across RLS and functions is hard to review"))
        if c.L >= 4:
            contribs.append(("scale.users_12_months", -1.5, "large scale outgrows a single BaaS project"))
        if c.size >= 6:
            contribs.append(("team.size", -1.0, "a %d-person team benefits from an explicit service codebase" % c.size))
        if c.size <= 3 and c.mvp <= 10:
            contribs.append(("timeline.mvp_weeks", 1.0, "a %d-person team ships in %d weeks without building auth/storage" % (c.size, c.mvp)))
        if r["offline_support"] and "offline" in caps:
            contribs.append(("requirements.offline_support", 0.8, "SDK offline cache"))
    if "serverless" in caps:
        if r["p95_latency_ms"] and r["p95_latency_ms"] <= 200:
            contribs.append(("requirements.p95_latency_ms", -1.5, "cold starts threaten p95=%dms" % r["p95_latency_ms"]))
        if c.traffic == "spiky":
            contribs.append(("scale.traffic_pattern", 1.0, "absorbs spiky traffic with pay-per-request billing"))
        if r["realtime"]:
            contribs.append(("requirements.realtime", -1.0, "functions cannot hold connections"))
    p95 = r["p95_latency_ms"]
    if p95 and p95 <= 100 and cand["perf"] >= 5:
        contribs.append(("requirements.p95_latency_ms", 1.0, "low per-request overhead for p95=%dms" % p95))
    elif p95 and p95 <= 150 and cand["perf"] <= 3:
        contribs.append(("requirements.p95_latency_ms", -1.0, "per-request overhead is tight for p95=%dms" % p95))
    if c.rps >= 2000 and cand["perf"] >= 5:
        contribs.append(("extras.peak_requests_per_sec", 1.0, "efficient at ~%d req/s" % c.rps))
    if c.type == "realtime_collab" and "realtime" in caps and cand["perf"] >= 5:
        contribs.append(("scale.peak_concurrent_users", 0.8, "holds %d concurrent sockets cheaply" % c.peak))
    return None


def _database(cand, c, contribs):
    r = c.req
    caps = cand["caps"]
    cid = cand["id"]
    if cid == "none":
        if c.needs_backend:
            return ("requirements", "user data must be stored somewhere")
        contribs.append(("project.type", 3.0, "content is Markdown in the repository; no database to run"))
        return None
    if c.is_static:
        contribs.append(("project.type", -3.0, "a database is unnecessary for static content"))
    if c.strong:
        if "strong" in caps or "transactions" in caps:
            contribs.append(("requirements.payments" if r["payments"] else "project.type", 1.5,
                             "ACID transactions keep money/bookings consistent"))
        else:
            contribs.append(("requirements.payments" if r["payments"] else "project.type", -2.0,
                             "weaker multi-record transactions for money/bookings"))
    shape = c.extras.get("data_shape")
    shape_cap = {"relational": "relational", "document": "document", "key_value": "key_value",
                 "time_series": "timeseries", "graph": "relational", "mixed": "json"}
    if shape and shape_cap[shape] in caps:
        contribs.append(("extras.data_shape", 1.5, "matches the %s data shape" % shape.replace("_", "-")))
    elif shape:
        contribs.append(("extras.data_shape", -0.8, "not a natural fit for %s data" % shape.replace("_", "-")))
    if "geo" in c.signals and "geo" in caps:
        contribs.append(("project.description", 0.8, "geo queries (description mentions '%s')" % c.signals["geo"]))
    if r["full_text_search"]:
        if "fts" in caps:
            contribs.append(("requirements.full_text_search", 0.8, "built-in full-text search"))
        elif "fts_basic" in caps:
            contribs.append(("requirements.full_text_search", 0.5, "built-in full-text search is enough at this size"))
    if r["ml_features"] == "llm_rag" and "vector" in caps:
        contribs.append(("requirements.ml_features", 1.0, "vector search (pgvector/Atlas Vector) for RAG"))
    ts_need = c.type == "iot" or "timeseries" in c.signals or c.extras.get("data_shape") == "time_series"
    if "timeseries" in caps and not ts_need:
        contribs.append(("project.type", -1.5, "time-series extension adds operations without a time-series need"))
    if ts_need and "timeseries" in caps:
        contribs.append(("project.type", 1.5, "time-series compression and retention"))
    if c.type == "data_analytics" and "analytics" in caps:
        contribs.append(("project.type", 1.0, "columnar engine for analytical queries"))
    if c.type not in ("data_analytics",) and "analytics" in caps:
        contribs.append(("project.type", -3.0, "OLAP engine, not an application database"))
    if c.workload == "write_heavy" and c.L >= 4 and "write_scale" in caps:
        contribs.append(("scale.workload", 1.5, "linear write scaling"))
    if "massive_scale" in caps and c.L <= 2:
        contribs.append(("scale.users_12_months", -1.0, "built for scale this project does not have"))
    if cid == "sqlite":
        if c.L >= 2 or c.req["availability"] in ("99.9", "99.95", "99.99"):
            contribs.append(("requirements.availability", -3.0, "single-file DB has no failover for %s%%" % c.req["availability"]))
    if r["multi_tenant"] and "relational" in caps:
        contribs.append(("requirements.multi_tenant", 0.5, "row-level security / tenant_id isolation"))
    if r["offline_support"] and "offline_sync" in caps:
        contribs.append(("requirements.offline_support", 0.6, "client SDK sync"))
    for kind, val in c.hints:
        if kind == "db" and val == cid:
            contribs.append(("infrastructure.existing_systems", 2.5, "reuses the existing %s data and skills" % cand["name"]))
    if cid == "sqlserver" and "csharp" not in c.langs and not any(v == "sqlserver" for k, v in c.hints if k == "db"):
        contribs.append(("team.languages", -2.0, "SQL Server licensing and tooling only pay off for .NET teams or existing SQL Server estates"))
    if cid == "cassandra" and c.L <= 3:
        contribs.append(("scale.users_12_months", -2.0, "cluster operations not justified at this scale"))
    return None


def _hosting(cand, c, contribs):
    r = c.req
    caps = cand["caps"]
    cid = cand["id"]
    pid = host_provider(cand, c)
    if cand["provider"] == "cloud":
        bad = provider_check(pid, c, "host")
        if bad:
            return bad
    if c.cloud == "none" and c.deploy == "managed_cloud":
        pass
    if cid == "github_pages" and c.commercial:
        return ("project.type", "GitHub Pages terms forbid commercial/SaaS sites")
    if cid == "k8s" and not (c.L >= 4 or c.size >= 12):
        contribs.append(("team.size", -4.0, "Kubernetes is over-engineering for %d engineers at this scale" % c.size))
    av = r["availability"]
    if av in ("99.95", "99.99"):
        if pid in ("aws", "gcp", "azure"):
            contribs.append(("requirements.availability", 1.0, "multi-zone deployments for %s%%" % av))
        elif cid == "vm_compose":
            contribs.append(("requirements.availability", -2.5, "a single VM cannot meet %s%%" % av))
        elif cid in ("render", "railway", "do_app"):
            contribs.append(("requirements.availability", -1.0, "fewer HA controls for %s%%" % av))
    if c.traffic in ("spiky", "seasonal") and caps & {"scale_to_zero", "serverless", "functions"}:
        contribs.append(("scale.traffic_pattern", 0.8, "scales automatically with %s traffic" % c.traffic))
    if c.traffic in ("spiky",) and cid == "vm_compose":
        contribs.append(("scale.traffic_pattern", -0.8, "fixed capacity under spiky traffic"))
    for kind, val in c.hints:
        if kind == "cloud" and val == pid and c.cloud == "any":
            contribs.append(("infrastructure.existing_systems", 1.0, "the organisation already uses %s" % kb.PROVIDERS[pid]["name"]))
    if c.extras.get("geo_spread") == "global" and "cdn" in caps:
        contribs.append(("extras.geo_spread", 1.0, "global edge network for worldwide users"))
    if r["auth"] == "enterprise_sso" and "easy_auth" in caps:
        contribs.append(("requirements.auth", 1.0, "built-in Entra ID sign-in (Easy Auth)"))
    if c.deploy == "self_hosted_cloud_vm" and cid == "vm_compose":
        contribs.append(("infrastructure.deployment_model", 1.5, "matches deployment_model=self_hosted_cloud_vm"))
    if cid == "vm_compose" and c.deploy == "managed_cloud":
        contribs.append(("infrastructure.deployment_model", -1.0, "you manage the OS although managed_cloud was requested"))
    if c.is_static and caps & {"containers", "vm"} and "static" not in caps:
        contribs.append(("project.type", -2.5, "paying for servers to serve static files"))
    if c.needs_backend and not (caps & {"containers", "functions", "serverless", "baas", "vm", "onprem", "k8s"}):
        contribs.append(("project.type", -1.0, "static hosting only; the API must run elsewhere"))
    if c.is_static and "static" in caps:
        contribs.append(("project.type", 1.0, "serves pre-built static files from a CDN"))
    return None


# ------------------------------------------------------------------ realisation + pair rules
def realize_db(db, host, be, c):
    """Pick the concrete database service. Returns (dict) or (None, reason)."""
    did = db["id"]
    if did == "none":
        return {"name": "None", "provider": None, "cost": (0, 0)}, None
    if be["id"] == "supabase":
        if did != "postgres":
            return None, ("backend", "Supabase is built on PostgreSQL")
        return {"name": "PostgreSQL (Supabase, %s)" % _region("supabase", c), "provider": "supabase",
                "cost": (0, 0), "included": True}, None
    if be["id"] == "firebase":
        if did != "firestore":
            return None, ("backend", "Firebase uses Firestore")
        return {"name": "Cloud Firestore (Firebase)", "provider": "firebase", "cost": (0, 0), "included": True}, None
    if did == "firestore" and be["id"] != "firebase":
        pass
    hp = host_provider(host, c)
    managed = c.deploy == "managed_cloud"
    if host["id"] == "onprem" or c.deploy in ("on_premise", "air_gapped"):
        if did in ("dynamodb", "firestore"):
            return None, ("infrastructure.deployment_model", "%s is cloud-only" % db["name"])
        base_name = {"sqlserver": "SQL Server (licensed)", "mongodb": "MongoDB Community Server"}.get(did, db["name"])
        return {"name": "%s (self-hosted)" % base_name, "provider": "self", "cost": (0, 0), "self": True}, None
    if c.deploy == "self_hosted_cloud_vm" or (host["id"] == "vm_compose" and c.L <= 2 and did != "sqlserver"):
        if did in ("dynamodb", "firestore"):
            if did == "dynamodb" and hp == "aws":
                pass
            else:
                return None, ("infrastructure.deployment_model", "%s is a managed-only service" % db["name"])
        else:
            if did == "sqlite" and host["id"] not in ("vm_compose", "onprem"):
                return None, ("hosting", "SQLite needs a persistent single server")
            return {"name": "%s (in Docker Compose on the VM)" % db["name"], "provider": hp, "cost": (0, 0),
                    "self": True}, None
    if did == "sqlite":
        if host["id"] in ("vm_compose", "onprem"):
            return {"name": "SQLite (file on the VM)", "provider": hp, "cost": (0, 0), "self": True}, None
        return None, ("hosting", "SQLite needs a persistent single server, not %s" % host["name"])
    services = {
        "aws": {"postgres": "Amazon RDS for PostgreSQL" if c.L <= 3 else "Amazon Aurora PostgreSQL",
                "mysql": "Amazon RDS for MySQL", "sqlserver": "Amazon RDS for SQL Server",
                "dynamodb": "Amazon DynamoDB", "cassandra": "Amazon Keyspaces",
                "timescale": "TimescaleDB on EC2 (self-managed)", "clickhouse": "ClickHouse on EC2 (self-managed)"},
        "gcp": {"postgres": "Cloud SQL for PostgreSQL", "mysql": "Cloud SQL for MySQL",
                "sqlserver": "Cloud SQL for SQL Server", "firestore": "Cloud Firestore",
                "timescale": "TimescaleDB on Compute Engine (self-managed)",
                "clickhouse": "ClickHouse on Compute Engine (self-managed)"},
        "azure": {"postgres": "Azure Database for PostgreSQL – Flexible Server",
                  "mysql": "Azure Database for MySQL – Flexible Server", "sqlserver": "Azure SQL Database",
                  "timescale": "TimescaleDB on Azure VM (self-managed)"},
        "digitalocean": {"postgres": "DigitalOcean Managed PostgreSQL", "mysql": "DigitalOcean Managed MySQL",
                         "mongodb": "DigitalOcean Managed MongoDB"},
        "render": {"postgres": "Render Postgres"},
        "railway": {"postgres": "Railway PostgreSQL", "mysql": "Railway MySQL"},
    }
    name, prov = None, hp
    if hp in services and did in services[hp]:
        name = services[hp][did]
    elif did == "mongodb":
        name, prov = "MongoDB Atlas", "atlas"
    elif did == "postgres" and hp in ("vercel", "netlify", "cloudflare", "github"):
        if c.eff_residency == "india":
            name, prov = "Supabase Postgres (database only)", "supabase"
        else:
            name, prov = "Neon serverless Postgres", "neon"
    if name is None:
        return None, ("database", "%s is not offered next to %s" % (db["name"], host["name"]))
    bad = provider_check(prov, c, "data")
    if bad:
        return None, bad
    selfm = "self-managed" in name
    return {"name": "%s (%s)" % (name, _region(prov, c)), "provider": prov,
            "cost": db_service_cost(did, prov, selfm, c), "self": selfm}, None


def _region(pid, c):
    p = kb.PROVIDERS.get(pid, {})
    lbl = p.get("region_label", {})
    zone = c.eff_residency if c.eff_residency in lbl else None
    if zone is None and "gdpr" in c.compliance and "eu" in lbl:
        zone = "eu"
    if zone is None:
        g = c.extras.get("geo_spread")
        zone = "india" if ("india" in lbl and g in (None, "single_city", "single_country")
                           and (c.residency == "none")) and _india_hint(c) else ("us" if "us" in lbl else None)
    return lbl.get(zone, "region nearest your users")


def _india_hint(c):
    text = (c.spec.get("project.description") or "").lower()
    return "india" in text or "dpdp" in c.compliance or "rbi_localisation" in c.compliance or "phone_otp" == c.req["auth"]


def pair_rules(combo, c):
    """Return (valid, reason, contribs, db_service)."""
    web, mob, desk, be, db, host = (combo.get(k) for k in ("web", "mobile", "desktop", "backend", "database", "hosting"))
    out = []
    if web and web.get("requires_backend") and be["id"] != web["requires_backend"]:
        return False, ("web", "%s needs the Django backend" % web["name"]), out, None
    if be.get("requires_web") and (not web or web["id"] != be["requires_web"]):
        return False, ("backend", "%s needs Next.js on the web" % be["name"]), out, None
    if host.get("requires_backend") and be["id"] != host["requires_backend"]:
        return False, ("hosting", "%s only hosts %s" % (host["name"], host["requires_backend"])), out, None
    if be["id"] == "aws_lambda" and host["id"] != "aws_serverless":
        return False, ("hosting", "Lambda is hosted by the AWS serverless stack"), out, None
    if be["id"] == "azure_functions" and host["id"] not in ("azure_static", "azure_app_service", "azure_container_apps"):
        return False, ("hosting", "Azure Functions run on Azure"), out, None
    if be["id"] == "none" and db["id"] != "none":
        return False, ("database", "a database needs a backend"), out, None
    if db["id"] == "none" and be["id"] != "none":
        return False, ("database", "the backend needs somewhere to store data"), out, None
    if be["id"] == "none" and not web:
        return False, ("backend", "nothing to serve"), out, None
    caps = host["caps"]
    if host["id"] == "baas_hosted":
        if be["id"] not in ("supabase", "firebase") or web:
            return False, ("hosting", "only for mobile-only apps on a BaaS"), out, None
    elif be["id"] in ("supabase", "firebase") and not web:
        return False, ("hosting", "a mobile-only BaaS app needs no separate hosting"), out, None
    if web and web["id"] == "nextjs" and host["id"] in ("cloudflare_pages", "azure_static", "firebase_hosting"):
        out.append(("hosting", -1.5, "%s supports Next.js only partially (adapter/preview), losing some SSR features" % host["name"]))
    if be["id"] in SERVER_BACKENDS and not (caps & {"containers", "vm", "onprem", "k8s"}):
        return False, ("hosting", "%s cannot run a long-lived %s server" % (host["name"], be["name"])), out, None
    if be["id"] == "next_fullstack" and not (caps & {"functions", "containers"}):
        return False, ("hosting", "%s cannot run Next.js server code" % host["name"]), out, None
    if be["id"] in ("supabase", "firebase", "none") and not (caps & {"static", "functions", "containers", "baas"}):
        return False, ("hosting", "no static hosting"), out, None
    if web and web["id"] in ("nextjs", "nuxt", "sveltekit") and caps == {"static"} and be["id"] != "none":
        out.append(("hosting", -1.0, "static export only: loses server rendering"))
    if web and web["id"] == "nextjs" and "next_native" in caps:
        out.append(("hosting", 1.0, "Vercel builds and serves Next.js natively"))
    if be["id"] == "next_fullstack" and "next_native" in caps:
        out.append(("hosting", 0.8, "API routes deploy with the front end"))
    if be["id"] in ("aspnet",) and host_provider(host, c) == "azure":
        out.append(("team.languages", 0.8, "Azure is the home platform for .NET"))
    if db["id"] == "sqlserver" and host_provider(host, c) == "azure":
        out.append(("infrastructure.existing_systems", 0.8, "Azure SQL is managed SQL Server"))
    # compute residency/compliance on the host
    compute = be["id"] in SERVER_BACKENDS or be["id"] == "next_fullstack"
    if compute:
        bad = provider_check(host_provider(host, c), c, "data")
        if bad:
            return False, bad, out, None
    # realtime transport
    if c.req["realtime"] and be["id"] not in ("supabase", "firebase") and "websockets" not in caps:
        out.append(("requirements.realtime", -1.5, "%s cannot hold WebSockets: a hosted real-time service is needed" % host["name"]))
    if c.req["realtime"] and be["id"] in ("supabase", "firebase"):
        out.append(("requirements.realtime", 0.8, "real-time channels included in %s" % be["name"].split(" (")[0]))
    # backend-as-a-service brings auth/storage/realtime: bonus when needed
    if be["id"] in ("supabase", "firebase"):
        k = sum(1 for x in ("file_media_storage", "realtime") if c.req[x]) + (1 if c.req["auth"] != "none" else 0)
        if k:
            out.append(("requirements.auth", 0.4 * k, "auth/storage/realtime come from one managed service"))
    dbs, why = realize_db(db, host, be, c)
    if dbs is None:
        return False, why, out, None
    # vendor count for small teams
    provs = {host_provider(host, c), dbs.get("provider"), be.get("provider")} - {None, "self"}
    if c.size <= 3 and len(provs) > 1:
        out.append(("team.size", -0.4 * (len(provs) - 1), "%d vendors to manage for a %d-person team" % (len(provs), c.size)))
    # budget for the combination
    hcost = host["cost"][c.L]
    hmin = 0 if (host["id"] == "vercel" and not c.commercial and c.L == 1) else hcost[0]
    bmin = be["cost"][c.L][0] if be["cost"] is not kb.FREE else 0
    total_min = hmin + bmin + dbs["cost"][0]
    if c.budget == 0 and total_min > 0:
        out.append(("budget.monthly_infra_usd", -4.0, "core stack costs ~$%d/month against a $0 budget" % total_min))
    elif c.budget > 0 and total_min > c.budget * 0.8:
        out.append(("budget.monthly_infra_usd", -min(6.0, 3.0 * (total_min / (0.8 * c.budget) - 1)),
                    "core stack starts near $%d/month against a $%g budget" % (total_min, c.budget)))
    if dbs.get("self") and not c.devops and c.deploy == "managed_cloud":
        out.append(("team.has_devops", -1.0, "self-managed database without a DevOps engineer"))
    if c.req["availability"] in ("99.95", "99.99") and dbs.get("self"):
        out.append(("requirements.availability", -1.5, "self-managed DB needs your own replication for %s%%" % c.req["availability"]))
    if c.req["offline_support"] and mob and be["id"] in SERVER_BACKENDS | {"supabase"}:
        out.append(("requirements.offline_support", 0.3, "sync endpoint you control"))
    return True, None, out, dbs


# ------------------------------------------------------------------ search
LAYER_TOPN = {"web": 5, "mobile": 4, "desktop": 2, "backend": 7, "database": 6, "hosting": 8}


def search(c):
    core = kb.all_core()
    layers = ["backend", "database", "hosting"]
    if "web" in c.platforms:
        layers.insert(0, "web")
    if "android" in c.platforms or "ios" in c.platforms:
        layers.insert(0, "mobile")
    if "desktop" in c.platforms:
        layers.insert(0, "desktop")
    scored, excluded = {}, {}
    for layer in layers:
        rows, ex = [], []
        for cand in core[layer]:
            s, contribs, excl = score(cand, layer, c)
            if excl:
                ex.append((cand, s, contribs, excl))
            else:
                rows.append((cand, s, contribs))
        rows.sort(key=lambda r: (-round(r[1], 6), r[0]["id"]))
        ex.sort(key=lambda r: (-round(r[1], 6), r[0]["id"]))
        scored[layer] = rows
        excluded[layer] = ex
    pools = [scored[l][:LAYER_TOPN[l]] for l in layers]
    combos = []
    invalid = {}
    if all(pools):
        for tup in itertools.product(*pools):
            combo = {l: t[0] for l, t in zip(layers, tup)}
            ok, why, pair, dbs = pair_rules(combo, c)
            if not ok:
                invalid[why] = invalid.get(why, 0) + 1
                continue
            unary = sum(t[1] for t in tup)
            total = unary + sum(d for _, d, _ in pair)
            key = tuple(combo[l]["id"] for l in layers)
            combos.append((round(total, 6), key, combo, pair, dbs, {l: t for l, t in zip(layers, tup)}))
    combos.sort(key=lambda x: (-x[0], x[1]))
    return layers, scored, excluded, combos, invalid


# ------------------------------------------------------------------ explanation
def _explain(cand, contribs, extra_pair=None, max_n=4):
    pos = sorted([x for x in contribs if x[1] > 0], key=lambda x: (-x[1], x[0]))
    neg = sorted([x for x in contribs if x[1] < 0], key=lambda x: (x[1], x[0]))
    if extra_pair:
        pos += [x for x in extra_pair if x[1] > 0]
        neg += [x for x in extra_pair if x[1] < 0]
    reasons = [t for _, _, t in pos[:max_n]]
    just = cand["why"][:1].upper() + cand["why"][1:] if cand.get("why") else cand["name"]
    if reasons:
        just += ". Fit for this project: " + "; ".join(reasons) + "."
    else:
        just += "."
    driven = []
    for f, d, _ in sorted(contribs + (extra_pair or []), key=lambda x: (-abs(x[1]), x[0])):
        if abs(d) >= 0.3 and f not in driven:
            driven.append(f)
    trade = list(cand.get("tradeoffs", []))
    for _, d, t in neg[:2]:
        if d > -0.5:
            continue
        t2 = t[:1].upper() + t[1:] + "."
        if t2 not in trade:
            trade.append(t2)
    return just, driven[:7], trade


def _why_not(alt, alt_contribs, chosen_contribs, gap, excl=None):
    if excl:
        return "Excluded: %s (%s)." % (excl[1], excl[0])
    chosen_fields = {}
    for f, d, t in chosen_contribs:
        chosen_fields[f] = chosen_fields.get(f, 0) + d
    alt_fields = {}
    for f, d, t in alt_contribs:
        alt_fields[f] = alt_fields.get(f, 0) + d
    worst = sorted([x for x in alt_contribs if x[1] < 0], key=lambda x: (x[1], x[0]))[:2]
    diffs = sorted(((chosen_fields.get(f, 0) - alt_fields.get(f, 0), f) for f in set(chosen_fields) | set(alt_fields)),
                   key=lambda x: (-x[0], x[1]))
    parts = [t for _, _, t in worst if abs(_) >= 0.4] if False else [t for f, d, t in worst if d <= -0.4]
    if diffs and diffs[0][0] > 0.2:
        f = diffs[0][1]
        best_txt = sorted([x for x in chosen_contribs if x[0] == f and x[1] > 0], key=lambda x: -x[1])
        if best_txt:
            parts.append("the chosen option wins on %s (%s)" % (f, best_txt[0][2]))
    if not parts:
        parts.append("close runner-up; the chosen option combines better with the rest of the stack")
    return "Scored %.1f points lower: %s." % (max(gap, 0.1), "; ".join(parts))


def _component(choice, cand, contribs, pair, alts):
    just, driven, trade = _explain(cand, contribs, pair)
    return {"choice": choice, "justification": just, "driven_by": driven or ["project.type"],
            "alternatives": alts, "tradeoffs": trade, "id": cand["id"]}


def _host_name(h, c):
    pid = host_provider(h, c)
    if h["id"] == "vm_compose":
        vm = {"aws": "Amazon EC2", "gcp": "Compute Engine VM", "azure": "Azure VM",
              "digitalocean": "DigitalOcean Droplet"}.get(pid, "Cloud VM")
        return "%s + Docker Compose (%s)" % (vm, _region(pid, c))
    if h["id"] == "k8s":
        k = {"aws": "Amazon EKS", "gcp": "Google GKE", "azure": "Azure AKS", "digitalocean": "DigitalOcean Kubernetes"}[pid]
        return "%s (%s)" % (k, _region(pid, c))
    if h["id"] == "onprem":
        return h["name"]
    if h["id"] == "baas_hosted":
        return "None (the mobile app calls the managed backend directly)"
    if pid in ("github", "cloudflare"):
        return h["name"]
    if c.is_static and h["provider"] in ("netlify", "vercel"):
        return h["name"]
    return "%s (%s)" % (h["name"], _region(pid, c))


def _alternatives(layer, best, combos, scored, excluded, c, chosen_contribs, n=2):
    chosen_id = best[2][layer]["id"]
    best_with = {}
    for combo_row in combos:
        cid = combo_row[2][layer]["id"]
        if cid not in best_with:
            best_with[cid] = combo_row
    alts, seen = [], {chosen_id}
    for cand, s, contribs in scored.get(layer, []):
        if cand["id"] in seen or cand["id"] not in best_with:
            continue
        seen.add(cand["id"])
        total, key, combo, pair, dbs, parts = best_with[cand["id"]]
        name = dbs["name"] if layer == "database" else (_host_name(cand, c) if layer == "hosting" else cand["name"])
        alts.append({"choice": name, "why_not": _why_not(cand, parts[layer][2], chosen_contribs, best[0] - total)})
        if len(alts) >= n:
            break
    if not alts:
        for cand, s, contribs in scored.get(layer, []):
            if cand["id"] not in seen:
                seen.add(cand["id"])
                alts.append({"choice": cand["name"], "why_not": "Does not combine with the rest of the stack under "
                                                                "the given constraints."})
                break
    hard_fields = ["infrastructure.data_residency", "compliance", "infrastructure.cloud_preference"]
    if c.deploy != "managed_cloud":
        hard_fields.append("infrastructure.deployment_model")
    for cand, s, contribs, excl in excluded.get(layer, [])[:8]:
        if excl[0] in hard_fields and cand["id"] not in seen and cand["id"] not in ("none", "baas_hosted"):
            alts.append({"choice": cand["name"], "why_not": _why_not(cand, contribs, chosen_contribs, 0, excl)})
            break
    if not alts:
        alts.append({"choice": "None", "why_not": "No other option satisfies the hard constraints."})
    return alts


# ------------------------------------------------------------------ main entry
def recommend(raw):
    spec, errors, assumptions, notes = schema.validate(raw)
    sid = raw.get("scenario_id") if isinstance(raw, dict) else None
    base = {"scenario_id": sid, "engine_version": ENGINE_VERSION, "llm_used": False}
    if errors:
        base.update({"status": "invalid_input",
                     "summary": "The input has %d invalid or missing field(s); fix them and run again." % len(errors),
                     "architecture_style": None, "stack": None, "estimated_monthly_cost_usd": None,
                     "warnings": notes, "assumptions": assumptions, "validation_errors": errors})
        return base
    c = build_ctx(spec, raw, assumptions)
    warnings = list(notes)
    hard, soft_relax = conflicts(c, warnings, assumptions)
    warnings += eol_warnings(c.existing)
    for name, matched in c.signals.items():
        if name in sigmod.IMPLIES:
            field, val = sigmod.IMPLIES[name]
            key = field.split(".", 1)[1]
            if c.req.get(key) != val:
                warnings.append({"code": "DESCRIPTION_MISMATCH", "severity": "info",
                                 "message": "The description mentions '%s' but %s is %s. StackSense follows the "
                                            "structured field; check it." % (matched, field, json.dumps(c.req.get(key))),
                                 "fields": ["project.description", field]})
    if hard:
        warnings = hard + warnings + soft_relax
        base.update({"status": "infeasible",
                     "summary": "Hard constraints contradict each other (%s); no compliant stack exists until one "
                                "is relaxed." % ", ".join(sorted({w["code"] for w in hard})),
                     "architecture_style": None, "stack": None, "estimated_monthly_cost_usd": None,
                     "warnings": warnings, "assumptions": assumptions, "validation_errors": []})
        return base
    layers, scored, excluded, combos, invalid = search(c)
    if not combos:
        reasons = []
        for layer in layers:
            if not scored[layer]:
                rs = sorted({e[3][1] for e in excluded[layer]})[:3]
                reasons.append("no %s option survives the hard constraints (%s)" % (layer, "; ".join(rs)))
        if not reasons:
            top = sorted(invalid.items(), key=lambda x: (-x[1], str(x[0])))[:3]
            reasons = ["%s" % k[1] for k, _ in top]
        fields = sorted({e[3][0] for l in layers for e in excluded[l]})
        warnings.append({"code": "NO_FEASIBLE_STACK", "severity": "critical",
                         "message": "No stack satisfies every hard constraint: " + "; ".join(reasons) + ".",
                         "fields": fields or ["infrastructure"]})
        base.update({"status": "infeasible", "summary": "No stack satisfies the hard constraints; see warnings.",
                     "architecture_style": None, "stack": None, "estimated_monthly_cost_usd": None,
                     "warnings": warnings, "assumptions": assumptions, "validation_errors": []})
        return base

    best = combos[0]
    total, key, combo, pair, dbs, parts = best
    pair_by_layer = {}
    layer_of = {"hosting": "hosting", "budget.monthly_infra_usd": "hosting", "requirements.realtime": "hosting",
                "team.size": "hosting", "web": "web", "backend": "backend", "database": "database",
                "team.languages": "backend", "infrastructure.existing_systems": "database",
                "requirements.auth": "backend", "team.has_devops": "database", "requirements.availability": "database",
                "requirements.offline_support": "backend"}
    for f, d, t in pair:
        pair_by_layer.setdefault(layer_of.get(f, "hosting"), []).append((f, d, t))

    stack = {}
    # frontend
    fparts, fcontribs, fcands = [], [], []
    for layer in ("web", "mobile", "desktop"):
        if layer in combo:
            cand = combo[layer]
            label = {"web": "web", "mobile": "mobile", "desktop": "desktop"}[layer]
            fparts.append("%s (%s)" % (cand["name"], label) if len([l for l in ("web", "mobile", "desktop") if l in combo]) > 1 else cand["name"])
            fcontribs += parts[layer][2]
            fcands.append((layer, cand))
    if fcands:
        just_parts, driven, trade, alts = [], [], [], []
        for layer, cand in fcands:
            j, d, t = _explain(cand, parts[layer][2], pair_by_layer.get(layer))
            just_parts.append(("%s: " % layer.capitalize() if len(fcands) > 1 else "") + j)
            driven += [x for x in d if x not in driven]
            trade += t
            alts += _alternatives(layer, best, combos, scored, excluded, c, parts[layer][2], n=2 if len(fcands) == 1 else 1)
        stack["frontend"] = {"choice": " + ".join(fparts), "justification": " ".join(just_parts),
                             "driven_by": driven[:8], "alternatives": alts, "tradeoffs": trade[:5],
                             "parts": {l: cand["name"] for l, cand in fcands}}
    else:
        stack["frontend"] = {"choice": "None", "justification": "platforms=['api_only']: consumers integrate "
                             "through the API, so there is no first-party UI. Publish an OpenAPI spec and a docs portal "
                             "instead.", "driven_by": ["project.platforms"],
                             "alternatives": [{"choice": "Developer docs site (Astro Starlight or Docusaurus)",
                                               "why_not": "Useful later, but not a product front end; the API reference "
                                                          "can be generated from the OpenAPI spec."}],
                             "tradeoffs": ["Partners need SDKs or clear docs to adopt the API."]}
    be = combo["backend"]
    stack["backend"] = _component(be["name"], be, parts["backend"][2], pair_by_layer.get("backend"),
                                  _alternatives("backend", best, combos, scored, excluded, c, parts["backend"][2]))
    db = combo["database"]
    dbc = _component(dbs["name"], db, parts["database"][2], pair_by_layer.get("database"),
                     _alternatives("database", best, combos, scored, excluded, c, parts["database"][2]))
    if dbs.get("provider") and dbs.get("provider") != "self" and c.eff_residency in ("india", "eu", "us"):
        dbc["justification"] += " Data stays in %s to satisfy data_residency=%s." % (
            _region(dbs["provider"], c), c.eff_residency)
        if "infrastructure.data_residency" not in dbc["driven_by"]:
            dbc["driven_by"].append("infrastructure.data_residency")
    if "geo" in c.signals and db["id"] == "postgres":
        dbc["justification"] += " Enable PostGIS for the GPS/location data."
    if "ledger" in c.signals and db["id"] in ("postgres", "mysql", "sqlserver"):
        dbc["justification"] += " Model the ledger as append-only double-entry journal tables with SERIALIZABLE transactions."
    stack["database"] = dbc
    host = combo["hosting"]
    hc = _component(_host_name(host, c), host, parts["hosting"][2], pair_by_layer.get("hosting"),
                    _alternatives("hosting", best, combos, scored, excluded, c, parts["hosting"][2]))
    if c.deploy != "managed_cloud" and "infrastructure.deployment_model" not in hc["driven_by"]:
        hc["driven_by"].insert(0, "infrastructure.deployment_model")
    if c.cloud in ("aws", "gcp", "azure") and "infrastructure.cloud_preference" not in hc["driven_by"]:
        hc["driven_by"].insert(0, "infrastructure.cloud_preference")
    stack["hosting"] = hc
    for k in ("backend", "database", "hosting"):
        stack[k].pop("id", None)

    supporting, sup_warn = choose_supporting(c, combo, dbs, host_provider(host, c))
    stack["supporting"] = supporting
    warnings += sup_warn
    style = architecture_style(c, combo, supporting)
    cost = estimate(c, combo, dbs, supporting, host_provider(host, c))
    warnings += budget_warnings(c, cost, assumptions)
    warnings += soft_relax
    warnings = _dedupe(warnings)
    sev = {w["severity"] for w in warnings}
    status = "ok_with_warnings" if (sev & {"warning", "critical"}) else "ok"
    base.update({
        "status": status,
        "summary": summary(c, combo, dbs, style, host),
        "architecture_style": style,
        "stack": stack,
        "estimated_monthly_cost_usd": cost,
        "warnings": warnings,
        "assumptions": assumptions,
        "validation_errors": [],
        "derived": {"scale_level": c.L, "peak_concurrent_users": c.peak, "estimated_peak_rps": c.rps,
                    "data_volume_gb": c.data_gb, "effective_data_residency": c.eff_residency,
                    "description_signals": c.signals, "combinations_evaluated": len(combos),
                    "input_hash": hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]},
    })
    return base


def _dedupe(ws):
    seen, out = set(), []
    for w in ws:
        k = (w["code"], w["message"])
        if k not in seen:
            seen.add(k)
            out.append(w)
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda w: (order[w["severity"]],))


def architecture_style(c, combo, supporting):
    be = combo["backend"]["id"]
    if be == "none":
        return "static"
    if be in ("supabase", "firebase", "aws_lambda", "azure_functions", "next_fullstack"):
        return "serverless"
    realtime_svc = any(s["layer"] == "realtime" and s.get("separate_service") for s in supporting)
    ml_svc = any(s["layer"] == "ml" and s.get("separate_service") for s in supporting)
    if c.L >= 4 and c.size >= 15 and c.devops:
        return "microservices"
    if realtime_svc or ml_svc:
        return "service_oriented"
    if c.size >= 4 or c.type in ("fintech", "healthtech", "saas_b2b", "marketplace", "ecommerce"):
        return "modular_monolith"
    return "monolith"


def summary(c, combo, dbs, style, host):
    be, db = combo["backend"], combo["database"]
    fe = [combo[l]["name"].split(" (")[0] for l in ("web", "mobile", "desktop") if l in combo]
    if style == "static":
        return "Static site built with %s and served free from %s; no backend or database to run." % (
            fe[0] if fe else "a static generator", host["name"])
    style_txt = style.replace("_", " ")
    if host["id"] == "baas_hosted":
        return "%s %s for the API, auth, storage and %s database%s; front end: %s; no separate hosting." % (
            style_txt[:1].upper() + style_txt[1:], be["name"].split(" (")[0], dbs["name"].split(" (")[0],
            " in India" if c.eff_residency == "india" else "", ", ".join(fe))
    return "%s %s on %s with %s as the database%s; front end: %s." % (
        style_txt[:1].upper() + style_txt[1:], be["name"].split(" (")[0], _host_name(host, c).split(" (")[0],
        dbs["name"].split(" (")[0], " in %s" % c.eff_residency.upper() if c.eff_residency in ("eu", "us") else
        (" in India" if c.eff_residency == "india" else ""), ", ".join(fe) or "none (API only)")


def budget_warnings(c, cost, assumptions):
    out = []
    mn, mx = cost["min"], cost["max"]
    if c.budget == 0 and mn > 0:
        out.append({"code": "OVER_BUDGET", "severity": "critical",
                    "message": "Even the leanest compliant stack costs about $%d-%d/month; the $0 budget cannot "
                               "cover it. Shown as a reduced-cost stack with the budget relaxed." % (mn, mx),
                    "fields": ["budget.monthly_infra_usd"]})
        assumptions.append("Relaxed: budget.monthly_infra_usd ($0) treated as soft; cheapest viable stack shown.")
    elif c.budget > 0 and mn > c.budget:
        sev = "critical" if mn > 2 * c.budget else "warning"
        out.append({"code": "OVER_BUDGET", "severity": sev,
                    "message": "Estimated $%d-%d/month exceeds the $%g budget at the low end. Use the cheapest tier of "
                               "each service or raise the budget." % (mn, mx, c.budget),
                    "fields": ["budget.monthly_infra_usd"]})
        if sev == "critical":
            assumptions.append("Relaxed: budget.monthly_infra_usd ($%g) cannot fund the minimum compliant stack." % c.budget)
    elif c.budget > 0 and mx > c.budget:
        out.append({"code": "BUDGET_HEADROOM", "severity": "info",
                    "message": "Upper estimate ($%d) exceeds the $%g budget; watch usage as you grow." % (mx, c.budget),
                    "fields": ["budget.monthly_infra_usd"]})
    if c.budget == 0 and mn == 0:
        out.append({"code": "FREE_TIER_LIMITS", "severity": "info",
                    "message": "Runs on free tiers; check their bandwidth/build limits if traffic grows.",
                    "fields": ["budget.monthly_infra_usd"]})
    return out
