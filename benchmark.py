"""Run beside agent.py and prompt.py. No generated/mock answers in this benchmark."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys
from time import perf_counter
import uuid

import agent


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure_scan(source, repetitions):
    durations = []
    targets = None
    for _ in range(repetitions):
        started = perf_counter()
        targets = agent.extract_targets(source)
        durations.append(perf_counter() - started)
    return targets, {
        "repetitions": repetitions,
        "median_seconds": statistics.median(durations),
        "min_seconds": min(durations),
        "max_seconds": max(durations),
        "first_seconds": durations[0],
    }


def main():
    parser = argparse.ArgumentParser(description="Measure the current LaTeX review agent.")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--runs", type=int, default=1,
                        help="Real complete runs; each can make billable API calls.")
    parser.add_argument("--scan-only", action="store_true",
                        help="Measure extraction only; never calls a model or Lean.")
    parser.add_argument("--scan-repetitions", type=int, default=200)
    parser.add_argument("--output", type=Path, default=Path("benchmark_results.json"))
    args = parser.parse_args()
    if min(args.attempts, args.runs, args.scan_repetitions) < 1:
        parser.error("attempts, runs and scan-repetitions must be positive.")

    source_path = args.file.expanduser().resolve()
    source = source_path.read_bytes().decode("utf-8")
    targets, scan = measure_scan(source, args.scan_repetitions)
    result = {
        "mode": "scan_only" if args.scan_only else "full_review",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(source_path),
        "source_sha256": fingerprint(source_path),
        "agent_sha256": fingerprint(Path(agent.__file__)),
        "prompt_sha256": fingerprint(Path(agent.__file__).with_name("prompt.py")),
        "target_count": len(targets),
        "targets": [asdict(target) for target in targets],
        "scan": scan,
        "formalization_measured": False,
        "runs": [],
    }
    save(args.output, result)
    print(f"Targets: {len(targets)}; extraction median: {scan['median_seconds']:.6f} s")
    if args.scan_only:
        print("Extraction only. No formalization or Lean compilation was measured.")
        return 0

    if not args.project or not args.model:
        parser.error("A full review requires --project and --model.")
    project = args.project.expanduser().resolve()
    if not shutil.which("lake"):
        result["unavailable_reason"] = "lake is not installed or is not on PATH"
        save(args.output, result)
        parser.error(result["unavailable_reason"])
    if not (project / "lean-toolchain").is_file():
        parser.error("--project must be an existing Lean project with Mathlib.")
    try:
        import litellm  # noqa: F401
    except ImportError:
        parser.error("Install litellm in this Python environment first.")

    for run_number in range(1, args.runs + 1):
        directory = project / f"benchmark_{run_number}_{uuid.uuid4().hex[:10]}"
        directory.mkdir()
        calls = []
        current_target = None

        def generator(model, messages):
            nonlocal current_target
            payload = json.loads(messages[1]["content"])
            current_target = payload["target"]["name"]
            item = {"target": current_target, "stage": "model", "succeeded": False}
            started = perf_counter()
            try:
                answer = agent.generate(model, messages)
                item["succeeded"] = True
                return answer
            except Exception as error:
                item["error_type"] = type(error).__name__
                raise
            finally:
                item["seconds"] = perf_counter() - started
                calls.append(item)

        def checker(project, filename):
            item = {"target": current_target, "stage": "lean", "passed": False}
            started = perf_counter()
            try:
                valid, diagnostics = agent.lean_check(project, filename)
                item["passed"] = bool(valid)
                return valid, diagnostics
            finally:
                item["seconds"] = perf_counter() - started
                calls.append(item)

        entry = {"run": run_number, "directory": str(directory), "model": args.model,
                 "attempt_limit": args.attempts, "calls": calls}
        result["runs"].append(entry)
        started = perf_counter()
        try:
            records = agent.review(source, targets, project, directory, args.model,
                                   args.attempts, generator=generator, checker=checker)
            entry["results"] = []
            for row in records:
                model_calls = [c for c in calls if c["target"] == row["name"] and c["stage"] == "model"]
                lean_calls = [c for c in calls if c["target"] == row["name"] and c["stage"] == "lean"]
                entry["results"].append({
                    "name": row["name"], "status": row["status"],
                    "verified": row["verified"], "attempts": row["attempts"],
                    "model_seconds": sum(c["seconds"] for c in model_calls),
                    "lean_seconds": sum(c["seconds"] for c in lean_calls),
                    "model_calls": len(model_calls), "lean_calls": len(lean_calls),
                })
            entry["completed_review_loop"] = True
        finally:
            entry["review_wall_seconds"] = perf_counter() - started
            entry["model_seconds"] = sum(c["seconds"] for c in calls if c["stage"] == "model")
            entry["lean_seconds"] = sum(c["seconds"] for c in calls if c["stage"] == "lean")
            entry["other_seconds"] = max(0, entry["review_wall_seconds"] - entry["model_seconds"] - entry["lean_seconds"])
            result["formalization_measured"] = any(c["stage"] == "lean" for r in result["runs"] for c in r["calls"])
            save(args.output, result)
        print(f"Run {run_number}: {entry['review_wall_seconds']:.3f} s; results: {directory}")

    result["median_review_wall_seconds"] = statistics.median(r["review_wall_seconds"] for r in result["runs"])
    save(args.output, result)
    print(f"Metrics: {args.output.resolve()}")
    return 0 if all(row["verified"] for run in result["runs"] for row in run.get("results", [])) else 1


if __name__ == "__main__":
    sys.exit(main())
