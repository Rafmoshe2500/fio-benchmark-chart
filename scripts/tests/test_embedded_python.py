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


# A heuristic scan for unbalanced quotes was tried here and removed: it
# cannot tell a "#" inside a string from a comment, so it flagged
# f"deployment #{i+1}" as unterminated. compile() already detects exactly
# this defect without guessing, so the heuristic added false positives and
# no coverage.


if __name__ == "__main__":
    unittest.main()
