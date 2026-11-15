"""Conflict detection, end-of-life checks and existing-system hints."""
import re

from . import kb


def existing_hints(existing):
    out = set()
    for item in existing or []:
        low = item.lower()
        for pattern, kind, val in kb.EXISTING:
            if re.search(pattern, low):
                out.add((kind, val))
    return sorted(out)


def eol_warnings(existing):
    out = []
    for item in existing or []:
        low = item.lower()
        for pattern, name, end, msg, src in kb.EOL:
            if re.search(pattern, low):
                past = end is not None and end <= kb.KB_DATE
                out.append({"code": "END_OF_LIFE" if end else "RENAMED_PRODUCT",
                            "severity": "warning" if past else "info",
                            "message": "Existing system '%s': %s Source: %s" % (item, msg, src),
                            "fields": ["infrastructure.existing_systems"]})
                break
    return out


def W(code, sev, msg, fields):
    return {"code": code, "severity": sev, "message": msg, "fields": fields}


def conflicts(c, warnings, assumptions):
    """Return (hard, soft). Hard conflicts make the scenario infeasible.

    Soft conflicts are returned as warnings. Requirements that contradict a
    hard constraint are relaxed in-place (and the relaxation is recorded as an
    assumption) so the engine can still return a clearly labelled stack.
    """
    hard, soft = [], []
    r = c.req
    plats = c.platforms
    # ---- hard vs hard
    if c.cloud in ("aws", "gcp", "azure") and c.deploy in ("on_premise", "air_gapped"):
        hard.append(W("CLOUD_VS_DEPLOYMENT", "critical",
                      "cloud_preference=%s conflicts with deployment_model=%s: a public cloud cannot also be "
                      "on-premise/air-gapped. Set cloud_preference to 'none' or pick a cloud deployment model."
                      % (c.cloud, c.deploy), ["infrastructure.cloud_preference", "infrastructure.deployment_model"]))
    if c.cloud == "none" and c.deploy in ("managed_cloud", "self_hosted_cloud_vm"):
        hard.append(W("NO_CLOUD_VS_CLOUD_DEPLOYMENT", "critical",
                      "cloud_preference=none (no public cloud) conflicts with deployment_model=%s, which needs a "
                      "cloud provider." % c.deploy,
                      ["infrastructure.cloud_preference", "infrastructure.deployment_model"]))
    if "rbi_localisation" in c.compliance and c.residency in ("eu", "us", "other"):
        hard.append(W("RBI_VS_RESIDENCY", "critical",
                      "rbi_localisation requires payment data to be stored only in India, but data_residency=%s. "
                      "These cannot both hold." % c.residency, ["compliance", "infrastructure.data_residency"]))
    if c.residency == "other":
        soft.append(W("RESIDENCY_UNVERIFIED", "warning",
                      "data_residency=other: StackSense limits choices to providers with many regions or "
                      "self-hosting, but you must confirm a region exists in the required country.",
                      ["infrastructure.data_residency"]))
    # ---- requirement vs hard constraint (relax requirement, keep stack)
    if c.deploy == "air_gapped":
        if r["payments"]:
            r["payments"] = False
            soft.append(W("AIRGAP_VS_PAYMENTS", "critical",
                          "Online payments need an external payment gateway, which an air-gapped network cannot "
                          "reach. Payments were removed from the recommendation; handle them in a separate, "
                          "connected system.", ["infrastructure.deployment_model", "requirements.payments"]))
            assumptions.append("Relaxed: requirements.payments dropped (impossible when air-gapped).")
        if r["auth"] in ("social", "phone_otp"):
            old = r["auth"]
            r["auth"] = "email_password"
            soft.append(W("AIRGAP_VS_AUTH", "critical",
                          "auth=%s needs external identity/SMS providers; an air-gapped network cannot reach them. "
                          "Replaced with local accounts in Keycloak." % old,
                          ["infrastructure.deployment_model", "requirements.auth"]))
            assumptions.append("Relaxed: requirements.auth %s -> email_password (air-gapped)." % old)
        if r["ml_features"] == "llm_rag":
            soft.append(W("AIRGAP_LLM", "warning",
                          "llm_rag when air-gapped needs a self-hosted open-weight model on local GPUs.",
                          ["requirements.ml_features"]))
        if r["seo_critical"]:
            soft.append(W("AIRGAP_VS_SEO", "warning",
                          "seo_critical=true but an air-gapped system is not reachable by search engines.",
                          ["requirements.seo_critical", "infrastructure.deployment_model"]))
    # ---- soft contradictions
    avail = r["availability"]
    if avail == "99.99" and (c.size == 1 or c.exp == "beginner" or not c.devops):
        soft.append(W("AVAILABILITY_VS_TEAM", "critical",
                      "99.99%% availability allows ~4 minutes of downtime a month. That needs multi-zone (often "
                      "multi-region) failover and 24x7 on-call, which a %s team of %d%s cannot realistically run. "
                      "The stack uses managed multi-zone services; plan for 99.9%% until on-call exists."
                      % (c.exp, c.size, " without DevOps" if not c.devops else ""),
                      ["requirements.availability", "team.size", "team.experience", "team.has_devops"]))
    elif avail == "99.95" and not c.devops and c.size <= 2:
        soft.append(W("AVAILABILITY_VS_TEAM", "warning",
                      "99.95% with a %d-person team and no DevOps relies entirely on managed multi-zone services."
                      % c.size, ["requirements.availability", "team.has_devops"]))
    if avail == "99.99" and c.budget < 500:
        soft.append(W("AVAILABILITY_VS_BUDGET", "warning",
                      "99.99%% needs redundant capacity in several zones; $%g/month is unlikely to fund it." % c.budget,
                      ["requirements.availability", "budget.monthly_infra_usd"]))
    clients = [p for p in plats if p in ("web", "android", "ios", "desktop")]
    load = len(clients) + sum(1 for k in ("payments", "realtime", "offline_support", "multi_tenant") if r[k]) \
        + (1 if r["ml_features"] != "none" else 0) + (1 if c.compliance else 0)
    capacity = c.size * c.mvp
    if load >= 3 and capacity < load * 4:
        sev = "critical" if capacity < load * 2 else "warning"
        soft.append(W("TIMELINE_VS_SCOPE", sev,
                      "%d client platform(s) plus %d major feature(s) in %d week(s) with %d engineer(s) is very "
                      "tight. Ship one platform first (the stack favours shared code across platforms)."
                      % (len(clients), load - len(clients), c.mvp, c.size),
                      ["timeline.mvp_weeks", "project.platforms", "team.size"]))
    if c.users >= 100000 and c.budget < 100 and not c.is_static:
        soft.append(W("SCALE_VS_BUDGET", "critical",
                      "%d users with a $%g/month budget is not realistic for a dynamic app; expect at least a few "
                      "hundred dollars a month." % (c.users, c.budget),
                      ["scale.users_12_months", "budget.monthly_infra_usd"]))
    if c.exp == "beginner" and c.size <= 2 and (c.type in ("fintech", "healthtech")
                                               or set(c.compliance) & {"pci_dss", "hipaa", "rbi_localisation"}):
        soft.append(W("SKILL_VS_RISK", "critical",
                      "A small beginner team building a regulated %s product carries high security and compliance "
                      "risk. Use managed, certified services and get an expert security review before launch."
                      % c.type.replace("_", " "), ["team.experience", "compliance", "project.type"]))
    if r["offline_support"] and not any(p in plats for p in ("android", "ios", "desktop")):
        soft.append(W("OFFLINE_WEB_ONLY", "info",
                      "offline_support on web only: the app must be a PWA with a service worker and IndexedDB; "
                      "browsers may evict this storage.", ["requirements.offline_support", "project.platforms"]))
    if r["seo_critical"] and "web" not in plats:
        soft.append(W("SEO_WITHOUT_WEB", "warning",
                      "seo_critical=true but there is no web platform with public pages to rank.",
                      ["requirements.seo_critical", "project.platforms"]))
    if r["seo_critical"] and c.type == "internal_tool":
        soft.append(W("SEO_INTERNAL_TOOL", "info", "An internal tool is normally behind login; SEO does not apply.",
                      ["requirements.seo_critical", "project.type"]))
    if r["p95_latency_ms"] and r["p95_latency_ms"] < 50:
        soft.append(W("LATENCY_TIGHT", "warning",
                      "p95 < 50 ms leaves little room for network round trips; users must be close to the servers "
                      "(edge/regional deployment and caching).", ["requirements.p95_latency_ms"]))
    if c.spec["scale.users_12_months"] < c.spec["scale.users_at_launch"]:
        soft.append(W("SHRINKING_USERS", "info",
                      "users_12_months is lower than users_at_launch; sizing uses the larger figure.",
                      ["scale.users_12_months", "scale.users_at_launch"]))
    if "pci_dss" in c.compliance and not r["payments"]:
        soft.append(W("PCI_WITHOUT_PAYMENTS", "info", "pci_dss is listed but payments=false.",
                      ["compliance", "requirements.payments"]))
    if r["payments"] and "pci_dss" not in c.compliance:
        soft.append(W("PCI_SCOPE", "info",
                      "Taking payments: use the gateway's hosted checkout/SDK so card data never touches your "
                      "servers (PCI DSS SAQ A).", ["requirements.payments"]))
    if r["payments"] and r["auth"] == "none":
        soft.append(W("PAYMENTS_WITHOUT_AUTH", "warning",
                      "payments=true but auth=none: refunds, receipts and disputes usually need user accounts.",
                      ["requirements.payments", "requirements.auth"]))
    if r["multi_tenant"] and r["auth"] == "none":
        soft.append(W("TENANCY_WITHOUT_AUTH", "warning",
                      "multi_tenant=true needs authenticated users to know which tenant a request belongs to.",
                      ["requirements.multi_tenant", "requirements.auth"]))
    if "hipaa" in c.compliance and c.residency not in ("us", "none"):
        soft.append(W("HIPAA_SCOPE", "info", "HIPAA is a US law; confirm it applies alongside the %s residency."
                      % c.residency, ["compliance"]))
    if c.type == "healthtech" and not ({"hipaa", "dpdp", "gdpr"} & set(c.compliance)):
        soft.append(W("HEALTH_DATA", "info",
                      "Health data is sensitive personal data under DPDP/GDPR and PHI under HIPAA; confirm which "
                      "regimes apply.", ["compliance", "project.type"]))
    if "rbi_localisation" in c.compliance:
        soft.append(W("RBI_LOCALISATION", "info",
                      "RBI storage directive (2018): the entire payment data, including backups, logs and the DR copy, "
                      "must stay in India. Use an Indian DR region (e.g. AWS ap-south-2 Hyderabad / Azure South "
                      "India) and keep observability data in-country.", ["compliance", "infrastructure.data_residency"]))
    if "pci_dss" in c.compliance and r["payments"]:
        soft.append(W("PCI_DSS_SCOPE", "info",
                      "PCI DSS: keep card data out of your systems with the gateway's tokenised checkout; your own "
                      "scope then covers the pages that load it, plus access control, logging and quarterly scans.",
                      ["compliance", "requirements.payments"]))
    if "dpdp" in c.compliance:
        soft.append(W("DPDP_NOTES", "info",
                      "DPDP Act 2023: collect consent per purpose, honour erasure requests and report breaches; the "
                      "Act itself does not force India-only storage unless data_residency requires it.",
                      ["compliance"]))
    if "gdpr" in c.compliance and c.residency == "none":
        soft.append(W("GDPR_REGION", "info",
                      "GDPR: an EU region is the simplest option; otherwise rely on the provider's DPA and SCCs.",
                      ["compliance", "infrastructure.data_residency"]))
    if r["ml_features"] == "training" and c.budget < 300:
        soft.append(W("ML_TRAINING_BUDGET", "warning",
                      "Model training needs GPU time; $%g/month leaves little room." % c.budget,
                      ["requirements.ml_features", "budget.monthly_infra_usd"]))
    if c.data_gb and c.budget and c.data_gb * 0.05 > c.budget:
        soft.append(W("STORAGE_VS_BUDGET", "warning",
                      "%.0f GB of data costs more to store than the $%g budget allows." % (c.data_gb, c.budget),
                      ["scale.data_volume_gb_12_months", "budget.monthly_infra_usd"]))
    if not c.langs:
        assumptions.append("team.languages is empty: language fit was not used; popular, easy-to-hire options "
                           "were preferred.")
    if c.cloud in ("aws", "gcp", "azure"):
        other = [v for k, v in c.hints if k == "cloud" and v != c.cloud]
        if other:
            soft.append(W("CROSS_CLOUD_INTEGRATION", "info",
                          "Existing systems live on %s while cloud_preference=%s; plan network connectivity "
                          "(VPN/private link) and identity federation." % (", ".join(other).upper(), c.cloud),
                          ["infrastructure.existing_systems", "infrastructure.cloud_preference"]))
    for kind, val in c.hints:
        if kind == "db" and val == "sqlserver" and c.deploy == "managed_cloud":
            soft.append(W("EXISTING_DB_INTEGRATION", "info",
                          "An existing SQL Server is listed: migrate the relevant tables to the managed database "
                          "(Azure Database Migration Service / DMS) or reach the on-prem server through a private "
                          "connection; do not expose it publicly.", ["infrastructure.existing_systems"]))
    return hard, soft
