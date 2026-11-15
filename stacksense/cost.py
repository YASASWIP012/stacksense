"""Order-of-magnitude monthly cost bands (USD).

Bands are built from public list prices (see report references) for the
smallest sensible production tier at each scale level. They are not quotes.
"""
from . import kb

# managed relational DB by scale level (single instance; doubled for multi-AZ)
BIG3_RDB = {1: (15, 35), 2: (30, 120), 3: (150, 800), 4: (400, 2500), 5: (2500, 15000)}
PAAS_RDB = {1: (7, 20), 2: (15, 100), 3: (100, 500), 4: (500, 3000), 5: (3000, 15000)}
SERVERLESS_DB = {1: (0, 5), 2: (5, 50), 3: (50, 500), 4: (300, 2500), 5: (2500, 15000)}
ATLAS = {1: (0, 10), 2: (57, 150), 3: (200, 1000), 4: (1000, 6000), 5: (6000, 30000)}
NEON = {1: (0, 19), 2: (19, 70), 3: (70, 700), 4: (700, 4000), 5: (4000, 20000)}
AZURE_SQL = {1: (5, 15), 2: (15, 150), 3: (150, 1000), 4: (1000, 6000), 5: (6000, 30000)}
SELF_DB = {1: (0, 0), 2: (0, 20), 3: (40, 300), 4: (300, 2000), 5: (2000, 10000)}


def db_service_cost(did, prov, selfm, c):
    L = c.L
    if selfm:
        base = SELF_DB[L]
    elif did in ("dynamodb", "firestore", "cassandra"):
        base = SERVERLESS_DB[L]
    elif prov == "atlas":
        base = ATLAS[L]
    elif prov == "neon":
        base = NEON[L]
    elif prov == "supabase":
        base = {1: (0, 25), 2: (25, 60), 3: (60, 400), 4: (400, 2500), 5: (2500, 15000)}[L]
    elif did == "sqlserver" and prov == "azure":
        base = AZURE_SQL[L]
    elif prov in ("aws", "gcp", "azure"):
        base = BIG3_RDB[L]
    else:
        base = PAAS_RDB[L]
    mn, mx = base
    if c.req["availability"] in ("99.95", "99.99") and not selfm:
        mn, mx = mn * 2, mx * 2
    storage = c.data_gb or 0
    mn += storage * 0.10
    mx += storage * 0.25
    return (int(round(mn)), int(round(mx)))


def estimate(c, combo, dbs, supporting, host_pid):
    host = combo["hosting"]
    be = combo["backend"]
    lo, hi = host["cost"][c.L]
    assumptions = []
    if host["id"] == "vercel" and not c.commercial and c.L == 1:
        lo, hi = 0, 0
        assumptions.append("Vercel Hobby is free for non-commercial personal projects")
    if c.req["availability"] in ("99.95", "99.99") and host["id"] not in ("github_pages", "cloudflare_pages", "netlify"):
        lo, hi = lo * 1.5, hi * 1.5
        assumptions.append("Compute sized for at least two zones for %s%% availability" % c.req["availability"])
    items = [("hosting", lo, hi)]
    if be["cost"] is not kb.FREE:
        bl, bh = be["cost"][c.L]
        if be["id"] == "supabase" and (c.commercial or (c.data_gb or 0) > 1 or c.users > 1000):
            bl = max(bl, 25)
            bh = max(bh, 60)
            assumptions.append("Supabase Pro ($25/month) for production: free projects pause when idle and include ~1 GB of file storage")
        items.append(("backend", bl, bh))
    if dbs and not dbs.get("included"):
        items.append(("database", dbs["cost"][0], dbs["cost"][1]))
    for s in supporting:
        cs = s.get("estimated_monthly_usd")
        if cs:
            items.append((s["layer"], cs[0], cs[1]))
    mn = sum(x[1] for x in items)
    mx = sum(x[2] for x in items)
    assumptions.insert(0, "Scale level L%d: ~%d users, ~%d peak concurrent, ~%d req/s at peak, ~%s GB stored" % (
        c.L, c.users, c.peak, c.rps, ("%g" % c.data_gb)))
    assumptions.append("List prices for the smallest production tier that fits; no reserved-instance or startup "
                       "credits; excludes domains, app-store fees, SMS and payment-gateway transaction fees")
    if c.deploy in ("on_premise", "air_gapped"):
        assumptions.append("On-premise: hardware purchase (capex) is excluded; figure covers power, licences and "
                           "maintenance contracts only")
    breakdown = {k: [int(round(a)), int(round(b))] for k, a, b in items}
    return {"min": int(round(mn)), "max": int(round(mx)), "assumptions": assumptions, "breakdown": breakdown}
