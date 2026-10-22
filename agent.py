import argparse
import re
import subprocess
import sys
from pathlib import Path

from litellm import completion


SYSTEM_PROMPT = """
Translate the supplied LaTeX function definition into Lean 4.

Requirements:
- Preserve its mathematical meaning, domain, and codomain.
- Define the function using the exact requested Lean name.
- Include `import Mathlib`.
- Return only a complete Lean source file, without Markdown fences.
- Do not use sorry, admit, axioms, or extra unproved assumptions.
- Do not add unrelated theorems or examples.
- If compiler errors are provided, return the complete corrected file.
"""


def generate(model, messages):
    response = completion(
        model=model,
        messages=messages,
        timeout=120,
    )
    code = response.choices[0].message.content
    if not code or not code.strip():
        raise RuntimeError("The model returned no Lean code.")
    return code.strip() + "\n"


def lean_check(project, filename):
    try:
        result = subprocess.run(
            ["lake", "env", "lean", filename],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "Lean compilation exceeded 120 seconds."

    diagnostics = result.stdout + result.stderr

    # Lean can accept incomplete proofs while reporting warnings.
    incomplete = re.search(
        r"\b(sorry|admit)\b", diagnostics, re.IGNORECASE
    )
    return result.returncode == 0 and not incomplete, diagnostics


def main():
    parser = argparse.ArgumentParser(
        description="Formalize a LaTeX function in Lean."
    )
    parser.add_argument("--latex", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", args.name):
        parser.error("--name must be a simple Lean identifier.")
    if args.attempts < 1:
        parser.error("--attempts must be at least 1.")

    project = args.project.expanduser().resolve()
    if not (project / "lean-toolchain").is_file():
        parser.error("--project must point to an existing Lean project.")

    output = project / f"{args.name}.lean"
    if output.exists():
        parser.error(f"Output already exists: {output}")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Lean declaration name: {args.name}\n\n"
                f"LaTeX function definition:\n{args.latex}"
            ),
        },
    ]

    for attempt in range(1, args.attempts + 1):
        print(f"Attempt {attempt}/{args.attempts}", file=sys.stderr)
        code = generate(args.model, messages)
        output.write_text(code, encoding="utf-8")

        valid, errors = lean_check(project, output.name)

        if valid:
            print(f"Lean compilation passed: {output}")
            return 0

        print(errors, file=sys.stderr)
        messages.extend([
            {"role": "assistant", "content": code},
            {
                "role": "user",
                "content": (
                    "Compilation failed. Correct the file while preserving "
                    f"the original function.\n\n{errors}"
                ),
            },
        ])

    print(
        f"Failed after {args.attempts} attempts. "
        f"Last candidate: {output}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)