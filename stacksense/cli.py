"""Command line: python -m stacksense run <scenario.json> [-o out.json]

A file may hold one scenario object or a list of them (like the public
scenarios file). With a list, one <scenario_id>_output.json per scenario is
written into the --out-dir directory.
"""
import argparse
import json
import os
import sys

from .engine import recommend


def _dump(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main(argv=None):
    p = argparse.ArgumentParser(prog="stacksense", description="Tech stack recommendation engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Run a scenario JSON file")
    r.add_argument("scenario")
    r.add_argument("-o", "--out", help="Output file (single scenario). Default: print to stdout")
    r.add_argument("--out-dir", default="outputs", help="Directory for list input (default: outputs)")
    s = sub.add_parser("serve", help="Start the web UI")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1")
    a = p.parse_args(argv)
    if a.cmd == "serve":
        from .server import serve
        serve(a.host, a.port)
        return
    try:
        with open(a.scenario, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        out = {"status": "invalid_input", "summary": "Could not read the scenario file.", "stack": None,
               "warnings": [], "assumptions": [], "validation_errors": [{"field": "$", "message": str(e)}]}
        print(json.dumps(out, indent=2))
        sys.exit(2)
    if isinstance(data, list):
        os.makedirs(a.out_dir, exist_ok=True)
        for i, sc in enumerate(data):
            res = recommend(sc)
            sid = res.get("scenario_id") or "scenario_%d" % (i + 1)
            path = os.path.join(a.out_dir, "%s_output.json" % sid)
            _dump(res, path)
            print("%-6s %-17s %s -> %s" % (sid, res["status"], res.get("architecture_style"), path))
        return
    res = recommend(data)
    if a.out:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        _dump(res, a.out)
        print("%s -> %s" % (res["status"], a.out))
    else:
        print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
