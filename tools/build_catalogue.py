"""Build catalogue.json from curated entries plus the published JevBench / ImageJevBench data.

Run on Sandy (needs /opt/model-market-comparison); the output is committed.
    python3 tools/build_catalogue.py tools/entries.json > catalogue.json
"""
import json, sys
from pathlib import Path

BENCH = Path("/opt/model-market-comparison/data/raw/benchmarks/jevbench")

def jevbench_rows():
    data = json.loads((BENCH / "v1.4.2/jevbench-v1.4.2-results.json").read_text())
    return {r["key"]: r for r in data["systems"]}

def jevbench_block(row):
    axes = row.get("axes") or {}
    capability = (axes["intelligence"] + axes["calibration"]) / 2 if axes.get("intelligence") is not None and axes.get("calibration") is not None else None
    return {"version": "v1.4.2", "key": row["key"], "score": round(capability, 2) if capability is not None else None,
            "composite": round(row["jevbench_score"], 2) if row.get("jevbench_score") is not None else None,
            "rank": row.get("rank"), "listing": row.get("listing"), "tiers": row.get("tiers"),
            "p50_s": (row.get("speed") or {}).get("p50_s_adjusted"),
            "cost_usd_per_1k": (row.get("cost") or {}).get("usd_per_1000")}

def main():
    entries = json.loads(Path(sys.argv[1]).read_text())
    rows = jevbench_rows()
    out = []
    for e in entries:
        key = e.pop("jevbench_key", None)
        if key:
            if key not in rows:
                raise SystemExit(f"unknown JevBench key {key}")
            e["jevbench"] = jevbench_block(rows[key])
        out.append(e)
    json.dump({"generated_from": "JevBench v1.4.2 published results; ImageJevBench v0.1 published results", "entries": out}, sys.stdout, indent=1, ensure_ascii=False)

main()
