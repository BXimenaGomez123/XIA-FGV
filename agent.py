import argparse
from dataclasses import asdict, dataclass
import re
import subprocess
import json
import sys
from pathlib import Path


from prompt import SYSTEM_PROMPT
ENVIRONMENTS = r"definition|theorem|lemma|corollary|proposition|example"
TOKEN = re.compile(r"\\(begin|end)\{(" + ENVIRONMENTS + r"|proof)\}")
IDENTIFIER = r"[A-Za-z][A-Za-z0-9_]*"
ACCEPTED = {"defined", "proved", "disproved"}


@dataclass
class Target:
    kind: str
    name: str
    label: str
    source: str
    line: int
    uses: list[str]


def mask_comments(source):
    """Keep offsets/newlines so extracted source is byte-for-byte unchanged."""
    return re.sub(r"(?<!\\)%[^\n]*", lambda m: " " * len(m[0]), source)


def extract_targets(source):
    """Small scanner for common, non-nested theorem environments, not full TeX."""

    masked = mask_comments(source)
    document = re.search(r"\\begin\{document\}", masked)
    start = document.end() if document else 0
    end_document = re.search(r"\\end\{document\}", masked[start:])
    end = start + end_document.start() if end_document else len(source)
    stack, spans = [], []

    for match in TOKEN.finditer(masked, start, end):
        action, kind = match.groups()
        if action == "begin":
            if stack:
                raise ValueError("Nested mathematical/proof environments are unsupported.")
            stack.append((kind, match.start()))
        else:
            if not stack or stack[-1][0] != kind:
                raise ValueError(f"Unmatched environment ending: {kind}")
            _, left = stack.pop()
            spans.append((kind, left, match.end()))
    if stack:
        raise ValueError(f"Unclosed environment: {stack[-1][0]}")

    targets, names = [], set()
    for i, (kind, left, right) in enumerate(spans):
        if kind == "proof":
            continue
        # Include an immediately following proof, ignoring whitespace/comments.
        if i + 1 < len(spans):
            next_kind, next_left, next_right = spans[i + 1]
            if next_kind == "proof" and not masked[right:next_left].strip():
                right = next_right
        text = source[left:right]
        labels = re.findall(r"\\label\{([^}]+)\}", mask_comments(text))
        label = labels[0] if labels else f"{kind}_{len(targets) + 1}"
        name = re.sub(r"[^A-Za-z0-9_]", "_", label)
        if not name or not name[0].isalpha():
            name = "item_" + name
        if name in names:
            raise ValueError(f"Duplicate Lean target name after label conversion: {name}")
        names.add(name)
        uses = []
        for hint in re.findall(r"(?m)^\s*%\s*lea:[^\n]*", text):
            dependency = re.search(r"\buses=\{([^}]*)\}", hint)
            if dependency:
                uses.extend(x.strip() for x in dependency[1].split(",") if x.strip())
        targets.append(Target(kind, name, label, text, source.count("\n", 0, left) + 1, uses))
    if not targets:
        raise ValueError("No supported mathematical environments found in the document.")
    return targets


def generate(model, messages):
    # Lazy import lets parsing/help/offline tests run without an API or LiteLLM.
    from litellm import completion
    response = completion(model=model, messages=messages, timeout=120)
    text = response.choices[0].message.content
    if not text or not text.strip():
        raise RuntimeError("The model returned an empty response.")
    return text.strip()


