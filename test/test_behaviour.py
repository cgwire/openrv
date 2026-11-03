"""
Compare the output of `behaviour_scenario.py` with the expected snapshot.

Run:    python -m unittest discover -s test
Needs:  pip install PySide6-Essentials

After an intended change of behaviour, read the difference, then update the
snapshot:    UPDATE_SNAPSHOT=1 python -m unittest discover -s test
"""

import difflib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENARIO = HERE / "behaviour_scenario.py"
EXPECTED = HERE / "behaviour.expected.txt"


def run_scenario():
    """
    Run the scenario in a temporary home and return its normalized output.
    """
    with tempfile.TemporaryDirectory() as home:
        result = subprocess.run(
            [sys.executable, "-B", "-u", str(SCENARIO)],
            env={
                **os.environ,
                "HOME": home,
                "XDG_CONFIG_HOME": home,
                "QT_QPA_PLATFORM": "offscreen",
                "PYTHONHASHSEED": "0",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    output = result.stdout.replace(home, "HOME")
    if result.returncode:
        raise AssertionError(f"The scenario failed:\n{output[-3000:]}")
    return output


# shortcut: Qt saves QSettings outside the temporary home on macOS and
# Windows, so the snapshot holds only on Linux. Add a settings path to the
# scenario when the test must run on these systems.
@unittest.skipUnless(sys.platform == "linux", "The snapshot needs Linux")
@unittest.skipUnless(
    importlib.util.find_spec("PySide6"), "The test needs PySide6"
)
class BehaviourTestCase(unittest.TestCase):
    def test_output_matches_snapshot(self):
        output = run_scenario()
        if os.environ.get("UPDATE_SNAPSHOT"):
            EXPECTED.write_text(output, encoding="utf-8")
        diff = difflib.unified_diff(
            EXPECTED.read_text(encoding="utf-8").splitlines(),
            output.splitlines(),
            "expected",
            "actual",
            lineterm="",
            n=2,
        )
        difference = "\n".join(list(diff)[:200])
        self.assertFalse(difference, f"\n{difference}")


if __name__ == "__main__":
    unittest.main()
