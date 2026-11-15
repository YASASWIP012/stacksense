"""Supporting services: auth, payments, storage, CDN, cache, queue, search,
realtime, ML, CI/CD, observability and other integrations.

Each layer is added only when an input calls for it, and each choice respects
the same hard constraints as the core layers (deployment model, cloud
preference, data residency, compliance).
"""
import re

from . import kb

# SaaS region facts for supporting vendors (conservative; see kb sources)
SAAS = {
    "auth0": {"india": False, "eu": True, "us": True, "none": True, "src": "https://auth0.com/docs/deploy-monitor/deploy-public-cloud/public-cloud-regions"},
    "pusher": {"india": True, "eu": True, "us": True, "src": "https://pusher.com/docs/channels/miscellaneous/clusters/"},
    "sentry": {"india": False, "eu": True, "us": True, "src": "https://docs.sentry.io/organization/data-storage-location/"},
    "msg91": {"india": True, "eu": False, "us": False, "src": "https://msg91.com/help/what-is-dlt"},
    "workos": {"india": False, "eu": False, "us": True, "src": "https://workos.com/security"},
    "twilio": {"india": False, "eu": True, "us": True, "src": "https://www.twilio.com/docs/verify"},
}


def S(layer, choice, why, driven, alts, trade=None, cost=(0, 0), **extra):
    d = {"layer": layer, "choice": choice, "justification": why, "driven_by": driven,
         "alternatives": [{"choice": a, "why_not": b} for a, b in alts], "tradeoffs": trade or [],
         "estimated_monthly_usd": list(cost)}
    d.update(extra)
    return d


def _saas_ok(name, c):
    if c.deploy in ("on_premise", "air_gapped"):
        return False
    z = c.eff_residency
    if z in ("india", "eu", "us"):
        return SAAS[name].get(z, False)
    return True


def _india(c):
    text = (c.spec.get("project.description") or "").lower()
    return (c.eff_residency == "india" or "rbi_localisation" in c.compliance or "dpdp" in c.compliance
            or "india" in text or "upi" in text)


def _domain_items(c, combo, dbs, hp, onprem, prior=()):
    out = []
    d = (c.spec.get("project.description") or "").lower()
    if c.type == "iot" or "timeseries" in c.signals:
        if onprem:
            ch = "EMQX or Eclipse Mosquitto (self-hosted MQTT broker) -> stream into the database"
        else:
            ch = {"aws": "AWS IoT Core (MQTT) -> Amazon Kinesis Data Streams", "gcp": "EMQX Cloud/Mosquitto on GKE (MQTT) -> Pub/Sub",
                  "azure": "Azure IoT Hub (MQTT) -> Event Hubs"}.get(hp, "EMQX (managed MQTT broker) -> stream consumer")
        out.append(S("other", ch, "Devices publish telemetry over MQTT (lightweight, works on flaky links); the broker authenticates "
                     "each device and buffers bursts before a consumer writes batches to the time-series store.",
                     ["project.type", "project.description"],
                     [("Devices calling the REST API directly", "HTTP per reading wastes battery and collapses under bursts.")],
                     ["Device provisioning and certificate rotation must be planned."], (10, 300)))
    ex_wh = next((x for x in c.existing if any(k in x.lower() for k in ("snowflake", "bigquery", "redshift", "databricks"))), None)
    if ex_wh:
        out.append(S("other", "%s (existing warehouse, reused)" % ex_wh, "The organisation already runs this warehouse; analytical "
                     "queries and dashboards use it rather than adding a second one.", ["infrastructure.existing_systems", "project.type"],
                     [("A new warehouse", "Duplicates data, cost and governance.")], [], (0, 0)))
    elif c.type == "data_analytics" and not onprem:
        ch = {"gcp": "BigQuery (serverless warehouse) + Looker Studio", "aws": "Amazon Redshift Serverless or Athena on S3",
              "azure": "Microsoft Fabric / Azure Synapse"}.get(hp, "ClickHouse Cloud or BigQuery")
        out.append(S("other", ch, "Analytical queries over large volumes run in a columnar warehouse with no servers to manage; the "
                     "application database stays small and fast.", ["project.type", "scale.data_volume_gb_12_months"],
                     [("Self-managed ClickHouse", "Fastest per dollar but needs operations.")], ["Per-query pricing needs guardrails."], (20, 500)))
    if c.type == "api_platform":
        ch = {"aws": "Amazon API Gateway (usage plans + API keys)", "gcp": "Google Cloud API Gateway (API keys + quotas; Apigee only for enterprise monetisation)", "azure": "Azure API Management"}.get(
            hp, "Kong Gateway (self-hosted)" if c.devops else "Zuplo (managed API gateway)") if not onprem else "Kong Gateway (self-hosted)"
        out.append(S("other", ch, "API products need per-customer keys, rate limits, quotas and usage metering at the edge; the "
                     "gateway also publishes the OpenAPI developer portal.", ["project.type", "project.platforms"],
                     [("Rate limiting inside the app", "Every service re-implements keys and quotas.")], [], (0, 100) if c.L <= 2 else (50, 600)))
    if "video" in d and any(k in d for k in ("consult", "telehealth", "call", "session", "class", "tutor", "meeting", "proctor", "webcam")) or "webcam" in d:
        hip = "hipaa" in c.compliance
        ch = "LiveKit (self-hosted in your cloud)" if (hip or onprem or c.eff_residency == "india") else "Daily (managed WebRTC video)"
        out.append(S("realtime", ch, "Video consultations need WebRTC with TURN relays; %s" % (
            ("self-hosting keeps media inside your %s boundary." % ("HIPAA and residency" if hip else "data-residency/compliance")) if ch.startswith("LiveKit") else "a managed API avoids running media servers."),
            ["project.description"] + (["compliance"] if hip else []),
            [("Zoom Video SDK", "Per-minute pricing; branding constraints."), ("Peer-to-peer WebRTC only", "Fails behind strict NATs without TURN.")],
            ["Media servers need bandwidth headroom."], (50, 600)))
    if c.type == "content_site" and any(k in d for k in ("editor", "newsroom", "journalist", "writers", "news", "cms", "authors", "marketing team")):
        ch = "WordPress (headless) + WPGraphQL" if "php" in c.langs else "Payload CMS (self-hosted) or Strapi"
        out.append(S("other", ch, "Several non-developer editors need drafts, roles, scheduling and media management; the site "
                     "pulls content from the CMS at build time and the CDN serves the pages.", ["project.type", "team.size"],
                     [("Markdown in Git", "Fine for one developer-author, not for an editorial team.")], [], (10, 60)))
    if re.search(r"online judge|coding judge|code execution|run (user|their) code|submit code|compile and run", d):
        out.append(S("other", "Judge0 (self-hosted) on isolated worker VMs for sandboxed code execution",
                     "Running untrusted submissions needs sandboxes with CPU/memory/time limits and no network; Judge0 provides "
                     "isolate-based sandboxes for 60+ languages, run on separate worker machines fed by the queue, never on the API servers.",
                     ["project.description"],
                     [("Executing code in the API process", "Remote-code-execution risk and noisy-neighbour slowdowns."),
                      ("Judge0 hosted API (RapidAPI)", "Fine to start, but per-call pricing and a third party sees submissions.")],
                     ["Worker capacity must match contest-time submission spikes."], (40, 400), separate_service=True))
    if re.search(r"podcast|audio episodes?|audiobook", d):
        out.append(S("other", "Audio pipeline: FFmpeg transcode (MP3/AAC, loudness-normalised) + CDN delivery + RSS feed generation",
                     "Uploaded episodes are transcoded and normalised in background jobs, served from object storage through a CDN "
                     "(egress dominates cost), and published via podcast RSS feeds that Apple/Spotify ingest.",
                     ["project.description", "requirements.file_media_storage"],
                     [("Serving raw uploads", "Inconsistent formats and loudness; no feed for podcast apps.")],
                     ["CDN egress grows with downloads."], (20, 300)))
    if "newsletter" in d or "email campaign" in d or "mailing list" in d:
        ch = "Amazon SES (newsletter sending) + double opt-in list" if hp == "aws" else "Buttondown / Resend (newsletter + transactional email)"
        out.append(S("other", ch, "The description mentions a newsletter: a sending service handles deliverability (SPF/DKIM/DMARC), "
                     "unsubscribes and bounces, which the site should not do itself.", ["project.description"],
                     [("Sending from your own SMTP server", "Poor deliverability and blocklist risk.")], [], (0, 30)))
    if re.search(r"\bpos\b|point[- ]of[- ]sale|in-store|card terminal", d):
        ch = "Razorpay POS / Pine Labs (card-present terminals)" if _india(c) else "Stripe Terminal (card-present SDK + certified readers)"
        out.append(S("payments", ch, "In-store card payments need certified card-present readers and an SDK that works with the terminal app; "
                     "hosted web checkout is for online payments only. Non-card sales are queued locally while offline.",
                     ["project.description", "requirements.payments"],
                     [("Hosted online checkout", "Not usable for card-present sales at a counter.")], ["Reader hardware cost per terminal."], (0, 0),
                     replaces_online=True))
    if re.search(r"app stores?|in-app purchase|play billing|apple subscription", d) or (
            "subscription" in c.signals and "web" not in c.platforms and ("android" in c.platforms or "ios" in c.platforms)):
        out.append(S("payments", "App Store / Google Play Billing via RevenueCat", "Digital subscriptions sold inside mobile apps must use the "
                     "stores' billing; RevenueCat unifies receipts, entitlements and webhooks across both stores.",
                     ["project.platforms", "project.description"],
                     [("Stripe in the app", "Store rules generally forbid external payment for digital content.")],
                     ["Store commission of 15-30%."], (0, 0), replaces_online=True))
    if ("leaderboard" in d or "ranking" in d) and not any(x["layer"] == "cache" for x in prior):
        ch = {"aws": "Amazon ElastiCache for Valkey (sorted sets)", "gcp": "Memorystore for Valkey (sorted sets)",
              "azure": "Azure Managed Redis (sorted sets)"}.get(hp, "Valkey/Redis sorted sets")
        out.append(S("cache", ch, "Leaderboards are sorted sets: ZADD/ZREVRANGE give rank and top-N in O(log n), well inside a tight p95; "
                     "scores are persisted to the database for history.", ["project.description", "requirements.p95_latency_ms"],
                     [("ORDER BY on the database", "Rank queries get slow and hot under write-heavy score updates.")], [], (15, 200)))
    if "video" in d and any(k in d for k in ("lecture", "course", "recorded", "stream", "vod", "on-demand")):
        if c.eff_residency == "india" or hp == "aws":
            ch = "S3 + AWS Elemental MediaConvert (HLS) + CloudFront (%s)" % kb.PROVIDERS["aws"]["region_label"].get(c.eff_residency if c.eff_residency != "none" else ("eu" if "gdpr" in c.compliance else "none"), "same region as the database")
        elif hp == "azure":
            ch = "Azure Blob + transcoding (FFmpeg jobs) + Azure Front Door"
        else:
            ch = "Mux or Cloudflare Stream (managed transcoding + HLS delivery)"
        out.append(S("other", ch, "Recorded video must be transcoded to adaptive HLS so it plays on low bandwidth, and delivered from a CDN; "
                     "egress, not storage, dominates the cost.", ["project.description", "requirements.file_media_storage"],
                     [("Serving MP4 files from object storage", "No adaptive bitrate: buffering on mobile networks.")],
                     ["Video egress cost grows with watch time."], (30, 800)))
    if c.type == "ecommerce" and (c.L >= 3 or any(k in d for k in ("flash", "oversell", "limited stock"))) and (c.traffic == "spiky" or "flash" in d):
        out.append(S("other", "Inventory reservation (atomic Valkey DECR + DB row lock) and a virtual waiting room",
                     "Flash sales oversell when many checkouts read the same stock; reserving stock atomically in Valkey, confirming "
                     "with a row lock in the transaction, and queueing excess visitors keeps orders correct under spikes.",
                     ["scale.traffic_pattern", "project.description"],
                     [("Checking stock in application code", "Race conditions oversell under concurrency.")], [], (0, 50)))
    if "hipaa" in c.compliance and not onprem:
        out.append(S("other", "HIPAA programme: BAAs with every vendor that touches PHI", "Sign a Business Associate Agreement with "
                     "the cloud provider and each PHI-handling vendor before launch, keep PHI out of logs, push payloads and analytics, "
                     "and enable audit logging on every PHI store.", ["compliance"],
                     [("Relying on vendor marketing", "HIPAA eligibility only applies to services listed under a signed BAA.")], [], (0, 0)))
    return out


