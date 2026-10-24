"""Score predictions against sample_output.csv.

Usage (from the repository root):

    python code/evaluation/main.py                          # predict the 32 samples, then score
    python code/evaluation/main.py --pred path/to/pred.csv  # score an existing prediction file
    python code/evaluation/main.py --exclude-ambiguous      # variant run for comparison

Metrics:
  status      deletion_status accuracy per case
  step        precision / recall / F1 over steps compared as whole tuples
              (phase, system_id, resource_id, operation, state, evidence_id)
  field       per-field accuracy over gold steps matched by (phase, resource_id)
  sequence    share of cases whose full steps_json array equals the gold array
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

from extract.env import CACHE_ROOT, REPO_ROOT  # noqa: E402

FIELDS = ("system_id", "resource_id", "operation", "state", "evidence_id")


def load(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return {r["case_id"]: r for r in csv.DictReader(fh)}


def tuples(steps: list[dict]) -> Counter:
    return Counter((s["phase"], s["system_id"], s["resource_id"], s["operation"], s["state"], s["evidence_id"])
                   for s in steps)


def comparable(steps: list[dict]) -> list[dict]:
    return [{k: s[k] for k in ("step_no", "phase", *FIELDS)} for s in steps]


def score(pred: dict[str, dict[str, str]], gold: dict[str, dict[str, str]]) -> dict:
    status_ok = tp = n_pred = n_gold = seq_ok = field_total = 0
    field_ok: Counter = Counter()
    per_case = []
    for case_id, g in sorted(gold.items(), key=lambda kv: int(kv[0].rsplit("_", 1)[1])):
        p = pred.get(case_id, {"deletion_status": "", "steps_json": "[]"})
        gs, ps = json.loads(g["steps_json"]), json.loads(p["steps_json"])
        status_ok += p["deletion_status"] == g["deletion_status"]
        gt, pt = tuples(gs), tuples(ps)
        overlap = sum((gt & pt).values())
        tp += overlap
        n_pred += sum(pt.values())
        n_gold += sum(gt.values())
        exact = comparable(gs) == comparable(ps)
        seq_ok += exact
        by_key = {(s["phase"], s["resource_id"]): s for s in ps}
        for s in gs:
            field_total += 1
            match = by_key.get((s["phase"], s["resource_id"]))
            for f in FIELDS:
                field_ok[f] += bool(match) and match[f] == s[f]
        per_case.append((case_id, g["deletion_status"], p["deletion_status"], overlap,
                         sum(gt.values()), sum(pt.values()), exact))
    precision = tp / n_pred if n_pred else 0.0
    recall = tp / n_gold if n_gold else 0.0
    return {
        "cases": len(gold),
        "status_accuracy": status_ok / len(gold),
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "field_accuracy": {f: field_ok[f] / field_total for f in FIELDS},
        "sequence_accuracy": seq_ok / len(gold),
        "per_case": per_case,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=REPO_ROOT / "dataset")
    parser.add_argument("--pred", type=Path, default=None)
    parser.add_argument("--exclude-ambiguous", action="store_true")
    args = parser.parse_args(argv)

    pred_path = args.pred
    if pred_path is None:
        import main as agent  # code/main.py
        pred_path = CACHE_ROOT / "evaluation" / "sample_pred.csv"
        cli = ["--set", "sample", "--out", str(pred_path), "--skip-extract",
               "--debug", str(CACHE_ROOT / "evaluation" / "sample_debug.jsonl")]
        if args.exclude_ambiguous:
            cli.append("--exclude-ambiguous")
        agent.main(cli)

    result = score(load(pred_path), load(args.dataset / "sample_output.csv"))
    print(f"\ncases={result['cases']}  status accuracy={result['status_accuracy']:.1%}  "
          f"step P/R/F1={result['step_precision']:.1%}/{result['step_recall']:.1%}/{result['step_f1']:.1%}  "
          f"sequence accuracy={result['sequence_accuracy']:.1%}")
    print("field accuracy (gold steps matched by phase+resource): "
          + ", ".join(f"{k}={v:.1%}" for k, v in result["field_accuracy"].items()))
    print("\ncase       gold         pred         matched/gold/pred  exact")
    for case_id, gs, ps, overlap, ng, np_, exact in result["per_case"]:
        flag = "" if gs == ps and exact else "  <--"
        print(f"{case_id:<10} {gs:<12} {ps:<12} {overlap:>3}/{ng:<3}/{np_:<3}         {'yes' if exact else 'no'}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
