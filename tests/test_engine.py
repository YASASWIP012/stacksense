"""StackSense tests: run with  python -m unittest discover -s tests -v"""
import copy
import glob
import json
import os
import unittest

from stacksense import recommend

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return json.load(f)


PUBLIC = {sid: load("scenarios/public/%s.json" % sid) for sid in ("P1", "P2", "P3", "P4", "P5", "P6")}
CORE = ("frontend", "backend", "database", "hosting")


def choices(out):
    return {k: out["stack"][k]["choice"] for k in CORE}


class OutputContract(unittest.TestCase):
    def test_public_scenarios_follow_output_schema(self):
        for sid, sc in PUBLIC.items():
            out = recommend(sc)
            with self.subTest(sid=sid):
                self.assertIn(out["status"], ("ok", "ok_with_warnings"))
                self.assertIn(out["architecture_style"], ("static", "monolith", "modular_monolith", "serverless",
                                                          "service_oriented", "microservices", "hybrid", "other"))
                for k in CORE:
                    comp = out["stack"][k]
                    self.assertTrue(comp["choice"])
                    self.assertTrue(comp["justification"])
                    self.assertTrue(comp["driven_by"])
                    self.assertGreaterEqual(len(comp["alternatives"]), 1)
                    for a in comp["alternatives"]:
                        self.assertTrue(a["choice"] and a["why_not"])
                cost = out["estimated_monthly_cost_usd"]
                self.assertLessEqual(cost["min"], cost["max"])
                self.assertTrue(cost["assumptions"])
                for w in out["warnings"]:
                    self.assertIn(w["severity"], ("info", "warning", "critical"))
                json.dumps(out)  # serialisable

    def test_determinism(self):
        for sid, sc in PUBLIC.items():
            a = json.dumps(recommend(copy.deepcopy(sc)), sort_keys=True)
            b = json.dumps(recommend(copy.deepcopy(sc)), sort_keys=True)
            self.assertEqual(a, b, sid)

    def test_key_order_does_not_matter(self):
        sc = PUBLIC["P2"]
        rev = json.loads(json.dumps(sc), object_pairs_hook=lambda kv: dict(reversed(kv)))
        self.assertEqual(choices(recommend(sc)), choices(recommend(rev)))


class Recommendations(unittest.TestCase):
    def test_p1_static_portfolio_costs_nothing(self):
        out = recommend(PUBLIC["P1"])
        self.assertEqual(out["architecture_style"], "static")
        self.assertEqual(out["stack"]["backend"]["choice"], "None")
        self.assertEqual(out["stack"]["database"]["choice"], "None")
        self.assertEqual(out["estimated_monthly_cost_usd"]["max"], 0)

    def test_india_residency_respected(self):
        for sid in ("P2", "P4", "P5", "P6"):
            out = recommend(PUBLIC[sid])
            db = out["stack"]["database"]["choice"]
            self.assertTrue(any(x in db for x in ("Mumbai", "India", "Pune", "BLR1")), (sid, db))
            self.assertNotIn("Stripe", " ".join(s["choice"] for s in out["stack"]["supporting"]))

    def test_p4_stays_on_aws_with_relational_db(self):
        out = recommend(PUBLIC["P4"])
        self.assertIn("AWS", out["stack"]["hosting"]["choice"])
        self.assertIn("PostgreSQL", out["stack"]["database"]["choice"])
        self.assertIn("Razorpay", json.dumps(out["stack"]["supporting"]))

    def test_p5_uses_azure_and_entra(self):
        out = recommend(PUBLIC["P5"])
        self.assertIn("Azure", out["stack"]["hosting"]["choice"])
        self.assertIn("Entra", json.dumps(out["stack"]["supporting"]))
        self.assertTrue(any(w["code"] == "END_OF_LIFE" for w in out["warnings"]))

    def test_p6_offline_mobile(self):
        out = recommend(PUBLIC["P6"])
        self.assertNotIn("Progressive Web App", out["stack"]["frontend"]["choice"])
        self.assertIn("Offline-first", json.dumps(out["stack"]["supporting"]))

    def test_p3_collaborative_realtime(self):
        out = recommend(PUBLIC["P3"])
        self.assertIn("Yjs", json.dumps(out["stack"]["supporting"]))
        self.assertNotIn("Kubernetes", out["stack"]["hosting"]["choice"])

    def test_no_kubernetes_for_small_projects(self):
        for sid, sc in PUBLIC.items():
            out = recommend(sc)
            self.assertNotRegex(out["stack"]["hosting"]["choice"], "EKS|GKE|AKS|Kubernetes", sid)
            self.assertNotEqual(out["architecture_style"], "microservices", sid)


