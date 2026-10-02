from pathlib import Path
import subprocess
import unittest


class EditorUIBehaviorTests(unittest.TestCase):
    def test_browser_and_deepseek_functions(self):
        """Keep actual JS behavior in the existing test:plugin/npm check gate."""
        completed = subprocess.run(
            ["node", "--test", str(Path(__file__).with_name("editor_ui_behavior.test.mjs"))],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
