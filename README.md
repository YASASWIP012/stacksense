# StackSense

StackSense recommends a tech stack (frontend, backend, database, hosting and the supporting services a project needs) from structured project parameters, and explains every choice. Each component lists the input fields that drove it, the alternatives it beat and why they lost, and its trade-offs. Hard constraints (data residency, compliance, cloud preference, deployment model) are never broken silently: options that violate them are filtered out and named as excluded alternatives, and contradictory inputs come back as `infeasible` or as a clearly labelled relaxed stack. The engine is plain, deterministic Python with no LLM anywhere, so the same input always gives the same stack. It runs with no installation beyond Python itself.

On the six public scenarios it returns, for example, a free static Astro site for the portfolio (P1), Spring Boot + RDS PostgreSQL in Mumbai with Razorpay and RazorpayX payouts for the RBI-regulated lender (P4), ASP.NET Core on Azure App Service with Entra ID SSO and an end-of-life warning for SQL Server 2019 (P5), and an offline-first React Native app on Supabase in Mumbai for the field-survey NGO (P6). All outputs are in [`outputs/`](outputs/).

**Repository:** https://github.com/YASASWIP012/stacksense

---

## 1. Quick start (under 2 minutes)

You need **Python 3.9 or newer** and nothing else. There are no third-party packages, so there is no `pip install`.

```bash
cd source
python3 -m stacksense serve            # opens on http://127.0.0.1:8000
```

Open <http://127.0.0.1:8000>, pick **P1–P6** or an edge case from the dropdown, or click **Import JSON** to load any scenario file. Press **Export output JSON** to save the result.

On Windows use `py -m stacksense serve` (or `python`), depending on how Python was installed.

## 2. Run a scenario file from the command line

```bash
# one scenario -> one output file
python3 -m stacksense run scenarios/public/P2.json -o outputs/P2_output.json

# print to the terminal instead
python3 -m stacksense run path/to/hidden_scenario.json

# a file that holds a JSON list of scenarios -> outputs/<scenario_id>_output.json for each
python3 -m stacksense run scenarios/public/all_public.json --out-dir outputs
```

The input must follow the standard schema in Section 3.2 of the problem statement. Output follows Section 4 and is written where `-o`/`--out-dir` says (default directory: `outputs/`). Malformed JSON, missing fields and wrong types never crash the engine. They return `"status": "invalid_input"` with field-level `validation_errors`.

## 3. Run the tests

```bash
python3 -m unittest discover -s tests -v
```

There are 24 tests. They cover the output contract on all public scenarios, determinism, key-order independence, hard constraints (residency, cloud, on-premise), sensitivity to small input changes, and every edge-case category (missing and invalid fields, garbage input, contradictions, over-engineering traps, air-gapped deployment, end-of-life systems, infeasible hard conflicts).

## 4. LLM switch

**StackSense uses no LLM at all.** Description parsing is done by deterministic keyword rules (`stacksense/signals.py`), and every explanation is generated from the scoring trace. There is nothing to switch off: the LLM-off test passes by construction, and every output carries `"llm_used": false`.

## 5. System architecture

```
 scenario JSON ──► schema.validate ──► build context ──► rules.conflicts ──► hard filters ──► unary scoring
   (UI / CLI)      field errors,        scale level L1-5,   infeasible?       residency,       per candidate:
                   defaults →           peak, req/s,        relax reqs?       compliance,      language fit,
                   assumptions          description         EOL checks        cloud, deploy    priorities, ops,
                                        signals                                                 learning, budget,
                                                                                                requirement fit
                                                                                                    │
     explained JSON ◄── cost band ◄── supporting services ◄── realise services ◄── joint search over every
     (driven_by,       per layer      auth, payments,         in the right region  compatible combination of
     alternatives,     + assumptions  storage, cache, queue,  (RDS Mumbai, Azure   web × mobile × backend ×
     trade-offs,                      search, realtime, ML,   SQL Pune, Supabase   database × hosting with
     warnings)                        CI/CD, observability    ap-south-1 …)        pair rules + budget fit
```