class Sensitivity(unittest.TestCase):
    """Small input changes should cause sensible, local changes."""

    def mutate(self, sid, path, value):
        sc = copy.deepcopy(PUBLIC[sid])
        cur = sc
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur[p]
        cur[parts[-1]] = value
        return recommend(sc)

    def test_residency_change_moves_region(self):
        out = self.mutate("P2", "infrastructure.data_residency", "eu")
        self.assertNotIn("Mumbai", out["stack"]["database"]["choice"])

    def test_cloud_preference_is_hard(self):
        for cloud, word in (("aws", "A"), ("gcp", "G"), ("azure", "Azure")):
            out = self.mutate("P2", "infrastructure.cloud_preference", cloud)
            h = out["stack"]["hosting"]["choice"] + " " + out["stack"]["database"]["choice"]
            self.assertNotIn("Supabase", h, cloud)
            self.assertNotIn("DigitalOcean", h, cloud)

    def test_team_language_shift(self):
        out = self.mutate("P5", "team.languages", ["python", "javascript"])
        self.assertNotIn("ASP.NET", out["stack"]["backend"]["choice"])

    def test_small_budget_change_keeps_stack(self):
        a = choices(recommend(PUBLIC["P3"]))
        b = choices(self.mutate("P3", "budget.monthly_infra_usd", 1600))
        self.assertEqual(a, b)

    def test_on_premise_only_self_hostable(self):
        sc = copy.deepcopy(PUBLIC["P2"])
        sc["infrastructure"]["deployment_model"] = "on_premise"
        sc["infrastructure"]["cloud_preference"] = "none"
        out = recommend(sc)
        blob = json.dumps(out["stack"])
        for saas in ("Supabase", "Vercel", "Cloudflare", "Firebase", "Amazon", "Azure"):
            self.assertNotIn(saas + " ", choices(out)["hosting"] + choices(out)["database"])
        self.assertIn("self-hosted", out["stack"]["database"]["choice"])


class EdgeCases(unittest.TestCase):
    def run_edge(self, name):
        return recommend(load("scenarios/edge/%s.json" % name))

    def test_missing_fields(self):
        out = self.run_edge("E01_missing_fields")
        self.assertEqual(out["status"], "invalid_input")
        self.assertIsNone(out["stack"])
        fields = {e["field"] for e in out["validation_errors"]}
        self.assertIn("project.platforms", fields)
        self.assertIn("budget.monthly_infra_usd", fields)

    def test_invalid_values(self):
        out = self.run_edge("E02_invalid_values")
        self.assertEqual(out["status"], "invalid_input")
        fields = {e["field"] for e in out["validation_errors"]}
        for f in ("scale.users_at_launch", "project.type", "priorities.cost", "requirements.realtime",
                  "budget.monthly_infra_usd", "team.experience"):
            self.assertIn(f, fields)

    def test_garbage_never_crashes(self):
        for raw in ([], "x", 42, None, {"project": "x"}, {"priorities": [1]}, {"scale": {"users_at_launch": True}}):
            out = recommend(raw) if isinstance(raw, dict) else recommend(raw)
            self.assertEqual(out["status"], "invalid_input")

    def test_contradictions_flagged_critical(self):
        out = self.run_edge("E03_contradictions")
        crit = {w["code"] for w in out["warnings"] if w["severity"] == "critical"}
        self.assertTrue({"AVAILABILITY_VS_TEAM", "OVER_BUDGET"} <= crit, crit)
        self.assertTrue(any(a.startswith("Relaxed") for a in out["assumptions"]))

    def test_static_high_traffic_stays_static(self):
        out = self.run_edge("E04_static_high_traffic")
        self.assertEqual(out["architecture_style"], "static")

    def test_air_gapped(self):
        out = self.run_edge("E06_air_gapped")
        self.assertIn("On-premise", out["stack"]["hosting"]["choice"])
        sup = json.dumps(out["stack"]["supporting"])
        self.assertNotIn("Razorpay", sup)
        self.assertNotIn("GitHub Actions", sup)
        self.assertTrue(any(w["code"] == "AIRGAP_VS_PAYMENTS" for w in out["warnings"]))

    def test_eol_systems_warned(self):
        out = self.run_edge("E07_legacy_systems")
        self.assertGreaterEqual(sum(1 for w in out["warnings"] if w["code"] == "END_OF_LIFE"), 4)

    def test_hard_conflicts_infeasible(self):
        for name in ("E09_hard_conflict_cloud", "E10_rbi_vs_eu"):
            out = self.run_edge(name)
            self.assertEqual(out["status"], "infeasible", name)
            self.assertTrue(any(w["severity"] == "critical" for w in out["warnings"]))

    def test_all_edge_files_run(self):
        for path in sorted(glob.glob(os.path.join(ROOT, "scenarios", "edge", "*.json"))):
            with open(path) as f:
                out = recommend(json.load(f))
            self.assertIn(out["status"], ("ok", "ok_with_warnings", "infeasible", "invalid_input"), path)


if __name__ == "__main__":
    unittest.main()
