#!/usr/bin/env python3
"""Verify parallel fan-out from a saved session under state/sessions/<sid>/.

Usage (from S8SharedCode/code/):
  uv run python verify_fanout.py s8-bbc2db62
  uv run python verify_fanout.py s8-bbc2db62 --expect 3

Checks:
  1. First planner plan has >= N sibling workers with inputs=[] (usually researcher).
  2. Those workers' wall-clock window overlaps.
  3. Wave duration ~= max(elapsed_s), not sum(elapsed_s).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SESSIONS = Path(__file__).parent / "state" / "sessions"
FANOUT_SKILLS = frozenset({"researcher", "retriever", "distiller", "summariser"})


def load_nodes(sid: str) -> list[dict]:
    nodes_dir = SESSIONS / sid / "nodes"
    if not nodes_dir.is_dir():
        raise SystemExit(f"no nodes/ in {SESSIONS / sid}")
    out = []
    for p in sorted(nodes_dir.glob("n_*.json")):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def load_graph(sid: str) -> dict:
    p = SESSIONS / sid / "graph.json"
    if not p.is_file():
        raise SystemExit(f"missing {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def first_planner_plan(graph: dict) -> tuple[dict | None, list[dict]]:
    """Return (planner_node, planned worker specs from output.nodes)."""
    planners = [n for n in graph["nodes"] if n.get("skill") == "planner"]
    if not planners:
        return None, []
    # Initial plan is the first planner node (n:1) unless only recovery planners exist
    root = min(planners, key=lambda n: int(n["id"].split(":")[1]))
    out = (root.get("result") or {}).get("output") or {}
    planned = out.get("nodes") or (root.get("result") or {}).get("successors") or []
    return root, planned


def fanout_workers(graph: dict, skill: str | None = "researcher") -> list[dict]:
    """Executed graph nodes that match fan-out workers (inputs=[])."""
    workers = []
    for n in graph["nodes"]:
        if skill and n.get("skill") != skill:
            continue
        if n.get("skill") not in FANOUT_SKILLS:
            continue
        if n.get("inputs") == []:
            workers.append(n)
    if skill:
        return workers
    return [
        n for n in graph["nodes"]
        if n.get("skill") in FANOUT_SKILLS and n.get("inputs") == []
    ]


def diagnose_plan(planned: list[dict], workers: list[dict]) -> None:
    by_skill: dict[str, list] = {}
    for p in planned:
        by_skill.setdefault(p.get("skill", "?"), []).append(p)
    print("\n--- Planner first plan ---")
    for sk, items in by_skill.items():
        print(f"  {sk}: {len(items)} node(s)")
        for it in items:
            meta = it.get("metadata") or {}
            q = (meta.get("question") or "")[:70]
            print(f"    label={meta.get('label')!r}  question={q!r}")

    if len(by_skill.get("retriever", [])) == 1 and not by_skill.get("researcher"):
        print(
            "\nNOTE: Planner chose ONE retriever (memory shortcut), not 3 researchers."
            "\n  Your MEMORY HITS already mention similar city-population queries."
            "\n  Fix: use the fresh-city query in readmedag.md (Tokyo / Seoul / Mexico City)"
            "\n       and phrase it as separate live web searches."
        )
    if len(by_skill.get("researcher", [])) == 1:
        print(
            "\nNOTE: Planner emitted only ONE researcher — no fan-out."
            "\n  Name three distinct items and ask for a comparison across all of them."
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session_id", help="e.g. s8-bbc2db62")
    ap.add_argument("--expect", type=int, default=3,
                    help="minimum parallel fan-out worker nodes (researcher)")
    ap.add_argument("--skill", default="researcher",
                    help="worker skill to count (default: researcher)")
    args = ap.parse_args()
    sid = args.session_id

    graph = load_graph(sid)
    node_files = {n["node_id"]: n for n in load_nodes(sid)}
    _, planned = first_planner_plan(graph)
    workers = fanout_workers(graph, args.skill)

    print(f"Session: {sid}")
    qpath = SESSIONS / sid / "query.txt"
    print(f"Query: {qpath.read_text(encoding='utf-8').strip()[:120]}...")
    print(f"Parallel {args.skill} nodes (inputs=[]): {len(workers)}")
    for w in workers:
        meta = w.get("metadata") or {}
        st = w.get("status", "?")
        print(f"  - {w['id']} [{st}] label={meta.get('label')!r} question={meta.get('question', '')[:60]!r}")

    if len(workers) < args.expect:
        diagnose_plan(planned, workers)
        print(f"\nFAIL: expected >= {args.expect} fan-out {args.skill} nodes, got {len(workers)}")
        print("See readmedag.md section 'Fan-out test query'.")
        return 1

    spans = []
    for w in workers:
        nf = node_files.get(w["id"])
        if not nf or nf.get("status") != "complete":
            print(f"\nWARN: {w['id']} has no complete node file — timing may be incomplete")
            continue
        spans.append((w["id"], nf["started_at"], nf["completed_at"], nf["result"]["elapsed_s"]))

    if len(spans) < args.expect:
        print("\nFAIL: need completed node files with timestamps for all fan-out branches")
        return 1

    t0 = min(s for _, s, _, _ in spans)
    t1 = max(c for _, _, c, _ in spans)
    wall = t1 - t0
    elapsed = [e for _, _, _, e in spans]
    sum_elapsed = sum(elapsed)
    max_elapsed = max(elapsed)

    print("\n--- Parallel layer timing ---")
    for nid, s, c, e in spans:
        print(f"  {nid}: started +{s - t0:.1f}s  duration {e:.1f}s  (wall slice {c - s:.1f}s)")
    print(f"\n  Wall-clock for wave (min start -> max end): {wall:.1f}s")
    print(f"  Sum of per-branch elapsed_s:                {sum_elapsed:.1f}s")
    print(f"  Max of per-branch elapsed_s:                {max_elapsed:.1f}s")

    ok_wall = abs(wall - max_elapsed) < max(15.0, max_elapsed * 0.25)
    ok_not_sum = wall < sum_elapsed * 0.5

    if ok_wall and ok_not_sum:
        print("\nPASS: wave time tracks max(branch), not sum(branches) - parallel gather confirmed.")
        return 0

    print("\nFAIL: timing does not look parallel (or timestamps missing/skewed).")
    if not ok_wall:
        print(f"  wall ({wall:.1f}s) should be close to max elapsed ({max_elapsed:.1f}s)")
    if not ok_not_sum:
        print(f"  wall should be much less than sum ({sum_elapsed:.1f}s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
