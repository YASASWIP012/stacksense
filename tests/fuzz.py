"""Random-scenario fuzzer: python3 tests/fuzz.py [N]. Fails on any exception or schema break."""
import json, random, sys, os, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stacksense import recommend, schema

def rand_scenario(r):
    sc = {"scenario_id": "F", "project": {"type": r.choice(schema.PROJECT_TYPES),
          "description": r.choice(["", "Live chat and GPS photos offline", "ledger for loans with UPI", "whiteboard with live cursors",
                                   "IoT sensor telemetry", "multiplayer game", "newsroom blog in Markdown", "KYC upload", "video consults"]),
          "platforms": r.sample(schema.PLATFORMS, r.randint(1, 3))},
          "scale": {"users_at_launch": r.choice([0, 10, 500, 5000, 50000, 1000000]), "users_12_months": r.choice([0, 100, 3000, 30000, 300000, 5000000]),
                    "traffic_pattern": r.choice(schema.TRAFFIC), "workload": r.choice(schema.WORKLOAD)},
          "team": {"size": r.randint(1, 20), "experience": r.choice(schema.EXPERIENCE),
                   "languages": r.sample(["javascript", "typescript", "python", "java", "kotlin", "csharp", "go", "php", "ruby", "rust", "sql", "dart", "elixir"], r.randint(0, 3)),
                   "has_devops": r.random() < 0.5},
          "budget": {"monthly_infra_usd": r.choice([0, 10, 50, 150, 500, 2000, 10000])},
          "timeline": {"mvp_weeks": r.randint(1, 30)},
          "infrastructure": {"cloud_preference": r.choice(schema.CLOUD), "deployment_model": r.choice(schema.DEPLOYMENT),
                             "data_residency": r.choice(schema.RESIDENCY),
                             "existing_systems": r.sample(["MySQL 5.7", "Active Directory (on-prem)", "Oracle Database 19c", "Google Workspace", "Node.js 16 API", "Kafka", "SQL Server 2019", "Okta", "Salesforce"], r.randint(0, 3))},
          "requirements": {k: r.random() < 0.4 for k in ("realtime", "offline_support", "full_text_search", "file_media_storage", "payments", "multi_tenant", "seo_critical")},
          "compliance": r.sample(schema.COMPLIANCE, r.randint(0, 3)),
          "priorities": {k: r.randint(1, 5) for k in schema.PRIORITY_KEYS}}
    sc["requirements"].update({"auth": r.choice(schema.AUTH), "ml_features": r.choice(schema.ML), "availability": r.choice(schema.AVAILABILITY),
                               "p95_latency_ms": r.choice([None, 50, 100, 300, 800])})
    if r.random() < 0.5:
        sc["scale"]["peak_concurrent_users"] = r.choice([1, 50, 800, 5000, 80000])
    if r.random() < 0.3:
        sc["extras"] = {"geo_spread": r.choice(schema.GEO_SPREAD), "vendor_lock_in_tolerance": r.choice(schema.LOCK_IN),
                        "data_shape": r.choice(schema.DATA_SHAPE), "consistency": r.choice(schema.CONSISTENCY)}
    return sc

def main(n):
    r = random.Random(42)
    bad = 0
    stats = {}
    for i in range(n):
        sc = rand_scenario(r)
        try:
            o = recommend(sc)
            json.dumps(o)
            stats[o["status"]] = stats.get(o["status"], 0) + 1
            if o["stack"]:
                for k in ("frontend", "backend", "database", "hosting"):
                    assert o["stack"][k]["alternatives"], k
        except Exception:
            bad += 1
            if bad <= 3:
                print(json.dumps(sc)); traceback.print_exc()
    print("runs", n, "failures", bad, stats)
    sys.exit(1 if bad else 0)

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2000)