def _push_use(c):
    d = (c.spec.get("project.description") or "").lower()
    if re.search(r"\bemis?\b|\bloans?\b", d):
        return "EMI due-date reminders and disbursal/repayment status"
    if c.type == "fintech":
        return "transaction alerts and security notices"
    if c.req["offline_support"]:
        return "sync-status nudges and new assignments"
    if "booking" in c.signals or c.type == "marketplace":
        return "booking and job-status updates"
    if c.req["realtime"]:
        return "messages that arrive while the app is closed"
    if c.type == "healthtech":
        return "appointment reminders (no health data in the payload)"
    return "account and activity notifications"


MIGRATE = {"mysql": "MySQL", "postgres": "PostgreSQL", "sqlserver": "SQL Server", "mongodb": "MongoDB",
           "dynamodb": "DynamoDB", "firestore": "Firestore"}


def _existing_plan(c, combo, dbs, hp):
    """Say how each existing system is reused, integrated or migrated."""
    items = []
    if not c.existing:
        return items
    db_id = combo["database"]["id"]
    lines = []
    for item in c.existing:
        low = item.lower()
        if any(k in low for k in ("mysql", "mariadb", "postgres", "sql server", "mssql", "mongo", "oracle", "dynamo")):
            src = item
            if db_id != "none" and (("mysql" in low and db_id == "mysql") or ("postgres" in low and db_id == "postgres")
                                    or (("sql server" in low or "mssql" in low) and db_id == "sqlserver")
                                    or ("mongo" in low and db_id == "mongodb")):
                tool = {"aws": "AWS DMS", "azure": "Azure Database Migration Service", "gcp": "Database Migration Service"}.get(hp, "native dump/restore with a short read-only window")
                eol = any(__import__("re").search(pat, low) for pat, *_ in kb.EOL)
                lines.append("%s: same engine as the recommended database, so lift the schema and data with %s%s." % (
                    src, tool, " and upgrade to a supported version in the same step" if eol else ""))
            elif c.type == "data_analytics":
                lines.append("%s: keep it as the system of record and replicate changes into the analytics store with "
                             "change-data-capture (e.g. Debezium/Oracle GoldenGate or the cloud DMS in CDC mode); do not "
                             "migrate or retire it." % src)
            elif db_id != "none":
                tool = (("ora2pg (or AWS SCT + DMS)" if hp == "aws" else "ora2pg") if "oracle" in low else "pgloader") if db_id == "postgres" else {"aws": "AWS DMS", "azure": "Azure DMS", "gcp": "Database Migration Service"}.get(hp, "an ETL script")
                lines.append("%s: migrate to %s with %s; run both in parallel with change-data-capture until "
                             "cut-over, then retire the old server." % (src, dbs["name"].split(" (")[0], tool))
        elif any(k in low for k in ("quickbooks", "xero", "zoho", "shopify", "hubspot", "tally", "freshdesk", "zendesk", "odk", "whatsapp", "google workspace", "slack", "gmail", "notion", "jira")):
            lines.append("%s: a third-party product; keep it and integrate through its official API/webhooks behind an adapter "
                         "module (OAuth app, retry with idempotency keys)." % item)
        elif any(k in low for k in ("node", "api", "service", "backend", "php", "java", "django", "rails")):
            lines.append("%s: keep it running behind the new API (strangler pattern) and move endpoints one at a "
                         "time; upgrade the runtime first if it is end-of-life." % item)
        elif any(k in low for k in ("angular", "react", "vue", "admin", "frontend", "jquery")):
            lines.append("%s: replace screen by screen in the recommended front end; link both from one "
                         "navigation until the old UI is empty." % item)
        elif any(k in low for k in ("redis", "valkey", "kafka", "rabbitmq", "active directory", "entra", "okta", "keycloak",
                                    "microsoft 365", "office 365", "google workspace", "salesforce", "sap", "twilio", "firebase", "stripe", "razorpay", "ldap",
                                    "ibm mq", "activemq", "snowflake", "bigquery", "redshift", "databricks", "github pages")):
            continue  # handled by the matching layer (cache/queue/auth/integration)
        elif any(k in low for k in ("centos", "ubuntu", "windows server", "servers", "vm")):
            lines.append("%s: do not deploy new services there; move workloads to the recommended hosting and "
                         "decommission once empty." % item)
        else:
            if not any(k in low for k in ("redis", "valkey", "kafka", "rabbitmq", "active directory", "entra", "okta", "keycloak",
                                          "microsoft 365", "office 365", "google workspace", "salesforce", "sap", "twilio",
                                          "firebase", "stripe", "razorpay", "oracle", "mysql", "postgres", "sql server", "mongo",
                                          "ibm mq", "activemq", "snowflake", "bigquery", "redshift", "databricks", "github pages")):
                lines.append("%s: not modelled by the knowledge base; keep it and integrate through its documented API or file "
                             "exports behind an adapter module, with an integration test in CI." % item)
    if lines:
        items.append(S("other", "Migration and integration plan for existing systems",
                       " ".join(lines), ["infrastructure.existing_systems"],
                       [("Big-bang rewrite", "Higher risk: everything switches at once with no fallback.")],
                       ["Running old and new systems in parallel costs extra for a few weeks."], (0, 0)))
    return items


