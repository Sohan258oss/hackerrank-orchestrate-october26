"""Run auditor stages 1-4 over a request set and report.

Usage (from the repository root):

    python code/engine/run_stages.py                         # 32 public samples, scored
    python code/engine/run_stages.py --set eval              # 518 evaluation requests
    python code/engine/run_stages.py --no-llm                # rules only, no model calls
    python code/engine/run_stages.py --trace case_05 case_25 # debug trace for chosen cases

Stages: 1 intent, 2 identity, 3 scoping, 4 state at audit_at. Scoping always
runs over sample and evaluation requests together, because deletion batches
of different cases interleave and are divided between them.

Writes .cache/engine/stages_<set>.jsonl. For the sample set, results are
scored against sample_output.csv.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.evidence import EvidenceStore  # noqa: E402
from engine.identity import Identity, IdentityIndex  # noqa: E402
from engine.intent import classify_all  # noqa: E402
from engine.scope import build_scopes, scope_summary  # noqa: E402
from engine.state import ResourceState, case_state  # noqa: E402
from extract import llm  # noqa: E402
from extract.env import CACHE_ROOT, REPO_ROOT  # noqa: E402

SETS = {"sample": "sample_deletion_requests.csv", "eval": "deletion_requests.csv"}
EXPECTED_STATE = {   # state a gold step implies was observed at audit_at
    "REPORT_REMAINING_RECORD": {"PRESENT"}, "REPORT_EXPOSED_PII": {"PRESENT"},
    "REPORT_ACTIVE_DATA_FLOW": {"ACTIVE"}, "REPORT_RESTORE_RISK": {"ACTIVE"},
    "VERIFY_RECORD_ABSENT": {"ABSENT"}, "VERIFY_RECORD_PRESENT": {"PRESENT"},
    "VERIFY_RECORD_REDACTED": {"REDACTED"}, "VERIFY_RECORD_ANONYMIZED": {"ANONYMIZED"},
    "VERIFY_DATA_FLOW_DISABLED": {"DISABLED"}, "VERIFY_RESTORE_BLOCKED": {"BLOCKED"},
}


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def usage_for_run(run_id: str) -> dict[str, int]:
    if not llm.USAGE_LOG.is_file():
        return {}
    rows = [json.loads(line) for line in llm.USAGE_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r["run_id"] == run_id and r.get("prompt") == "intent_classifier"]
    return {
        "calls": sum(1 for r in rows if not r["cached"]),
        "cache_hits": sum(1 for r in rows if r["cached"]),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
    }


def print_trace(case_id: str, req: dict[str, str], ident: Identity | None, row: dict[str, Any],
                states: dict[str, ResourceState], risks: list[dict[str, str]]) -> None:
    print(f"\n{'=' * 100}\nTRACE {case_id}  email={req['email']}  requested={req['requested_at']}  "
          f"reported={req['reported_status']}@{req['reported_at']}  audit_at={req['audit_at']}")
    intent = row["intent"]
    print(f"  intent: authorizes_deletion={intent['authorizes_deletion']} via {intent['source']} "
          f"({intent['request_type']}); signals={ {k: v for k, v in intent['signals'].items() if v} }")
    if not ident:
        print(f"  identity: {row['identity_status']}")
        return
    print(f"  identity: {ident.user_id} ({ident.name}) matched via {ident.matched_via}")
    print(f"    emails={ident.emails}\n    phones={ident.phones}\n    hashes={[h[:16] + '…' for h in ident.hashes]}")
    scope = row["scope"]
    print(f"  scoped resources ({len(scope['resources'])}):")
    for rid, s in scope["resources"].items():
        note = f"  [{s['note']}]" if s["note"] else ""
        print(f"    {rid:<18} sources={s['sources']} activities={s['activity_ids']} owners={s['owners']}{note}")
    if scope["excluded"]:
        shown = list(scope["excluded"].items())
        print(f"  excluded ({len(shown)}):")
        for rid, why in shown[:12]:
            print(f"    {rid:<18} {why}")
        if len(shown) > 12:
            print(f"    … {len(shown) - 12} more")
    print("  state at audit_at:")
    for rid, st in states.items():
        print(f"    {rid:<18} {st.state:<10} {st.category or '?':<20} basis={st.basis:<16} evidence={st.evidence_id} "
              f"({st.captured_at})")
        if len(st.copies) > 1 or st.drift:
            print(f"      copies: {[(c['copy'], c['state']) for c in st.copies]}" + (f"  DRIFT: {st.drift}" if st.drift else ""))
        if any(st.exposed.values()):
            print(f"      exposed: { {k: v for k, v in st.exposed.items() if v} }")
        if st.history[:-1]:
            print(f"      earlier evidence: {st.history[:-1]}")
        if st.later_evidence:
            print(f"      ignored (captured after audit_at): {st.later_evidence}")
        if st.claims:
            print(f"      claims: {[(c['activity_id'], c['event_type'], c['reported_result']) for c in st.claims]}")
    print(f"  recreation risks: {risks or 'none'}")


def score(rows: list[dict[str, Any]], gold_rows: list[dict[str, str]]) -> None:
    gold = {g["case_id"]: g for g in gold_rows}
    tp = fp = fn = 0
    ev_match = ev_total = st_match = st_total = 0
    misses, extras, ev_wrong, st_wrong = [], [], [], []
    for row in rows:
        g = gold[row["case_id"]]
        steps = json.loads(g["steps_json"])
        G = {s["resource_id"] for s in steps}
        P = set(row.get("scope", {}).get("resources", {}))
        tp += len(G & P); fp += len(P - G); fn += len(G - P)
        misses += [f"{row['case_id']}:{r}" for r in sorted(G - P)]
        extras += [f"{row['case_id']}:{r}" for r in sorted(P - G)]
        states = row.get("states", {})
        for s in steps:
            st = states.get(s["resource_id"])
            observed = s["phase"] == "FINDING" or g["deletion_status"] == "COMPLETE"
            if s["evidence_id"]:
                ev_total += 1
                if st and st["evidence_id"] == s["evidence_id"]:
                    ev_match += 1
                else:
                    ev_wrong.append(f"{row['case_id']}:{s['resource_id']} gold={s['evidence_id']} "
                                    f"ours={st['evidence_id'] if st else None}")
            if observed and s["operation"] in EXPECTED_STATE:
                st_total += 1
                if st and st["state"] in EXPECTED_STATE[s["operation"]]:
                    st_match += 1
                else:
                    st_wrong.append(f"{row['case_id']}:{s['resource_id']} {s['operation']} "
                                    f"ours={st['state'] if st else 'not scoped'}")
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    print(f"\nscoping vs gold resources: precision {tp}/{tp + fp} = {p:.1%}, recall {tp}/{tp + fn} = {r:.1%}")
    print(f"  missed: {misses}\n  extra:  {extras}")
    print(f"evidence choice vs gold evidence_id: {ev_match}/{ev_total} = {ev_match / max(ev_total, 1):.1%}")
    for w in ev_wrong:
        print(f"  {w}")
    print(f"observed state vs gold-implied state: {st_match}/{st_total} = {st_match / max(st_total, 1):.1%}")
    for w in st_wrong:
        print(f"  {w}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=REPO_ROOT / "dataset")
    parser.add_argument("--set", choices=sorted(SETS), default="sample")
    parser.add_argument("--no-llm", action="store_true", help="rules only; undecided texts stay unresolved")
    parser.add_argument("--trace", nargs="*", default=[], help="case ids to print a debug trace for")
    args = parser.parse_args(argv)

    all_requests = {name: load_csv(args.dataset / f) for name, f in SETS.items()}
    requests = all_requests[args.set]
    combined = all_requests["sample"] + all_requests["eval"]
    intents = classify_all({r["case_id"]: r["request_text"] for r in combined}, use_llm=not args.no_llm)
    index = IdentityIndex(args.dataset)
    resolutions = {r["case_id"]: index.resolve(r["email"], as_of=r["audit_at"]) for r in combined}
    deletion_identities = {c: res.identity for c, res in resolutions.items()
                           if res.identity and intents[c].authorizes_deletion is not False}

    store = EvidenceStore(args.dataset, index)
    scopes = build_scopes(combined, deletion_identities, store)

    out_path = CACHE_ROOT / "engine" / f"stages_{args.set}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    traces = []
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        for req in requests:
            c = req["case_id"]
            intent, resolution = intents[c], resolutions[c]
            row: dict[str, Any] = {
                "case_id": c,
                "intent": intent.to_dict(),
                "predicted_not_applicable": intent.authorizes_deletion is False,
                "identity_status": resolution.status,
                "identities": [x.to_dict() for x in resolution.candidates],
            }
            states, risks = {}, []
            if c in scopes:
                row["scope"] = scope_summary(scopes[c])
                states, risks = case_state(store, scopes[c], req["audit_at"])
                row["states"] = {rid: asdict(st) for rid, st in states.items()}
                row["recreation_risks"] = risks
            rows.append(row)
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            if c in args.trace:
                traces.append((c, req, deletion_identities.get(c), row, states, risks))

    sources = collections.Counter(r["intent"]["source"] for r in rows)
    decided = collections.Counter(
        "NOT_APPLICABLE" if r["intent"]["authorizes_deletion"] is False
        else "deletion" if r["intent"]["authorizes_deletion"] else "unresolved" for r in rows)
    print(f"set={args.set} requests={len(rows)}  intent sources={dict(sources)}  outcome={dict(decided)}")
    statuses = collections.Counter(r["identity_status"] for r in rows)
    via = collections.Counter(",".join(r["identities"][0]["matched_via"]) for r in rows if r["identities"])
    print(f"identity: {dict(statuses)}  matched_via={dict(via)}")
    scoped = [r for r in rows if "scope" in r]
    n_res = [len(r["scope"]["resources"]) for r in scoped]
    src = collections.Counter(s for r in scoped for v in r["scope"]["resources"].values() for s in v["sources"])
    amb = sum(len(r["scope"]["ambiguous"]) for r in scoped)
    st_counts = collections.Counter(v["state"] for r in scoped for v in r["states"].values())
    basis = collections.Counter(v["basis"] for r in scoped for v in r["states"].values())
    drift = sum(1 for r in scoped for v in r["states"].values() if v["drift"])
    risk_kinds = collections.Counter(k["kind"] for r in scoped for k in r["recreation_risks"])
    print(f"scoping: {len(scoped)} deletion cases, resources/case min={min(n_res, default=0)} "
          f"avg={sum(n_res) / max(len(n_res), 1):.1f} max={max(n_res, default=0)}; sources={dict(src)}; "
          f"ambiguous batch assignments={amb}; alias-resolved records={store.alias_resolved}")
    print(f"state: {dict(st_counts)}  basis={dict(basis)}  drift={drift}  recreation={dict(risk_kinds)}")

    if args.set == "sample":
        gold_rows = load_csv(args.dataset / "sample_output.csv")
        gold_na = {g["case_id"]: g["deletion_status"] == "NOT_APPLICABLE" for g in gold_rows}
        correct = sum(1 for r in rows if r["predicted_not_applicable"] == gold_na[r["case_id"]])
        print(f"intent accuracy (NOT_APPLICABLE vs other): {correct}/{len(rows)} = {correct / len(rows):.1%}")
        score(rows, gold_rows)

    for trace in traces:
        print_trace(*trace)
    usage = usage_for_run(llm.RUN_ID)
    print(f"\nLLM intent usage this run: {usage or 'none'}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
