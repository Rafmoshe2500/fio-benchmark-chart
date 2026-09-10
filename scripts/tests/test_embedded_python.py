import glob
import io
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Heredoc bodies whose delimiter is PY / PYEOF are Python and must compile.
HEREDOC = re.compile(r"<<'(PY[A-Z]*)'\n(.*?)\n\1\n", re.S)


def _shell_scripts():
    for pattern in ("scripts/*.sh", "scripts/lib/*.sh", "nifi/*.sh"):
        for p in sorted(glob.glob(os.path.join(REPO, pattern))):
            yield p


class EmbeddedPythonTest(unittest.TestCase):
    """Python embedded in shell heredocs is never imported, so nothing else
    checks it. A run_suite.sh block once shipped with a literal newline inside
    a string literal: the snippet died with SyntaxError, the suite resolved to
    an empty test list, and the run reported '0 valid run(s)' as though that
    were an outcome rather than a crash.
    """

    def test_every_embedded_block_compiles(self):
        checked, failures = 0, []
        for path in _shell_scripts():
            text = io.open(path, encoding="utf-8").read()
            for m in HEREDOC.finditer(text):
                tag, body = m.group(1), m.group(2)
                line = text[:m.start()].count("\n") + 1
                checked += 1
                try:
                    compile(body, "%s:%d" % (path, line), "exec")
                except SyntaxError as e:
                    failures.append("%s (%s heredoc at line %d): line %s: %s"
                                    % (os.path.relpath(path, REPO), tag, line,
                                       e.lineno, e.msg))
        self.assertTrue(checked, "found no embedded Python to check")
        self.assertEqual(failures, [], "\n  " + "\n  ".join(failures))


class CarriageReturnTest(unittest.TestCase):
    """Every capture from python3 must be piped through nocr.

    A native Windows python3 writes CRLF, and $(...) strips only the trailing
    newline -- so every line but the last comes back with a \\r attached.
    `run_suite.sh characterise` resolved to 'low_qd_latency\\r' plus six more
    names that matched no file, and the failure reads as "no such fio job
    file", which points nowhere near line endings.
    """

    CAPTURE = re.compile(r"[$<]\(\s*python3\b")

    @staticmethod
    def _capture_text(text, start):
        """The whole substitution, not just its first line: several of these
        span a heredoc and the pipe lands well below the opening paren."""
        depth = 0
        for i in range(start + 1, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                if depth == 0:
                    return text[start:i]
                depth -= 1
        return text[start:]

    def test_python_captures_strip_carriage_returns(self):
        offenders = []
        # The fio path only. The nifi scripts are a separate tool with their
        # own entry points and are not part of a suite run.
        for path in [p for p in _shell_scripts()
                     if os.sep + "scripts" + os.sep in p]:
            text = io.open(path, encoding="utf-8").read()
            for m in self.CAPTURE.finditer(text):
                body = self._capture_text(text, m.start())
                if "nocr" in body:
                    continue
                line_no = text[:m.start()].count("\n") + 1
                offenders.append("%s:%d: %s"
                                 % (os.path.relpath(path, REPO), line_no,
                                    body.strip().splitlines()[0][:90]))
        self.assertEqual(offenders, [],
                         "capture(s) not piped through nocr:\n  "
                         + "\n  ".join(offenders))


# A heuristic scan for unbalanced quotes was tried here and removed: it
# cannot tell a "#" inside a string from a comment, so it flagged
# f"deployment #{i+1}" as unterminated. compile() already detects exactly
# this defect without guessing, so the heuristic added false positives and
# no coverage.


if __name__ == "__main__":
    unittest.main()