def choose_supporting(c, combo, dbs, hp):
    hp = {"azure_swa": "azure", "firebase": "gcp"}.get(hp, hp)
    out, warns = [], []
    be = combo["backend"]["id"]
    db = combo["database"]["id"]
    host = combo["hosting"]
    r = c.req
    onprem = c.deploy in ("on_premise", "air_gapped")
    airgap = c.deploy == "air_gapped"
    if be == "none":
        if r["full_text_search"]:
            out.append(S("search", "Pagefind (static search index built at deploy time)",
                         "Search runs entirely in the browser from an index generated during the build, so a static site needs no search server or budget.",
                         ["requirements.full_text_search", "budget.monthly_infra_usd"],
                         [("Algolia DocSearch", "Free for open-source docs, but an external service and application."),
                          ("Postgres/Meilisearch", "Would add a server to a site that has none.")], [], (0, 0)))
        if r["payments"]:
            pay = "Razorpay Payment Pages / Payment Button" if _india(c) else "Stripe Payment Links"
            out.append(S("payments", pay, "A hosted payment page handles the donation/checkout, receipts and card/UPI data, so the static site "
                         "only links to it: no backend, PCI scope stays with the gateway.", ["requirements.payments"],
                         [("Custom checkout with a backend", "Needs a server and webhooks for no benefit at this scale.")],
                         ["Gateway transaction fees apply."], (0, 0)))
        d0 = (c.spec.get("project.description") or "").lower()
        if "newsletter" in d0 or "mailing list" in d0:
            out.append(S("other", "Buttondown (hosted newsletter: signup form + sending)", "The description mentions a newsletter; a hosted "
                         "service provides the embeddable signup form, double opt-in, unsubscribes and deliverability, so the static "
                         "site still needs no backend.", ["project.description"],
                         [("Running your own mail server", "Deliverability and spam-list risk.")], [], (0, 9)))
        out.append(S("ci_cd", "GitHub Actions (build and deploy on push)",
                     "Builds the static site on every push to main and deploys it; free for public repos.",
                     ["project.type", "budget.monthly_infra_usd"],
                     [("Host's built-in Git integration", "Equally fine; Actions keeps build steps in the repo.")]))
        out.append(S("observability", "Free uptime check (e.g. UptimeRobot) + host analytics",
                     "A static site only needs to know it is up; no servers to monitor.",
                     ["requirements.availability"],
                     [("Sentry", "No server code to instrument.")]))
        return out, warns
    L = c.L
    hints = dict(c.hints)
    idp = [v for k, v in c.hints if k == "idp"]

    # ---------------------------------------------------------------- auth
    if r["auth"] != "none":
        drv = ["requirements.auth"]
        if be in ("supabase", "firebase") and r["auth"] == "enterprise_sso" and idp:
            be_auth_override = True
        else:
            be_auth_override = False
        if be == "supabase" and not be_auth_override:
            ch, why = "Supabase Auth", "Included in the Supabase project; issues JWTs that Postgres RLS policies check."
            alts = [("Clerk", "Separate vendor; user data stored in the US.")]
        elif be == "firebase" and not be_auth_override:
            ch, why = "Firebase Authentication", "Included with Firebase; drop-in UI for email, phone and social sign-in."
            alts = [("Auth0", "Extra vendor for no gain here.")]
        elif r["auth"] == "enterprise_sso":
            drv.append("infrastructure.existing_systems")
            has_entra = "entra" in idp
            if onprem:
                if "ad_onprem" in idp or has_entra:
                    ch = "Keycloak (self-hosted) federated to the existing directory (Active Directory/LDAP)"
                    why = ("The deployment is %s, so no cloud identity service is allowed. Keycloak runs inside the "
                           "network, federates users from the existing directory over LDAP/Kerberos and gives the app OIDC/SAML "
                           "single sign-on." % c.deploy.replace("_", "-"))
                    alts = [("Microsoft Entra ID", "Cloud service: not reachable from a %s network." % c.deploy.replace("_", "-")),
                            ("AD FS", "Also on-prem, but Windows-only and SAML/WS-Fed oriented; Keycloak covers OIDC for modern apps.")]
                else:
                    ch = "Keycloak (self-hosted)"
                    why = "Open-source IdP with SAML/OIDC that runs inside your network; no external identity service."
                    alts = [("Auth0 / Okta", "SaaS: not allowed for a %s deployment." % c.deploy.replace("_", "-"))]
                drv.append("infrastructure.deployment_model")
            elif "okta" in idp and ("okta" in (c.spec.get("project.description") or "").lower() or not has_entra):
                ch, why = "Okta (existing IdP via OIDC)", "Reuses the company's Okta tenant named in the inputs; no new passwords or directory."
                alts = [("Microsoft Entra ID", "Also listed, but the description names Okta as the SSO to use.") if has_entra else ("Keycloak", "Duplicates the existing IdP.")]
            elif has_entra or "ad_onprem" in idp:
                ch = "Microsoft Entra ID (OIDC/SAML single sign-on)"
                why = ("The organisation's directory is Microsoft (listed in existing_systems), so the app trusts Entra ID "
                       "via OpenID Connect: no new passwords, and joiners/leavers are handled centrally.")
                if "ad_onprem" in idp:
                    why += " On-prem Active Directory syncs into Entra ID with Entra Connect."
                if host["id"] == "azure_app_service":
                    why += " App Service 'Easy Auth' can enforce sign-in with no auth code."
                alts = [("Keycloak", "Would duplicate the identity provider the company already runs.")]
            elif "okta" in idp:
                ch, why = "Okta (existing IdP via OIDC)", "Reuses the company's Okta tenant listed in existing_systems."
                alts = [("Keycloak", "Duplicates the existing IdP.")]
            elif "google" in idp:
                ch = "Google Workspace SSO (OIDC)" + (" + Identity-Aware Proxy" if hp == "gcp" and c.type == "internal_tool" else "")
                why = ("Staff already have Google Workspace accounts (existing_systems), so sign-in uses them directly"
                       "%s." % ("; IAP puts the whole internal app behind Google login with no auth code" if hp == "gcp" and c.type == "internal_tool" else ""))
                alts = [("Auth0", "Per-MAU fees to re-implement a login the company already has.")]
            elif c.type in ("saas_b2b",) or r["multi_tenant"]:
                if c.devops:
                    ch, why = "Keycloak (self-hosted, Organizations per customer)", "Each customer connects its own SAML/OIDC IdP; no per-MAU fees."
                    alts = [("WorkOS / Auth0 Organizations", "Faster to start but priced per connection/MAU.")]
                else:
                    ch, why = "WorkOS (enterprise SSO per customer)", "Customers plug in their own Okta/Entra/Google IdP without you running an IdP."
                    alts = [("Keycloak", "Needs someone to operate it.")]
                    if not _saas_ok("workos", c):
                        ch, why = ("Keycloak (self-hosted, Organizations per customer)",
                                   "Customer IdPs federate into a Keycloak you host in the required region (data_residency=%s); "
                                   "WorkOS keeps its data in the US." % c.eff_residency)
                        alts = [("WorkOS / Auth0", "No guaranteed %s data region." % c.eff_residency.upper())]
                        drv.append("infrastructure.data_residency")
            elif c.cloud == "azure" or hp == "azure":
                ch, why = "Microsoft Entra ID (workforce SSO)", "Managed SSO native to the chosen Azure platform; federates with other corporate IdPs."
                alts = [("Keycloak", "Needs someone to operate it.")]
            elif c.devops:
                ch, why = "Keycloak (self-hosted)", "Open-source IdP with SAML/OIDC brokering to any corporate directory."
                alts = [("Auth0 / Okta", "Per-user pricing for a capability you can run.")]
            elif c.cloud == "aws" or hp == "aws":
                ch, why = "Amazon Cognito (SAML/OIDC federation)", "Managed user pools in the same AWS region federating to the customer IdP."
                alts = [("Keycloak", "Needs someone to operate it.")]
            else:
                ch, why = "Auth0 by Okta (Enterprise Connections)", "Managed SSO to many corporate IdPs without running an IdP."
                alts = [("Keycloak", "Needs someone to operate it.")]
                if not _saas_ok("auth0", c):
                    ch, why = "Keycloak (self-hosted)", "Auth0 has no %s region, so the IdP runs next to the app." % c.eff_residency
                    alts = [("Auth0", "No region for data_residency=%s." % c.eff_residency)]
                    drv.append("infrastructure.data_residency")
        elif onprem:
            ch, why = "Keycloak (self-hosted)", "Runs inside your network; no external identity service."
            alts = [("Framework built-in auth", "Keycloak centralises accounts for future apps.")]
            drv.append("infrastructure.deployment_model")
        elif be == "aws_lambda":
            ch, why = "Amazon Cognito", "Managed user pools that API Gateway authorizers verify directly."
            alts = [("Auth0", "Extra vendor; Cognito is native to API Gateway.")]
        elif r["auth"] == "social" and c.type in ("saas_b2b", "realtime_collab") and r["multi_tenant"]:
            if _saas_ok("auth0", c) and c.users <= 200000:
                ch = "Auth0 by Okta (Organizations, %s tenant)" % ("EU" if ("gdpr" in c.compliance or c.eff_residency == "eu") else "US")
                why = ("Multi-tenant B2B SaaS with social login: Auth0 Organizations models each customer team (invites, roles, "
                       "per-tenant login) as a managed service, so engineering time goes into the product rather than running an IdP"
                       "%s." % ("; the EU tenant keeps identity data in the EU for GDPR" if ("gdpr" in c.compliance or c.eff_residency == "eu") else ""))
                alts = [("Keycloak (self-hosted, Organizations)", "No per-MAU fees, but the team must run, patch and back up an IdP."),
                        ("Clerk", "Stores data in the US; weaker fit for GDPR-minded EU customers.")]
            elif c.devops:
                ch = "Keycloak (self-hosted, Organizations for tenants)"
                why = ("Multi-tenant B2B SaaS with social login: Keycloak's Organizations feature models customer "
                       "teams, and the DevOps engineer can run it. No per-MAU fees as seats grow.")
                alts = [("Auth0 Organizations", "Per-MAU pricing climbs quickly as seats grow."),
                        ("Clerk", "Stores data in the US; weaker fit for GDPR-minded EU customers.")]
            else:
                ch, why = "Auth0 by Okta (Organizations)", "Managed social login with per-customer organisations."
                alts = [("Keycloak", "Needs someone to operate it.")]
            drv += ["requirements.multi_tenant", "team.has_devops"]
        else:
            lib = {"django": "Django auth + django-allauth", "nestjs": "Passport.js (NestJS) with JWT sessions",
                   "fastify": "@fastify/auth + JWT sessions", "fastapi": "FastAPI security utilities + Authlib",
                   "spring_boot": "Spring Security (+ OAuth2 client)", "aspnet": "ASP.NET Core Identity",
                   "go_chi": {"aws": "Amazon Cognito", "gcp": "Google Identity Platform", "azure": "Microsoft Entra External ID"}.get(hp, "Ory Kratos (self-hosted identity server)"), "laravel": "Laravel Fortify + Socialite",
                   "rails": "Devise + OmniAuth", "phoenix": "phx.gen.auth + Ueberauth",
                   "next_fullstack": "Auth.js (NextAuth)", "azure_functions": "Microsoft Entra External ID"}.get(be, "Framework built-in auth")
            ch = lib
            if lib.startswith(("Amazon Cognito", "Google Identity Platform", "Microsoft Entra External ID")):
                why = ("Managed user directory in the same cloud%s, so the service only verifies tokens instead of storing passwords." % (
                    " and region" if c.eff_residency in ("india", "eu", "us") else ""))
                alts = [("Ory Kratos (self-hosted)", "Another service to run for standard email/social login.")]
            elif lib.startswith(("Ory Kratos", "Keycloak")):
                why = "Open-source identity server run next to the API, so user data stays in your own infrastructure%s." % (
                    " in %s" % c.eff_residency.upper() if c.eff_residency in ("eu", "us") else (" in India" if c.eff_residency == "india" else ""))
                alts = [("Auth0 / Clerk", "Separate vendor with per-MAU pricing.")]
            else:
                why = "Uses the backend framework's maintained auth library, so user data stays in your own database%s." % (
                    " in %s" % c.eff_residency.upper() if c.eff_residency in ("eu", "us") else (" in India" if c.eff_residency == "india" else ""))
                alts = [("Auth0 / Clerk", "Separate vendor with per-MAU pricing%s." % (
                    "; no India region" if c.eff_residency == "india" else ""))]
        dsa = (c.spec.get("project.description") or "").lower()
        if re.search(r"aadhaar|digilocker|e-?kyc", dsa) and not airgap:
            ch = "%s + Aadhaar OTP/eKYC via a UIDAI-licensed AUA/KUA partner or DigiLocker OAuth" % ch
            why += (" Aadhaar-based login/verification cannot use a plain SMS OTP: it must go through a UIDAI-licensed "
                    "AUA/KUA (or DigiLocker's OAuth), and Aadhaar numbers must be stored only as tokens/reference keys.")
            drv.append("project.description")
        if r["auth"] == "phone_otp" and not airgap:
            sms = "MSG91 (DLT-registered SMS OTP)" if _india(c) else "Twilio Verify"
            ch = "%s + %s" % (ch, sms)
            why += (" Phone OTP is sent through %s; in India every SMS template and sender ID must be registered on "
                    "the TRAI DLT platform first." % sms.split(" (")[0]) if _india(c) else " OTPs are sent through Twilio Verify."
            drv.append("requirements.auth")
            alts.append(("Firebase phone auth", "Firebase Authentication processes data in the US."))
        out.append(S("auth", ch, why, sorted(set(drv)), alts,
                     ["Sign-in depends on the corporate IdP's availability; keep a break-glass admin account."] if r["auth"] == "enterprise_sso"
                     else ["Account recovery and MFA flows must still be designed."],
                     (0, 0) if not any(k in ch for k in ("Auth0", "WorkOS", "Cognito", "Identity Platform", "Entra External", "Supabase", "Firebase", "Okta"))
                     else ((0, 25) if L <= 2 else (25, 250))))

    # ---------------------------------------------------------------- payments
    if r["payments"]:
        drv = ["requirements.payments"]
        sub = "subscription" in c.signals
        if _india(c):
            drv += ["infrastructure.data_residency"] if c.eff_residency == "india" else []
            desc_l = (c.spec.get("project.description") or "").lower()
            if c.type == "fintech" and ("wallet" in desc_l or "prepaid" in desc_l):
                ch = "RBI-licensed PPI partner (e.g. via a sponsor bank) + Razorpay/Cashfree for top-ups"
                why = ("A stored-value wallet is a Prepaid Payment Instrument under RBI rules: it needs a PPI licence or a licensed "
                       "partner bank; top-ups and withdrawals run over UPI through a payment aggregator.")
                alts = [("Holding balances yourself", "Not allowed without an RBI PPI authorisation.")]
                warns.append({"code": "RBI_PPI_LICENCE", "severity": "warning",
                              "message": "Wallets that hold customer money in India need an RBI Prepaid Payment Instrument licence or a licensed partner.",
                              "fields": ["project.type", "project.description"]})
            elif c.type == "fintech" and re.search(r"disburs|\bemis?\b|\bloans?\b|lending", desc_l):
                ch = "Razorpay (UPI/cards + UPI Autopay/eNACH for EMIs) + RazorpayX Payouts"
                why = ("Indian, RBI-authorised payment aggregator and PCI DSS Level 1: collects EMIs through UPI "
                       "Autopay/eNACH mandates and disburses loans to bank accounts/UPI via RazorpayX payouts. "
                       "Payment data stays in India as RBI's storage directive requires.")
                alts = [("Cashfree Payments (Payouts + Subscriptions)", "Equally capable; Razorpay has wider "
                         "mandate and payout docs. A sensible second gateway for redundancy."),
                        ("Stripe", "Stripe is invite-only for new Indian businesses and does not cover UPI payouts.")]
            elif c.type == "marketplace" or "get paid" in (c.spec.get("project.description") or "").lower():
                ch = "Razorpay + Razorpay Route (split settlements)"
                why = ("UPI, cards and netbanking; Route splits each payment between the platform commission and "
                       "the provider's linked account, which is what a services marketplace needs.")
                alts = [("Cashfree Easy Split", "Similar split-payments product; fewer SDK examples."),
                        ("Stripe Connect", "Invite-only for new Indian businesses.")]
            else:
                ch = "Razorpay" + (" (Subscriptions)" if sub else "")
                why = "UPI-first Indian gateway, PCI DSS Level 1, hosted checkout keeps card data off your servers."
                alts = [("Cashfree Payments", "Comparable; choose on negotiated fees."),
                        ("Stripe", "Invite-only for new Indian businesses.")]
            drv.append("compliance")
        else:
            desc = (c.spec.get("project.description") or "").lower()
            if c.type == "marketplace" or "payout" in desc or "providers get paid" in desc:
                ch = "Stripe Connect (marketplace payments and payouts)"
                why = "Collects from buyers, splits the platform fee and pays out to sellers' connected accounts, with KYC for sellers handled by Stripe."
                alts = [("Adyen for Platforms", "Enterprise onboarding and minimums."), ("PayPal Commerce Platform", "Weaker payout control.")]
            elif "card" in desc and ("issu" in desc or "expense" in desc or "corporate" in desc):
                ch = "Stripe Issuing (virtual/physical cards) + Stripe Treasury where available"
                why = "Issues spend cards with real-time authorisation webhooks and spending controls; card data stays with the issuer (PCI scope minimal)."
                alts = [("Marqeta", "Strong issuing platform, but enterprise contracts."), ("Adyen Issuing", "Enterprise minimums.")]
            elif c.type == "api_platform" or "usage" in desc or "metered" in desc or "per call" in desc:
                ch = "Stripe Billing (usage-based / metered pricing)"
                why = "Meters API calls per customer and invoices on usage tiers; usage events are sent from the API gateway logs."
                alts = [("Orb / Metronome", "Specialist usage billing; extra vendor."), ("Flat plans", "Leaves money on the table for heavy API users.")]
            elif sub or c.type in ("saas_b2b", "realtime_collab"):
                ch = "Stripe Billing (per-seat subscriptions) + Stripe Tax"
                why = "Per-seat plans, proration and invoices out of the box; Stripe Tax handles EU VAT."
                alts = [("Paddle", "Merchant of record removes VAT work, but higher fees and less control."),
                        ("Chargebee", "Billing layer on top of a gateway: one more vendor.")]
                drv.append("project.description")
            else:
                ch = "Stripe (Checkout / Payment Element)"
                why = "Hosted checkout keeps you in PCI DSS SAQ A scope; broad global payment methods."
                alts = [("Adyen", "Enterprise minimums."), ("PayPal Braintree", "Fewer local methods.")]
        out.append(S("payments", ch, why, sorted(set(drv)), alts,
                     ["Gateway fees (~2% per transaction) are not part of the infra estimate.",
                      "Webhook handling must be idempotent."], (0, 0)))

    # ---------------------------------------------------------------- storage
    if r["file_media_storage"]:
        drv = ["requirements.file_media_storage"]
        gb = c.data_gb or 10
        if be == "supabase":
            ch = "Supabase Storage (S3-compatible, same region)"
            alts = [("Amazon S3", "Another vendor; Supabase Storage enforces the same RLS rules.")]
        elif be == "firebase":
            ch = "Cloud Storage for Firebase"
            alts = [("Amazon S3", "Another vendor.")]
        elif onprem:
            ch = "MinIO (self-hosted S3-compatible object storage)"
            alts = [("Ceph RGW", "Much heavier to operate.")]
        elif hp == "aws" or c.cloud == "aws":
            ch = "Amazon S3 (%s) with pre-signed uploads" % kb.PROVIDERS["aws"]["region_label"].get(c.eff_residency if c.eff_residency != "none" else ("eu" if "gdpr" in c.compliance else "none"), "same region as the database")
            alts = [("Cloudflare R2", "No egress fees, but %s." % ("no India jurisdiction option" if c.eff_residency == "india" else "a second vendor next to AWS"))]
        elif hp == "gcp":
            ch = "Google Cloud Storage (%s)" % kb.PROVIDERS["gcp"]["region_label"].get(c.eff_residency if c.eff_residency != "none" else ("eu" if "gdpr" in c.compliance else "none"), "same region as the database")
            alts = [("Amazon S3", "Cross-cloud egress and IAM.")]
        elif hp == "azure":
            ch = "Azure Blob Storage (%s)" % kb.PROVIDERS["azure"]["region_label"].get(c.eff_residency if c.eff_residency != "none" else ("eu" if "gdpr" in c.compliance else "none"), "same region as the database")
            alts = [("SharePoint/OneDrive via Microsoft Graph", "Fine for documents, poor as app storage.")] if any(
                "Graph" in v for k, v in c.hints) else [("Amazon S3", "Cross-cloud egress and identity.")]
        elif hp == "digitalocean":
            ch = "DigitalOcean Spaces (%s)" % kb.PROVIDERS["digitalocean"]["region_label"].get(c.eff_residency, "nearest region")
            alts = [("Amazon S3", "Another vendor and egress fees.")]
        else:
            if c.eff_residency == "india":
                ch = "Amazon S3 (ap-south-1 Mumbai) with pre-signed uploads"
            else:
                ch = "Cloudflare R2 (S3-compatible, no egress fees)"
            alts = [("Amazon S3", "Egress fees for media-heavy apps.")] if "S3" not in ch else [("Cloudflare R2", "No egress fees but no India jurisdiction option.")]
        why = ("Clients upload directly with %s, so large files never pass through the API; objects "
               "are encrypted at rest and private by default." % ("short-lived SAS tokens" if "Azure Blob" in ch else "pre-signed URLs"))
        if "kyc" in (c.spec.get("project.description") or "").lower():
            why += " KYC documents go in a separate bucket with KMS keys, short-lived links and access logging."
            drv.append("project.description")
        if c.eff_residency in ("india", "eu", "us"):
            drv.append("infrastructure.data_residency")
        if "low_bandwidth" in c.signals:
            why += " Mobile clients compress photos before upload and retry with resumable/multipart uploads on weak networks."
        out.append(S("storage", ch, why, drv, alts, ["Needs lifecycle rules to control growth."],
                     (round(gb * 0.02), round(gb * 0.05 + 5))))

    # ---------------------------------------------------------------- cdn
    if "web" in c.platforms and not onprem and not (host["caps"] & {"cdn"}) and c.type != "internal_tool" and (
            r["seo_critical"] or r["file_media_storage"] or c.extras.get("geo_spread") in ("multi_region", "global")):
        ch = {"aws": "Amazon CloudFront", "gcp": "Cloud CDN", "azure": "Azure Front Door (Standard)"}.get(hp, "Cloudflare (free plan)")
        cdrv = [f for f, on in (("requirements.seo_critical", r["seo_critical"]), ("requirements.file_media_storage", r["file_media_storage"]),
                                ("extras.geo_spread", c.extras.get("geo_spread") in ("multi_region", "global"))) if on]
        note = " Private user uploads bypass the CDN and are served via short-lived signed links from the bucket in the residency region." if c.eff_residency in ("india", "eu", "us") and r["file_media_storage"] else ""
        what = "public pages and media" if r["seo_critical"] else ("uploaded media and static assets" if r["file_media_storage"] else "static assets")
        out.append(S("cdn", ch, "Caches %s close to users and absorbs traffic spikes.%s" % (what, note), cdrv,
                     [("No CDN", "Slower first loads for public pages and media.")], [],
                     (0, 0) if ch.startswith("Cloudflare") else (((35, 60) if hp == "azure" else (0, 20)) if L <= 2 else (40, 250))))

    # ---------------------------------------------------------------- realtime
    sep_rt = False
    if r["realtime"]:
        drv = ["requirements.realtime"]
        collab = c.type == "realtime_collab" or any(k in (c.spec.get("project.description") or "").lower()
                                                     for k in ("whiteboard", "cursor", "co-edit", "collaborative edit"))
        if collab:
            sep_rt = True
            ts_team = bool(set(c.langs) & {"javascript", "typescript"}) or not c.langs
            server = "Hocuspocus sync server (TypeScript)" if ts_team else ("pycrdt-websocket (Python) sync server" if "python" in c.langs else "Hocuspocus sync server (TypeScript)")
            ch = "Yjs (CRDT) + %s, Redis pub/sub between nodes" % server
            why = ("Concurrent edits to the same document/board must merge without conflicts: Yjs CRDTs do that, the %s "
                   "syncs updates and awareness (live cursors, presence) over WebSockets, and Redis fans updates out across "
                   "instances for %d concurrent users. Snapshots persist to %s." % (server.split(" (")[0], c.peak, dbs["name"].split(" (")[0]))
            alts = [("Liveblocks", "Managed and fast to start, but per-MAU pricing and US data storage."),
                    ("Last-write-wins over plain WebSockets", "Loses concurrent edits.")]
            drv += ["project.type", "scale.peak_concurrent_users"]
        elif be == "supabase":
            what = []
            desc = (c.spec.get("project.description") or "").lower()
            if "chat" in desc:
                what.append("chat messages")
            if "booking" in c.signals or c.type == "marketplace":
                what.append("booking/job-status changes")
            if "presence" in desc or "online" in desc:
                what.append("presence")
            what = what or ["row changes and broadcast events"]
            ch = "Supabase Realtime (Broadcast + Postgres changes)"
            why = "Included in the Supabase project; pushes %s to subscribed clients over WebSockets." % " and ".join(what)
            alts = [("Socket.IO server", "An extra service to run.")]
        elif be == "firebase":
            ch, why = "Firestore real-time listeners", "Included in Firebase."
            alts = [("Pusher", "Extra vendor.")]
        elif c.type == "gaming" or "multiplayer game" in (c.spec.get("project.description") or "").lower() or "battle" in (c.spec.get("project.description") or "").lower():
            sep_rt = True
            if "csharp" in c.langs:
                ch = "Dedicated game server with Nakama (self-hosted) or Unity Netcode + Unity Relay"
            elif set(c.langs) & {"javascript", "typescript"}:
                ch = "Colyseus (authoritative Node.js game server, rooms + state sync) + Redis presence"
            else:
                ch = "Nakama (open-source game server: matchmaking, realtime, leaderboards)"
            why = ("Matches need an authoritative server that owns game state, matchmaking and per-room broadcast at low "
                   "latency for %d concurrent players; leaderboards live in the database/Redis sorted sets." % c.peak)
            alts = [("Plain WebSockets in the API", "You would rebuild rooms, matchmaking and reconnection logic."),
                    ("Photon / PlayFab", "Managed and proven, but per-CCU pricing and vendor lock-in.")]
            drv += ["project.type", "scale.peak_concurrent_users"]
        elif False:
            sep_rt = True
            ch = "Yjs (CRDT) + Hocuspocus sync server, Redis pub/sub between nodes"
            why = ("Concurrent edits to one board must merge without conflicts: Yjs CRDTs do that, Hocuspocus "
                   "(TypeScript) syncs documents and awareness (live cursors) over WebSockets, and Redis fans "
                   "updates out across instances for %d concurrent users. Board snapshots persist to the database."
                   % c.peak)
            alts = [("Liveblocks", "Managed and fast to start, but per-MAU pricing and US data storage."),
                    ("Custom operational-transform server in Go", "Months of work to get conflict handling right.")]
            drv += ["project.type", "scale.peak_concurrent_users"]
        elif not (host["caps"] & {"websockets"}):
            ch = "Pusher Channels (%s cluster)" % ("ap2 Mumbai" if c.eff_residency == "india" else "nearest")
            why = "The host cannot keep WebSockets open, so a hosted pub/sub service delivers live updates."
            alts = [("Ably", "Comparable; region guarantees need an enterprise plan.")]
        else:
            ch = {"aspnet": "SignalR" + (" (Azure SignalR Service)" if hp == "azure" else ""),
                  "phoenix": "Phoenix Channels + Presence", "django": "Django Channels + Redis channel layer",
                  "nestjs": "NestJS WebSocket gateway (Socket.IO) + Redis adapter",
                  "fastify": "Socket.IO + Redis adapter", "spring_boot": "Spring WebSocket (STOMP)",
                  "rails": "Action Cable", "laravel": "Laravel Reverb",
                  "go_chi": "WebSockets in Go (coder/websocket) + Redis pub/sub"}.get(be, "WebSockets")
            why = "Runs inside the existing backend; a Redis adapter lets several instances share connections."
            alts = [("Pusher/Ably", "Per-connection pricing for something the backend can do itself.")]
        out.append(S("realtime", ch, why, drv, alts, ["Long-lived connections must be drained on deploys; size Redis pub/sub for peak fan-out."] if not (be in ("supabase", "firebase")) else ["Channel limits depend on the Supabase/Firebase plan."],
                     (0, 0) if be in ("supabase", "firebase") else ((0, 30) if L <= 2 else (50, 400)),
                     separate_service=sep_rt))

    # ---------------------------------------------------------------- cache
    need_cache = (L >= 3 or (r["p95_latency_ms"] and r["p95_latency_ms"] <= 150) or c.rps >= 500
                  or (r["realtime"] and be not in ("supabase", "firebase")))
    existing_redis = next((x for x in c.existing if "redis" in x.lower() or "valkey" in x.lower()), None)
    if need_cache and be not in ("supabase", "firebase", "aws_lambda", "next_fullstack", "azure_functions"):
        if existing_redis:
            ch = "Existing %s (reused)" % existing_redis
        elif onprem or c.deploy == "self_hosted_cloud_vm" or host["id"] == "vm_compose":
            ch = "Valkey (Redis-compatible, container)"
        else:
            ch = {"aws": "Amazon ElastiCache for Valkey", "gcp": "Memorystore for Valkey",
                  "azure": "Azure Managed Redis", "digitalocean": "DigitalOcean Managed Caching (Valkey)",
                  "render": "Render Key Value (Valkey)", "railway": "Redis on Railway"}.get(hp, "Valkey (managed)")
        why = ("Caches hot reads and sessions%s" % (" to hold p95=%dms" % r["p95_latency_ms"] if r["p95_latency_ms"] else " to keep latency low at peak")
               + (", and carries pub/sub for real-time fan-out." if r["realtime"] else "."))
        if any(x["layer"] == "queue" and ("Redis" in x["choice"] or "Valkey" in x["choice"] or "BullMQ" in x["choice"] or "Celery" in x["choice"]) for x in out):
            why += " It is also the job-queue broker."
        out.append(S("cache", ch, why,
                     [x for x, on in (("requirements.p95_latency_ms", bool(r["p95_latency_ms"])), ("scale.peak_concurrent_users", L >= 3),
                                      ("requirements.realtime", r["realtime"])) if on] or ["scale.users_12_months"],
                     [("Memcached", "No pub/sub or persistence, so it cannot double as a broker."),
                      ("No cache", "Every read hits the database at peak.")],
                     ["Another stateful component to size and monitor."],
                     (0, 0) if "container" in ch else ((15, 50) if L <= 2 else (50, 400))))

    # ---------------------------------------------------------------- queue
    need_q = (r["payments"] or r["ml_features"] in ("inference_api", "training") or L >= 3
              or (r["file_media_storage"] and L >= 2) or "workflow" in c.signals or "notifications" in c.signals)
    if need_q and be not in ("none",):
        ex_q = next((x for x in c.existing if any(k in x.lower() for k in ("kafka", "ibm mq", "rabbitmq", "activemq", "amazon mq"))), None)
        if ex_q:
            ch = "%s (existing, reused)" % ex_q
        elif be == "supabase":
            ch = "Supabase Queues (pgmq) + Edge Functions"
        elif be == "firebase":
            ch = "Cloud Tasks"
        elif be == "aws_lambda" or (hp == "aws" and be in ("spring_boot", "go_chi")):
            ch = "Amazon SQS (+ dead-letter queue)"
        elif onprem:
            ch = "RabbitMQ (self-hosted)"
        elif be == "aspnet" and hp == "azure":
            ch = "Azure Service Bus"
        else:
            ch = {"django": "Celery (Valkey/Redis broker)", "fastapi": "Celery (Valkey/Redis broker)",
                  "nestjs": "BullMQ (on Valkey/Redis)", "fastify": "BullMQ (on Valkey/Redis)",
                  "laravel": "Laravel Queues (Redis)", "rails": "Solid Queue (database-backed)",
                  "phoenix": "Oban (Postgres-backed jobs)", "spring_boot": "RabbitMQ", "go_chi": "NATS JetStream",
                  "next_fullstack": "Inngest / QStash (serverless jobs)", "aspnet": "Hangfire (database-backed)",
                  "azure_functions": "Azure Queue Storage"}.get(be, "RabbitMQ")
        jobs = [x for x, on in (("payment webhooks", r["payments"]), ("model scoring", r["ml_features"] in ("inference_api", "training")),
                                ("image/file processing", r["file_media_storage"]), ("approval e-mails", "workflow" in c.signals),
                                ("notifications", "notifications" in c.signals or "booking" in c.signals)) if on] or ["background jobs"]
        why = "Moves slow or retry-prone work (%s) off the request path so p95 stays low and failures retry safely." % ", ".join(jobs)
        out.append(S("queue", ch, why,
                     [x for x, on in (("requirements.payments", r["payments"]), ("requirements.ml_features", r["ml_features"] != "none"),
                                      ("requirements.file_media_storage", r["file_media_storage"]), ("scale.users_12_months", L >= 3)) if on] or ["project.description"],
                     [("Kafka", "Event-streaming platform: heavy to run for job queues at this scale.") if "existing" not in ch else
                      ("A new queue service", "The organisation already runs one; reuse it.")],
                     ["Jobs must be idempotent; monitor the dead-letter queue."],
                     (0, 10) if L <= 2 else (10, 150)))
        if ("Redis" in ch or "Valkey" in ch) and not any(s["layer"] == "cache" for s in out) and not onprem:
            vch = {"aws": "Amazon ElastiCache for Valkey", "gcp": "Memorystore for Valkey", "azure": "Azure Managed Redis",
                   "digitalocean": "DigitalOcean Managed Caching (Valkey)", "render": "Render Key Value (Valkey)",
                   "railway": "Redis on Railway"}.get(hp, "Valkey")
            if host["id"] == "vm_compose" or c.deploy == "self_hosted_cloud_vm":
                vch = "Valkey (Redis-compatible, container)"
            out.append(S("cache", vch, "Job broker for %s; also available for caching." % ch.split(" (")[0],
                         ["requirements.payments"] if r["payments"] else ["scale.users_12_months"],
                         [("Database-backed queue", "Fewer components but more DB load.")], [],
                         (0, 0) if "container" in vch else (15, 40)))

    # ---------------------------------------------------------------- search
    if r["full_text_search"]:
        drv = ["requirements.full_text_search"]
        indic = bool(re.search(r"\b(hindi|telugu|tamil|kannada|marathi|bengali|gujarati|indic|regional languages?)\b", (c.spec.get("project.description") or "").lower()))
        if indic:
            zone = c.eff_residency if c.eff_residency in ("india", "eu", "us") else "india"
            if not onprem and (hp == "aws" or c.cloud in ("any", "aws")):
                ch = "Amazon OpenSearch Service (%s) with ICU + Indic analysers" % kb.PROVIDERS["aws"]["region_label"][zone]
            else:
                ch = "OpenSearch (self-hosted, same region as the database) with ICU + Indic analysers"
            why = ("Hindi/Telugu and other Indic scripts need Unicode-aware tokenisation and transliteration-friendly matching; "
                   "PostgreSQL has no Indic dictionaries, so a Lucene-based engine with ICU analysis is used.")
            alts = [("PostgreSQL full-text search", "No stemming/dictionaries for Indic languages."),
                    ("Meilisearch", "Simpler, but weaker control over Indic analysis.")]
            cost = (50, 400)
            drv.append("project.description")
        elif db == "mongodb":
            ch, why = "MongoDB Atlas Search", "Lucene-based search inside Atlas; no extra service."
            alts = [("Meilisearch", "Extra service to sync.")]
            cost = (0, 0)
        elif db in ("postgres", "timescale") and (L <= 3 or not c.devops) and (c.data_gb or 0) <= 2000:
            ch = "PostgreSQL full-text search (tsvector + GIN, pg_trgm for typos)"
            why = ("At %d users and ~%s GB the database's own search is fast enough, transactional and needs no sync "
                   "pipeline." % (c.users, "%g" % c.data_gb))
            alts = [("Meilisearch", "Better typo tolerance and ranking, but another service to keep in sync; "
                                    "move to it if search becomes core."),
                    ("OpenSearch", "Over-sized at this scale.")]
            cost = (0, 0)
            drv.append("scale.users_12_months")
        elif hp == "aws" and not onprem:
            ch, why = "Amazon OpenSearch Service", "Managed search in the same region at large scale."
            alts = [("Meilisearch", "Simpler, but self-managed at this size.")]
            cost = (100, 800)
        elif c.devops or onprem:
            ch, why = "Meilisearch (self-hosted)", "Typo-tolerant search engine, easy to run beside the app."
            alts = [("OpenSearch", "Heavier JVM cluster.")]
            cost = (0, 50)
        else:
            ch, why = "Typesense Cloud / Meilisearch Cloud (managed)", "Managed typo-tolerant search, since no one on the team operates infrastructure."
            alts = [("Self-hosted Meilisearch", "Needs someone to run and back it up.")]
            cost = (30, 150)
        out.append(S("search", ch, why, drv, alts, [], cost))

    # ---------------------------------------------------------------- ML
    if r["ml_features"] != "none":
        drv = ["requirements.ml_features"]
        sep = False
        if r["ml_features"] == "inference_api":
            if be in ("django", "fastapi"):
                ch = "In-process model (scikit-learn/XGBoost, exported to ONNX) inside the Python backend"
                cost = (0, 0)
            elif hp == "aws" and c.devops and c.budget >= 2000:
                ch = "Amazon SageMaker real-time endpoint (%s) for the scoring model" % kb.PROVIDERS["aws"]["region_label"].get(c.eff_residency, "same region")
                sep, cost = True, (100, 600)
            elif "python" in c.langs or not c.langs:
                ch = "Python FastAPI scoring service (XGBoost/LightGBM) beside the main backend"
                sep, cost = True, (10, 100)
            else:
                ch = "Model trained offline, served in-process via ONNX Runtime from the %s backend" % combo["backend"]["name"].split(" (")[0]
                sep, cost = False, (0, 0)
            dsc = (c.spec.get("project.description") or "").lower()
            textgen = any(k in dsc for k in ("summar", "chatbot", "assistant", "generate", "draft", "translat", "llm", "gpt"))
            credit = any(k in dsc for k in ("credit", "loan", "lending", "fraud", "risk scor"))
            speech = any(k in dsc for k in ("speech", "pronunciation", "voice", "audio scor"))
            vision = any(k in dsc for k in ("webcam", "camera", "image", "vision", "proctor", "face", "photo classif"))
            if speech and not textgen:
                ch = {"azure": "Azure AI Speech (pronunciation assessment)", "aws": "Amazon Transcribe + custom scoring",
                      "gcp": "Google Cloud Speech-to-Text + custom scoring"}.get(hp, "Azure AI Speech pronunciation assessment or self-hosted Whisper")
                sep, cost = False, (20, 300)
                why = "Speech scoring is an audio-model task: a managed speech API returns transcripts and pronunciation scores per phoneme."
                alts = [("Training your own acoustic model", "Needs large labelled audio datasets.")]
            elif vision and not textgen:
                ch = "On-device detection (MediaPipe/ML Kit) + server-side review queue"
                sep, cost = False, (0, 100)
                why = ("Camera/vision checks run on the client (face presence, multiple faces, gaze) so raw video is not uploaded; "
                       "flagged events go to a review queue with short clips for human decision.")
                alts = [("Streaming all video to the server for analysis", "Bandwidth, privacy and GPU cost.")]
            elif textgen:
                llm = {"aws": "Amazon Bedrock (in-region)", "azure": "Azure OpenAI (in-region deployment)",
                       "gcp": "Vertex AI (Gemini, in-region)"}.get(hp, "a hosted LLM API (e.g. Claude or GPT) with zero-retention terms")
                ch = "Hosted LLM via %s, called from the backend" % llm
                sep, cost = False, (20, 300)
                why = ("Text generation (summaries, drafts, chat) is a foundation-model task; calling a hosted model from the "
                       "backend avoids training or serving your own, and prompts/outputs are logged for review.")
                alts = [("Self-hosted open-weight model", "Needs GPUs and MLOps for a small team."),
                        ("Training a custom model", "No labelled data or need at this stage.")]
            else:
                why = ("Serves a model you train on your own data behind an internal API; versioned models and logged "
                       "inputs/outputs make every prediction reproducible and auditable.")
                if credit:
                    why += " Credit/risk decisions need explainability (feature attributions) and human review of rejections."
                    alts = [("Third-party scoring API", "Risk models are a core asset and contain personal data; keep them in your region."),
                            ("LLM-based scoring", "Non-deterministic and hard to explain for credit decisions.")]
                else:
                    alts = [("Managed AutoML endpoint", "Faster start, but higher per-prediction cost and less control."),
                            ("Rules only", "Cannot learn from the data as it grows.")]
        elif r["ml_features"] == "llm_rag":
            vec = "pgvector" if db in ("postgres",) else ("Atlas Vector Search" if db == "mongodb" else "pgvector (add Postgres)")
            if onprem:
                llm = "self-hosted open-weight model on vLLM (local GPUs)"
            elif hp == "aws" or c.cloud == "aws":
                llm = "Amazon Bedrock (in-region)"
            elif hp == "azure" or c.cloud == "azure":
                llm = "Azure OpenAI (in-region deployment)"
            elif hp == "gcp" or c.cloud == "gcp":
                llm = "Vertex AI (Gemini, in-region)"
            else:
                llm = "a hosted LLM API (e.g. Claude or GPT) with zero-retention terms"
            ch = "RAG: %s for embeddings + %s" % (vec, llm)
            why = "Retrieval over your own documents grounds answers; keeping the model in-region respects residency."
            alts = [("Dedicated vector DB (Qdrant/Pinecone)", "Another store to sync; pgvector is enough below ~10M vectors.")]
            cost = (20, 300) if not onprem else (0, 0)
        else:
            if onprem:
                ch, cost = "MLflow + on-prem GPU nodes", (0, 0)
            else:
                ch = {"aws": "Amazon SageMaker (training jobs + model registry)", "gcp": "Vertex AI Training",
                      "azure": "Azure Machine Learning"}.get(hp, "Modal / Lambda Cloud GPU jobs + MLflow")
                cost = (50, 1000)
            why = "Managed training jobs with experiment tracking and a model registry."
            alts = [("Notebook on a GPU VM", "Not reproducible or auditable.")]
            sep = True
        out.append(S("ml", ch, why, drv, alts, ["Models need monitoring for drift."], cost, separate_service=sep))

    # ---------------------------------------------------------------- other: offline sync, notifications, integrations
    mobile = combo.get("mobile")
    if r["offline_support"] and not mobile and combo.get("desktop"):
        out.append(S("other", "Offline-first desktop: local SQLite + outbox sync", "Each sale/record is written to a local SQLite file first "
                     "and synced in the background with idempotent IDs when the network returns, so the counter keeps working offline.",
                     ["requirements.offline_support", "project.platforms"],
                     [("Online-only client", "Stops trading whenever the connection drops.")], [], (0, 0)))
    if r["offline_support"] and mobile:
        lib = {"rn_expo": "expo-sqlite (or WatermelonDB)", "kotlin_android": "Room + WorkManager",
               "flutter": "drift (SQLite)", "kmp": "SQLDelight", "swift_ios": "SwiftData",
               "capacitor": "@capacitor-community/sqlite", "pwa": "IndexedDB + Background Sync"}.get(mobile["id"], "SQLite")
        why = ("Every record/form is saved to %s on the device first and placed in an outbox; a background job "
               "syncs when signal returns using idempotent upload IDs, so retries never duplicate records."
               % lib.split(" (")[0])
        if "geo" in c.signals:
            why += " GPS coordinates and capture time are stored with each record at capture time."
        out.append(S("other", "Offline-first sync: %s + outbox queue" % lib, why,
                     ["requirements.offline_support", "project.description"],
                     [("PowerSync / ElectricSQL", "Managed Postgres-to-SQLite sync; worth it if two-way sync of "
                       "shared data grows, but adds a vendor/service."),
                      ("Firestore offline cache", "Firebase Auth data lives in the US; residency rules it out." if c.eff_residency == "india" else "Proprietary lock-in.")],
                     ["Conflict rules must be explicit (field agents rarely edit the same record)."], (0, 0)))
    if mobile and not airgap:
        out.append(S("other", "Firebase Cloud Messaging (push notifications)",
                     "Free push delivery to Android%s for %s.%s" % (" and iOS (via APNs)" if "ios" in c.platforms else "", _push_use(c),
                     " Android push always goes through FCM; payloads carry only an ID and a short text, never personal data, so residency is unaffected." if c.eff_residency in ("india", "eu", "us") or c.deploy != "managed_cloud" else ""),
                     ["project.platforms"], [("SMS for every notification", "Costs per message and DLT templates.")],
                     ["Notification payloads must not contain sensitive data%s." % (" (no PHI: FCM is not covered by a HIPAA BAA)" if "hipaa" in c.compliance else "")], (0, 0)))
    integ = [v for k, v in c.hints if k == "integration"]
    if any("SuccessFactors" in v for v in integ):
        integ = [v for v in integ if not v.startswith("SAP (")]
    for val in integ:
        if True:
            out.append(S("other", val, "Listed in existing_systems; integrate through its official API with a "
                         "service account and least-privilege scopes rather than duplicating its data.",
                         ["infrastructure.existing_systems"], [("Manual CSV exports", "Error-prone and stale.")], [], (0, 0)))
    dom = _domain_items(c, combo, dbs, hp, onprem, out)
    if any(x.get("replaces_online") for x in dom):
        out = [y for y in out if y["layer"] != "payments"]
    for x in dom:
        if x["layer"] == "cache" and "sorted sets" in x["choice"]:
            for y in out:
                if y["layer"] == "cache":
                    y["choice"] += " (also leaderboard sorted sets)"
                    y["justification"] += " Leaderboards use Valkey sorted sets (ZADD/ZREVRANGE: rank and top-N in O(log n)); scores persist to the database."
                    y["driven_by"] = sorted(set(y["driven_by"]) | {"project.description"})
                    break
            else:
                out.append(x)
        else:
            out.append(x)
    out += _existing_plan(c, combo, dbs, hp)
    regulated = set(c.compliance) & {"pci_dss", "hipaa", "iso27001", "soc2", "rbi_localisation"}
    if regulated and not onprem:
        ch = {"aws": "AWS KMS + Secrets Manager + CloudTrail + AWS WAF", "gcp": "Cloud KMS + Secret Manager + Cloud Audit Logs + Cloud Armor",
              "azure": "Azure Key Vault + Activity Log + Azure WAF"}.get(hp, "Supabase Vault (secrets) + Postgres audit logging (pgAudit) + Cloudflare WAF" if be == "supabase" else "Doppler (secrets) + provider audit logs + Cloudflare WAF")
        out.append(S("other", ch, "Compliance controls for %s: encryption keys you control, no secrets in code, "
                     "tamper-evident audit logs and a web application firewall." % ", ".join(sorted(x.upper() for x in regulated)),
                     ["compliance"], [("Application-level only", "Auditors expect platform-level key management and logs.")],
                     ["Costs grow with log volume."], (20, 200) if L <= 3 else (200, 1500)))

    # ---------------------------------------------------------------- CI/CD + observability
    if airgap:
        ci = ("GitLab CE (self-managed) CI/CD", "Code, pipelines and the container registry all live inside the air-gapped network.")
    elif onprem:
        ci = ("GitLab CE (self-managed) or GitHub Actions with self-hosted runners", "Builds run on runners inside your network.")
    else:
        ci = ("GitHub Actions", "Runs tests and deploys on every merge; free minutes cover a small team.")
    if combo.get("mobile") and combo["mobile"]["id"] == "rn_expo" and not onprem:
        ci = (ci[0] + " + Expo EAS (Build, Submit, Update)", ci[1] + " EAS builds signed Android/iOS binaries in the cloud, submits them to the stores and ships JavaScript-only fixes over the air.")
    if "geo" in c.signals and db in ("postgres",) and not any("PostGIS" in x["choice"] for x in out):
        out.append(S("other", "PostGIS extension (spatial types and indexes)",
                     "The description mentions %s data: store points as geography(Point), index them with GiST and query by "
                     "distance/area in SQL, inside the same Postgres database." % c.signals["geo"],
                     ["project.description"], [("Lat/long as plain numbers", "No spatial indexes; distance queries scan every row.")], [], (0, 0)))
    out.append(S("ci_cd", ci[0], ci[1], ["infrastructure.deployment_model", "team.size"],
                 [("Jenkins", "Self-hosted server to maintain.")], [], (0, 0) if L <= 2 else (0, 50)))
    if onprem:
        ob = ("Prometheus + Grafana + Loki (self-hosted)", "Metrics, dashboards and logs without any external SaaS.", (0, 0))
    else:
        native = {"aws": "Amazon CloudWatch", "gcp": "Google Cloud Monitoring/Logging",
                  "azure": "Azure Monitor + Application Insights"}.get(hp)
        sentry_ok = _saas_ok("sentry", c)
        if native and (c.eff_residency in ("india",) or regulated or L >= 3):
            ob = ("%s + OpenTelemetry tracing" % native,
                  "Logs, metrics and traces stay in the same cloud and region as the data%s." % (
                      "; Sentry has no India data region" if c.eff_residency == "india" else ""), (10, 50) if L <= 2 else (50, 500))
        elif sentry_ok:
            ob = ("Sentry (errors + performance) + host logs",
                  "Free Developer plan catches errors in the backend%s with stack traces." % (
                      " and mobile apps" if ("android" in c.platforms or "ios" in c.platforms) else (" and web front end" if "web" in c.platforms else "")), (0, 26))
        else:
            ob = ("Grafana Cloud (free tier) + OpenTelemetry, metrics and PII-scrubbed logs only",
                  "Free tier for metrics and logs. Because data residency applies, only metrics and logs scrubbed of personal data leave the region; request payloads are never exported.", (0, 50))
    if r["availability"] in ("99.95", "99.99"):
        ob = (ob[0] + " + uptime checks and on-call paging", ob[1] + " %s%% needs alerting that wakes someone up." % r["availability"], ob[2])
    out.append(S("observability", ob[0], ob[1], [f for f, on in (("requirements.availability", True),
                 ("infrastructure.data_residency", c.eff_residency != "none"), ("compliance", bool(regulated))) if on],
                 [("Datadog", "Strong product but expensive per host at this budget.")], [], ob[2]))
    return out, warns