| Module | What it does |
|---|---|
| `stacksense/schema.py` | Input schema (mandatory plus 7 optional extras), defaults and field-level validation. Never raises. |
| `stacksense/kb.py` | Knowledge base: 64 core technologies across 6 layers, 15 providers with region and compliance facts, 22 end-of-life rules, existing-system patterns. Every entry cites sources. |
| `stacksense/signals.py` | Deterministic keyword signals from `project.description` (GPS, ledger, low-end devices, offline, KYC…). |
| `stacksense/rules.py` | Hard-vs-hard conflicts (→ `infeasible`), requirement relaxations (air-gapped payments…), soft contradictions, EOL warnings. |
| `stacksense/engine.py` | Scoring, the combination search with compatibility rules, service realisation, explanations and alternatives. |
| `stacksense/supporting.py` | Supporting layers, each added only when an input calls for it, under the same hard constraints. |
| `stacksense/cost.py` | Order-of-magnitude monthly cost band with a per-layer breakdown and stated assumptions. |
| `stacksense/server.py`, `web/` | Standard-library HTTP server and a vanilla-JS UI (form generated from the schema). |
| `stacksense/cli.py` | `run` and `serve` commands. |

**Data flow.** A scenario is validated first. Invalid input stops here. Next, a context is derived: scale level L1–L5 from users and peak concurrency, estimated req/s and data volume, effective residency (for example, `rbi_localisation` forces India), and description signals. Conflicting hard constraints stop with `infeasible`. Each candidate in each layer is then filtered by hard constraints and given a unary score, and the engine records every contribution as a (field, points, reason) triple. The top candidates per layer are combined exhaustively (up to a few thousand combinations), invalid pairings are dropped (e.g. Django on Netlify, SQLite on serverless, Supabase without Postgres), and pair bonuses or penalties plus the budget fit are added. The highest total wins, with ties broken by candidate id, which keeps the result deterministic. Those recorded contributions produce `driven_by`, the justifications, the alternatives' `why_not` and the trade-offs.

## 6. Technology used to build the engine

| Component | Version |
|---|---|
| Python | 3.9+ (developed and tested on 3.15) |
| Python standard library only | `json`, `re`, `itertools`, `hashlib`, `http.server`, `argparse`, `unittest` |
| Web UI | HTML + CSS + vanilla JavaScript (ES2020), no build step, no CDN |

## 7. Project structure

```
source/
├── README.md
├── stacksense/            engine package (schema, kb, signals, rules, engine, supporting, cost, cli, server)
├── web/                   index.html, app.js, style.css
├── scenarios/
│   ├── public/            P1–P6 (one file each) + all_public.json
│   └── edge/              12 edge-case scenarios used in the report and tests
├── tests/test_engine.py   24 unit tests
└── outputs/               engine output for P1–P6 (and edge cases)
```

`.env.example` is not included because the engine reads no environment variables.

## 8. Extra inputs StackSense collects

All of these are optional. The engine works on files that contain only the standard fields.

| Field | Values | What it changes |
|---|---|---|
| `extras.geo_spread` | single_city, single_country, multi_region, global | CDN/edge bonus; static pages favoured for global audiences |
| `extras.vendor_lock_in_tolerance` | low, medium, high | Penalises proprietary services (DynamoDB, Firestore, Firebase, Lambda) |
| `extras.consistency` | strong, eventual | Strong: transactional databases and backends win |
| `extras.data_shape` | relational, document, key_value, time_series, graph, mixed | Database fit (e.g. time_series → TimescaleDB) |
| `extras.team_growth_12_months` | integer | Favours mainstream stacks with large hiring pools |
| `extras.admin_panel_needed` | boolean | Favours Django/Laravel/Rails built-in admin |
| `extras.peak_requests_per_sec` | number | Overrides the req/s estimate; high values favour Go/.NET/Phoenix |

## 9. Credits

- **Third-party libraries:** none. The engine and UI use only the Python standard library and browser APIs.
- **Datasets and sources** (cited per entry in `kb.py`, all listed in the report): official documentation and pricing pages of AWS, Google Cloud, Azure, DigitalOcean, Render, Railway, Supabase, Neon, MongoDB Atlas, Vercel, Netlify, Cloudflare, GitHub, Firebase, Razorpay, Cashfree, Stripe, MSG91, Twilio, Sentry; framework docs (Astro, Next.js, Nuxt, SvelteKit, React, Vite, Angular, Blazor, Hugo, Django, HTMX, FastAPI, NestJS, Fastify, Spring Boot, ASP.NET Core, Go, Laravel, Rails, Phoenix, Expo, Flutter, Kotlin, SwiftUI); the Stack Overflow Developer Survey 2025; endoflife.date and vendor lifecycle pages; RBI's 2018 directive on storage of payment system data; India's DPDP Act 2023; TRAI DLT rules.
- **AI assistance:** Claude Code (Anthropic) was used as a coding assistant. See the report's AI-assistance declaration.
