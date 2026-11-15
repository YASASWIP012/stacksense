"""Supporting services: auth, payments, storage, CDN, cache, queue, search,
realtime, ML, CI/CD, observability and other integrations.

Each layer is added only when an input calls for it, and each choice respects
the same hard constraints as the core layers (deployment model, cloud
preference, data residency, compliance).
"""
from . import kb

# SaaS region facts for supporting vendors (conservative; see kb sources)
SAAS = {
    "auth0": {"india": False, "eu": True, "us": True, "src": "https://auth0.com/docs/deploy-monitor/deploy-public-cloud/public-cloud-regions"},
    "pusher": {"india": True, "eu": True, "us": True, "src": "https://pusher.com/docs/channels/miscellaneous/clusters/"},
    "sentry": {"india": False, "eu": True, "us": True, "src": "https://docs.sentry.io/organization/data-storage-location/"},
    "msg91": {"india": True, "eu": False, "us": False, "src": "https://msg91.com/help/what-is-dlt"},
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


def choose_supporting(c, combo, dbs, hp):
    out, warns = [], []
    be = combo["backend"]["id"]
    db = combo["database"]["id"]
    host = combo["hosting"]
    r = c.req
    onprem = c.deploy in ("on_premise", "air_gapped")
    airgap = c.deploy == "air_gapped"
    if be == "none":
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
        if be == "supabase":
            ch, why = "Supabase Auth", "Included in the Supabase project; issues JWTs that Postgres RLS policies check."
            alts = [("Clerk", "Separate vendor; user data stored in the US.")]
        elif be == "firebase":
            ch, why = "Firebase Authentication", "Included with Firebase; drop-in UI for email, phone and social sign-in."
            alts = [("Auth0", "Extra vendor for no gain here.")]
        elif r["auth"] == "enterprise_sso":
            drv.append("infrastructure.existing_systems")
            if "entra" in idp or c.cloud == "azure":
                ch = "Microsoft Entra ID (OIDC/SAML single sign-on)"
                why = ("Employees already sign in with Entra ID; the app trusts it via OpenID Connect, so there are "
                       "no new passwords and joiners/leavers are handled centrally.")
                if host["id"] == "azure_app_service":
                    why += " App Service 'Easy Auth' can enforce sign-in with no auth code."
                alts = [("Keycloak", "Would duplicate the identity provider the company already runs.")]
            elif "okta" in idp:
                ch, why = "Okta (existing IdP via OIDC)", "Reuses the company's Okta tenant."
                alts = [("Keycloak", "Duplicates the existing IdP.")]
            elif onprem or c.devops:
                ch, why = "Keycloak (self-hosted)", "Open-source IdP with SAML/OIDC brokering to any corporate directory; runs on your servers."
                alts = [("Auth0 / Okta", "SaaS: not available %s." % ("air-gapped" if airgap else "self-hosted"))]
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
            if c.devops:
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
                   "go_chi": "Ory Kratos (self-hosted identity server)", "laravel": "Laravel Fortify + Socialite",
                   "rails": "Devise + OmniAuth", "phoenix": "phx.gen.auth + Ueberauth",
                   "next_fullstack": "Auth.js (NextAuth)", "azure_functions": "Microsoft Entra External ID"}.get(be, "Framework built-in auth")
            ch = lib
            why = "Uses the backend framework's maintained auth library, so user data stays in your own database%s." % (
                " in %s" % c.eff_residency.upper() if c.eff_residency in ("eu", "us") else (" in India" if c.eff_residency == "india" else ""))
            alts = [("Auth0 / Clerk", "Separate vendor with per-MAU pricing%s." % (
                "; no India region" if c.eff_residency == "india" else ""))]
        if r["auth"] == "phone_otp" and not airgap:
            sms = "MSG91 (DLT-registered SMS OTP)" if _india(c) else "Twilio Verify"
            ch = "%s + %s" % (ch, sms)
            why += (" Phone OTP is sent through %s; in India every SMS template and sender ID must be registered on "
                    "the TRAI DLT platform first." % sms.split(" (")[0]) if _india(c) else " OTPs are sent through Twilio Verify."
            drv.append("requirements.auth")
            alts.append(("Firebase phone auth", "Firebase Authentication processes data in the US."))
        out.append(S("auth", ch, why, sorted(set(drv)), alts,
                     ["Account recovery and MFA flows must still be designed."],
                     (0, 25) if L <= 2 else (25, 250)))

    # ---------------------------------------------------------------- payments
    if r["payments"]:
        drv = ["requirements.payments"]
        sub = "subscription" in c.signals
        if _india(c):
            drv += ["infrastructure.data_residency"] if c.eff_residency == "india" else []
            if c.type == "fintech" or "disburs" in (c.spec.get("project.description") or "").lower():
                ch = "Razorpay (UPI/cards + UPI Autopay/eNACH for EMIs) + RazorpayX Payouts"
                why = ("Indian, RBI-authorised payment aggregator and PCI DSS Level 1: collects EMIs through UPI "
                       "Autopay/eNACH mandates and disburses loans to bank accounts/UPI via RazorpayX payouts. "
                       "Payment data stays in India as RBI's storage directive requires.")
                alts = [("Cashfree Payments (Payouts + Subscriptions)", "Equally capable; Razorpay has wider "
                         "mandate and payout docs. A sensible second gateway for redundancy."),
                        ("Stripe", "Stripe is invite-only for new Indian businesses and does not cover UPI payouts.")]
            elif c.type == "marketplace":
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
            if sub or c.type in ("saas_b2b", "realtime_collab"):
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
            ch = "Amazon S3 (%s) with pre-signed uploads" % kb.PROVIDERS["aws"]["region_label"].get(c.eff_residency, "nearest region")
            alts = [("Cloudflare R2", "No egress fees, but no India jurisdiction option.")]
        elif hp == "gcp":
            ch = "Google Cloud Storage (%s)" % kb.PROVIDERS["gcp"]["region_label"].get(c.eff_residency, "nearest region")
            alts = [("Amazon S3", "Cross-cloud egress and IAM.")]
        elif hp == "azure":
            ch = "Azure Blob Storage (%s)" % kb.PROVIDERS["azure"]["region_label"].get(c.eff_residency, "nearest region")
            alts = [("SharePoint/OneDrive via Microsoft Graph", "Fine for documents, poor as app storage.")]
        elif hp == "digitalocean":
            ch = "DigitalOcean Spaces (%s)" % kb.PROVIDERS["digitalocean"]["region_label"].get(c.eff_residency, "nearest region")
            alts = [("Amazon S3", "Another vendor and egress fees.")]
        else:
            if c.eff_residency == "india":
                ch = "Amazon S3 (ap-south-1 Mumbai) with pre-signed uploads"
            else:
                ch = "Cloudflare R2 (S3-compatible, no egress fees)"
            alts = [("Amazon S3", "Egress fees for media-heavy apps.")]
        why = ("Clients upload directly with pre-signed URLs, so large files never pass through the API; objects "
               "are encrypted at rest and private by default.")
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
    if "web" in c.platforms and not onprem and not (host["caps"] & {"cdn"}) and (
            r["seo_critical"] or r["file_media_storage"] or c.extras.get("geo_spread") in ("multi_region", "global")):
        ch = {"aws": "Amazon CloudFront", "gcp": "Cloud CDN", "azure": "Azure Front Door"}.get(hp, "Cloudflare (free plan)")
        out.append(S("cdn", ch, "Caches static assets and public media close to users and absorbs traffic spikes.",
                     ["requirements.seo_critical", "requirements.file_media_storage"],
                     [("No CDN", "Slower first loads for public pages and media.")], [], (0, 20) if L <= 2 else (20, 200)))

    # ---------------------------------------------------------------- realtime
    sep_rt = False
    if r["realtime"]:
        drv = ["requirements.realtime"]
        collab = c.type == "realtime_collab" or any(k in (c.spec.get("project.description") or "").lower()
                                                     for k in ("whiteboard", "cursor", "multiplayer", "co-edit"))
        if be == "supabase":
            ch, why = "Supabase Realtime (Broadcast + Postgres changes)", "Included; pushes chat messages and job-status changes to subscribed clients."
            alts = [("Socket.IO server", "An extra service to run.")]
        elif be == "firebase":
            ch, why = "Firestore real-time listeners", "Included in Firebase."
            alts = [("Pusher", "Extra vendor.")]
        elif collab:
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
        out.append(S("realtime", ch, why, drv, alts, ["Long-lived connections need sticky sessions or a pub/sub layer."],
                     (0, 0) if be in ("supabase", "firebase") else ((0, 30) if L <= 2 else (50, 400)),
                     separate_service=sep_rt))

    # ---------------------------------------------------------------- cache
    need_cache = (L >= 3 or (r["p95_latency_ms"] and r["p95_latency_ms"] <= 150) or c.rps >= 500
                  or (r["realtime"] and be not in ("supabase", "firebase")))
    if need_cache and be not in ("supabase", "firebase", "aws_lambda", "next_fullstack", "azure_functions"):
        if onprem or c.deploy == "self_hosted_cloud_vm" or host["id"] == "vm_compose":
            ch = "Valkey (Redis-compatible, container)"
        else:
            ch = {"aws": "Amazon ElastiCache for Valkey", "gcp": "Memorystore for Valkey",
                  "azure": "Azure Managed Redis", "digitalocean": "DigitalOcean Managed Caching (Valkey)",
                  "render": "Render Key Value (Valkey)", "railway": "Redis on Railway"}.get(hp, "Valkey (managed)")
        why = ("Caches hot reads and sessions to hold p95=%sms, and doubles as the pub/sub and job-queue broker."
               % (r["p95_latency_ms"] or "low"))
        out.append(S("cache", ch, why,
                     [x for x in ("requirements.p95_latency_ms", "scale.peak_concurrent_users", "requirements.realtime")
                      if (x != "requirements.p95_latency_ms" or r["p95_latency_ms"])],
                     [("Memcached", "No pub/sub or persistence, so it cannot double as a broker."),
                      ("No cache", "Every read hits the database at peak.")],
                     ["Another stateful component to size and monitor."],
                     (0, 0) if "container" in ch else ((15, 50) if L <= 2 else (50, 400))))

    # ---------------------------------------------------------------- queue
    need_q = (r["payments"] or r["ml_features"] in ("inference_api", "training") or L >= 3
              or (r["file_media_storage"] and L >= 2) or "workflow" in c.signals or "notifications" in c.signals)
    if need_q and be not in ("none",):
        if any(v == "kafka" for k, v in c.hints if k == "queue"):
            ch = "Apache Kafka (existing cluster)"
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
        why = "Moves slow or retry-prone work (payment webhooks, notifications, image processing, scoring) off the request path so p95 stays low and failures retry safely."
        out.append(S("queue", ch, why,
                     [x for x, on in (("requirements.payments", r["payments"]), ("requirements.ml_features", r["ml_features"] != "none"),
                                      ("requirements.file_media_storage", r["file_media_storage"]), ("scale.users_12_months", L >= 3)) if on] or ["project.description"],
                     [("Kafka", "Event-streaming platform: heavy to run for job queues at this scale.") if "Kafka" not in ch else
                      ("SQS/RabbitMQ", "Kafka already exists; reuse it.")],
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
        if db == "mongodb":
            ch, why = "MongoDB Atlas Search", "Lucene-based search inside Atlas; no extra service."
            alts = [("Meilisearch", "Extra service to sync.")]
            cost = (0, 0)
        elif db in ("postgres", "timescale") and L <= 3 and (c.data_gb or 0) <= 500:
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
        else:
            ch, why = "Meilisearch (self-hosted)", "Typo-tolerant search engine, easy to run beside the app."
            alts = [("OpenSearch", "Heavier JVM cluster.")]
            cost = (0, 50)
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
            else:
                ch = "Python FastAPI scoring service (XGBoost/LightGBM) beside the main backend"
                sep, cost = True, (10, 100)
            why = ("Serves a model you train on your own data behind an internal API; versioned models and logged "
                   "inputs/outputs make every decision explainable and auditable.")
            if c.type == "fintech":
                why += " Credit decisions need explainability (feature attributions) and human review of rejections."
            alts = [("Third-party scoring API", "Credit models are a core asset and contain personal data; keep them in your region."),
                    ("LLM-based scoring", "Non-deterministic and hard to explain for credit decisions.")]
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
    if r["offline_support"] and mobile:
        lib = {"rn_expo": "expo-sqlite (or WatermelonDB)", "kotlin_android": "Room + WorkManager",
               "flutter": "drift (SQLite)", "kmp": "SQLDelight", "swift_ios": "SwiftData",
               "capacitor": "@capacitor-community/sqlite", "pwa": "IndexedDB + Background Sync"}.get(mobile["id"], "SQLite")
        why = ("Every survey/form is saved to %s on the device first and placed in an outbox; a background job "
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
                     "Free push delivery to Android%s for job updates and reminders." % (" and iOS (via APNs)" if "ios" in c.platforms else ""),
                     ["project.platforms"], [("SMS for every notification", "Costs per message and DLT templates.")],
                     ["Notification payloads must not contain sensitive data."], (0, 0)))
    for kind, val in c.hints:
        if kind == "integration":
            out.append(S("other", val, "Listed in existing_systems; integrate through its official API with a "
                         "service account and least-privilege scopes rather than duplicating its data.",
                         ["infrastructure.existing_systems"], [("Manual CSV exports", "Error-prone and stale.")], [], (0, 0)))
    regulated = set(c.compliance) & {"pci_dss", "hipaa", "iso27001", "soc2", "rbi_localisation"}
    if regulated and not onprem:
        ch = {"aws": "AWS KMS + Secrets Manager + CloudTrail + AWS WAF", "gcp": "Cloud KMS + Secret Manager + Cloud Audit Logs + Cloud Armor",
              "azure": "Azure Key Vault + Activity Log + Azure WAF"}.get(hp, "Managed KMS/secrets + audit logging + WAF")
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
                  "Free Developer plan catches crashes in the app and mobile clients with stack traces.", (0, 26))
        else:
            ob = ("Grafana Cloud (free tier) + OpenTelemetry",
                  "Free tier for logs/metrics; scrub personal data before export since data residency applies.", (0, 50))
    if r["availability"] in ("99.95", "99.99"):
        ob = (ob[0] + " + uptime checks and on-call paging", ob[1] + " %s%% needs alerting that wakes someone up." % r["availability"], ob[2])
    out.append(S("observability", ob[0], ob[1], ["requirements.availability", "infrastructure.data_residency"],
                 [("Datadog", "Strong product but expensive per host at this budget.")], [], ob[2]))
    return out, warns
