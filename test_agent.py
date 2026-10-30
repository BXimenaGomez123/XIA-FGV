import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent


def response(status, code, explanation="Explicación de prueba."):
    return json.dumps(dict(status=status, code=code, explanation=explanation, source_issues=[]))


class ParserTests(unittest.TestCase):
    def test_exact_source_and_associated_proof(self):
        source = r"""\begin{document}
% \begin{theorem}fake\end{theorem}
\begin{definition}[With human typo]\label{def:f}
% lea: define label=f context={Keep typo.}
\operatorname{f (0)=1
\end{definition}
\begin{lemma}\label{f_zero}
% lea: formalize uses={def:f}
f(0)=1
\end{lemma}
% between proof and statement
\begin{proof}By definition.\end{proof}
\end{document}"""
        targets = agent.extract_targets(source)
        self.assertEqual([t.name for t in targets], ["def_f", "f_zero"])
        self.assertIn(r"\operatorname{f (0)=1", targets[0].source)
        self.assertEqual(targets[1].uses, ["def:f"])
        self.assertTrue(targets[1].source.endswith(r"\end{proof}"))
        for target in targets:
            self.assertIn(target.source, source)

    def test_rejects_duplicate_names_and_broken_environments(self):
        for source in [r"\begin{lemma}x", r"\end{lemma}",
                       r"\begin{lemma}\begin{theorem}x\end{theorem}\end{lemma}",
                       r"\begin{lemma}\label{a-b}\end{lemma}\begin{lemma}\label{a:b}\end{lemma}"]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                agent.extract_targets(source)

    def test_response_checks(self):
        t = agent.Target("theorem", "claim", "claim", "", 1, [])
        for raw in [response("proved", "axiom claim : False"),
                    response("proved", "theorem claim : True := by sorry"),
                    response("disproved", "theorem claim_negation : True := by trivial")]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                agent.parse_response(raw, t)
        good = response("disproved", "theorem claim_negation : ¬ False := by simp")
        self.assertEqual(agent.parse_response(good, t)["status"], "disproved")


class ReviewTests(unittest.TestCase):
    def run_review(self, targets, responses, verdicts):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        project = Path(temp.name)
        directory = project / "demo_review"
        directory.mkdir()
        generator, checker = iter(responses), iter(verdicts)
        seen = []

        def generate(model, messages):
            return next(generator)

        def check(project, filename):
            seen.append((project / filename).read_text())
            return next(checker)

        rows = agent.review("source with errors", targets, project, directory,
                            "fake/model", 2, generate, check)
        return rows, directory, seen

    def test_retry_and_only_checked_prefix_is_accumulated(self):
        targets = [agent.Target("definition", "f", "f", "typo", 1, []),
                   agent.Target("lemma", "f_zero", "f_zero", "", 5, ["f"])]
        rows, directory, seen = self.run_review(targets, [
            response("defined", "def f : Nat := broken"),
            response("defined", "def f : Nat := 1"),
            response("proved", "theorem f_zero : f = 1 := by rfl"),
        ], [(False, "error: unknown identifier broken"), (True, ""), (True, "")])
        self.assertEqual([r["status"] for r in rows], ["defined", "proved"])
        self.assertEqual(rows[0]["attempts"], 2)
        final = (directory / "Formalization.lean").read_text()
        self.assertNotIn("broken", final)
        self.assertIn("def f", seen[-1])
        events = [json.loads(x) for x in (directory / "trace.jsonl").read_text().splitlines()]
        self.assertEqual(len(events), 3)
        self.assertIn("error: unknown", events[1]["messages"][-1]["content"])
        self.assertEqual((directory / "source.tex").read_text(), "source with errors")

    def test_false_claim_needs_checked_negation(self):
        t = agent.Target("theorem", "every_day_weekend", "every_day_weekend", "", 1, [])
        rows, directory, _ = self.run_review([t], [response("disproved",
            "theorem every_day_weekend_negation : ¬ (False) := by simp")], [(True, "")])
        self.assertEqual(rows[0]["status"], "disproved")
        self.assertTrue(rows[0]["verified"])
        self.assertIn("every_day_weekend_negation", (directory / "Formalization.lean").read_text())
        # This test validates host state handling, not correspondence to the Day theorem.

    def test_compiler_failure_is_never_a_disproof(self):
        t = agent.Target("theorem", "claim", "claim", "", 1, [])
        rows, directory, _ = self.run_review([t], [response("disproved",
            "theorem claim_negation : ¬ True := by simp")] * 2,
            [(False, "error: unsolved goals")] * 2)
        self.assertEqual(rows[0]["status"], "needs_review")
        self.assertFalse(rows[0]["verified"])
        self.assertFalse((directory / "Formalization.lean").exists())

    def test_unavailable_dependency_blocks_without_api(self):
        targets = [agent.Target("definition", "f", "f", "", 1, []),
                   agent.Target("lemma", "property", "property", "", 3, ["f"])]
        rows, _, _ = self.run_review(targets, [response("needs_review", "")], [])
        self.assertEqual([r["status"] for r in rows], ["needs_review", "blocked"])
        self.assertEqual(rows[1]["attempts"], 0)

    def test_invalid_json_retries(self):
        t = agent.Target("definition", "f", "f", "", 1, [])
        rows, directory, _ = self.run_review([t], ["not json", response("defined", "def f := 1")], [(True, "")])
        self.assertEqual(rows[0]["attempts"], 2)
        self.assertTrue((directory / "trace.jsonl").exists())

    def test_provider_failure_preserves_report_and_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            directory = project / "review"
            directory.mkdir()
            def failing_provider(model, messages):
                raise PermissionError("provider unavailable")
            rows = agent.review("source", [agent.Target("definition", "f", "f", "", 1, [])],
                                project, directory, "fake/model", 2, failing_provider)
            self.assertEqual(rows[0]["status"], "needs_review")
            self.assertEqual(rows[0]["attempts"], 1)
            self.assertIn("provider unavailable", (directory / "report.json").read_text())
            self.assertIn("provider unavailable", (directory / "trace.jsonl").read_text())

    def test_sorry_warning_rejected_even_when_exit_code_zero(self):
        fake = type("Result", (), dict(returncode=0, stdout="warning: declaration uses 'sorry'", stderr=""))()
        with patch("agent.subprocess.run", return_value=fake):
            self.assertFalse(agent.lean_check(Path("."), "candidate.lean")[0])


if __name__ == "__main__":
    unittest.main()