def lean_check(project, filename):
    try:
        result = subprocess.run(
            ["lake", "env", "lean", filename], cwd=project,
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "Lean compilation exceeded 120 seconds."
    diagnostics = result.stdout + result.stderr
    incomplete = re.search(r"\b(sorry|admit)\b", diagnostics, re.IGNORECASE)
    return result.returncode == 0 and not incomplete, diagnostics


def lean_tokens(code):
    """Remove nested block comments, line comments and strings for basic checks."""
    clean, i, depth = [], 0, 0
    while i < len(code):
        if code.startswith("/-", i):
            depth += 1
            clean.append(" ")
            i += 2
        elif depth and code.startswith("-/", i):
            depth -= 1
            i += 2
        elif depth:
            clean.append("\n" if code[i] == "\n" else " ")
            i += 1
        elif code.startswith("--", i):
            end = code.find("\n", i)
            i = len(code) if end == -1 else end
        elif code[i] == '"':
            clean.append(" ")
            i += 1
            while i < len(code):
                if code[i] == "\\":
                    i += 2
                elif code[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
        else:
            clean.append(code[i])
            i += 1
    return "".join(clean)


def parse_response(raw, target):
    # Tolerate a provider adding one surrounding Markdown fence.
    raw = re.sub(r"\A```(?:json)?\s*\n([\s\S]*?)\n```\s*\Z", r"\1", raw)
    result = json.loads(raw)
    if not isinstance(result, dict) or set(result) != {"status", "code", "explanation", "source_issues"}:
        raise ValueError("Return exactly status, code, explanation, source_issues as JSON.")
    if result["status"] not in ACCEPTED | {"needs_review"}:
        raise ValueError("Invalid status.")
    if not isinstance(result["code"], str) or not isinstance(result["explanation"], str):
        raise ValueError("code and explanation must be strings.")
    if not isinstance(result["source_issues"], list) or not all(isinstance(x, str) for x in result["source_issues"]):
        raise ValueError("source_issues must be a list of strings.")
    status = result["status"]
    if status == "needs_review":
        if result["code"].strip():
            raise ValueError("needs_review must have empty code.")
        return result
    if (target.kind == "definition") != (status == "defined"):
        raise ValueError("Use defined for definitions; proved/disproved for assertions.")
    code = lean_tokens(result["code"])
    if re.search(r"\b(sorry|admit|axiom|sorryAx|native_decide)\b", code):
        raise ValueError("Unproved assumptions/placeholders or native_decide are forbidden.")
    if re.search(r"(?m)^\s*(import|namespace|section|end)\b", code):
        # Indented `end` can terminate constructs; top-level sections are unnecessary.
        raise ValueError("Return declarations only, without imports/namespaces/sections/end.")
    expected = target.name + "_negation" if status == "disproved" else target.name
    kinds = "def|abbrev|inductive|structure" if status == "defined" else "theorem|lemma"
    if not re.search(r"\b(?:" + kinds + r")\s+" + re.escape(expected) + r"\b", code):
        raise ValueError(f"Missing requested declaration: {expected}")
    if status == "disproved" and not re.search(
        r"\b(?:theorem|lemma)\s+" + re.escape(expected) + r"\s*:\s*(?:¬|Not\b)", code
    ):
        raise ValueError("A disproof must state the negation of the complete proposition, without added parameters.")
    return result


def write_report(directory, records):
    (directory / "report.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Revisión LaTeX → Lean", "",
             "La compilación comprueba el código Lean. La correspondencia con el LaTeX "
             "requiere revisión humana; los diagnósticos del compilador no demuestran falsedad.", ""]
    for row in records:
        lines += [f"## {row['name']} — {row['status']}", "",
                  f"Línea de origen: {row['line']}. Intentos: {row['attempts']}.", "",
                  row["explanation"], ""]
        for issue in row["source_issues"]:
            lines += [f"- {issue}"]
        if row["source_issues"]:
            lines.append("")
        if row.get("diagnostics"):
            lines += ["Diagnóstico del último intento:", "", "```text", row["diagnostics"], "```", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


def review(source, targets, project, directory, model, attempts, generator=generate, checker=lean_check):
    
    prefix = "import Mathlib\n\n"
    records, completed = [], set()
    trace = directory / "trace.jsonl"
    candidate = directory / "Candidate.lean"
    artifact = directory / "Formalization.lean"
    (directory / "source.tex").write_text(source, encoding="utf-8")
    
    for target in targets:
        row = {**asdict(target), "status": "needs_review", "attempts": 0,
               "explanation": "No se obtuvo una formalización verificada.",
               "source_issues": [], "diagnostics": "", "verified": False}
        missing = [x for x in target.uses if x not in completed]
        if missing:
            row.update(status="blocked", explanation="Dependencias no verificadas: " + ", ".join(missing))
        else:
            payload = {"document": source, "target": asdict(target),
                       "negation_name": target.name + "_negation",
                       "checked_prefix": prefix,
                       "previous_results": [{"name": x["name"], "status": x["status"]} for x in records]}
            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
            for attempt in range(1, attempts + 1):
                print(f"{target.name}: attempt {attempt}/{attempts}", file=sys.stderr)
                row["attempts"] = attempt
                event = {"target": target.name, "attempt": attempt, "messages": list(messages)}
                raw = None
                try:
                    raw = generator(model, messages)
                    event["response"] = raw
                    result = parse_response(raw, target)
                    row["source_issues"] = result["source_issues"]
                    row["explanation"] = result["explanation"]
                    if result["status"] == "needs_review":
                        event["result"] = "needs_review"
                        break
                    code = prefix + result["code"].strip() + "\n\n"
                    candidate.write_text(code, encoding="utf-8")
                    (directory / f"{target.name}_attempt_{attempt}.lean").write_text(code, encoding="utf-8")
                    valid, errors = checker(project, str(candidate.relative_to(project)))
                    row["diagnostics"] = errors
                    event.update(diagnostics=errors, compiler_passed=bool(valid))
                    if valid:
                        prefix = code
                        artifact.write_text(prefix, encoding="utf-8")
                        row.update(status=result["status"], verified=True)
                        # A refuted proposition is not available as a proved dependency.
                        if result["status"] != "disproved":
                            completed.update({target.label, target.name})
                        break
                    feedback = "Compilation failed. Correct the NEW code preserving the original claim and checked prefix.\n" + errors
                except (ValueError, RuntimeError) as error:
                    feedback = str(error)
                    event["error"] = feedback
                    row["diagnostics"] = feedback
                except Exception as error:
                    # Provider/OS failures keep partial results and do not consume more API calls.
                    row["diagnostics"] = f"{type(error).__name__}: {error}"
                    event["error"] = row["diagnostics"]
                    break
                finally:
                    with trace.open("a", encoding="utf-8") as log:
                        log.write(json.dumps(event, ensure_ascii=False) + "\n")
                if raw is not None:
                    messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": feedback})
            if not row["verified"] and row["diagnostics"]:
                row["explanation"] = "No se verificó este bloque. La explicación del modelo no constituye un resultado verificado. " + row["explanation"]
        records.append(row)
        write_report(directory, records)
        print(f"{target.name}: {row['status']}")
    return records


def main():
    parser = argparse.ArgumentParser(description="Review a LaTeX document in Lean.")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--file", type=Path, help="Read an unchanged UTF-8 .tex document.")
    inputs.add_argument("--latex", help="One inline definition (legacy mode).")
    parser.add_argument("--name", help="Output folder stem; required in --latex mode.")
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error("--attempts must be at least 1.")
    name = args.name or (args.file.stem if args.file else None)
    if not name or not re.fullmatch(IDENTIFIER, name):
        parser.error("Supply --name with a simple identifier (required for --latex).")
    project = args.project.expanduser().resolve()
    if not (project / "lean-toolchain").is_file():
        parser.error("--project must point to an existing Lean project with Mathlib.")
    if args.file:
        # No LaTeX normalization/repairs; preserve the supplied source.
        source = args.file.expanduser().read_bytes().decode("utf-8")
        targets = extract_targets(source)
    else:
        source = args.latex
        targets = [Target("definition", name, name, source, 1, [])]
    directory = project / f"{name}_review"
    if directory.exists():
        parser.error(f"Output already exists: {directory}. Choose a different --name.")
    directory.mkdir()
    records = review(source, targets, project, directory, args.model, args.attempts)
    print(f"Review saved in: {directory}")
    return 0 if all(x["status"] in ACCEPTED for x in records) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)