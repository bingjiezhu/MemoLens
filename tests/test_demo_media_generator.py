from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = PROJECT_ROOT / "scripts" / "create_demo_library.py"


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class DemoMediaGeneratorTests(unittest.TestCase):
    def test_generates_images_landscape_audio_and_silent_vertical_video(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-demo-media-") as temp_dir:
            output = Path(temp_dir) / "library"
            completed = subprocess.run(
                [sys.executable, str(GENERATOR), "--output", str(output)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(len(list(output.glob("*.jpg"))), 12)
            self.assertEqual(len(list(output.glob("*.mp4"))), 2)
            self.assertEqual(list(output.glob(".*.part.mp4")), [])

            landscape = self._probe(output / "demo_mountain_to_coast.mp4")
            vertical = self._probe(output / "demo_vertical_city_story.mp4")
            self.assertEqual((landscape["width"], landscape["height"]), (1280, 720))
            self.assertEqual((vertical["width"], vertical["height"]), (720, 1280))
            self.assertTrue(landscape["has_audio"])
            self.assertFalse(vertical["has_audio"])
            self.assertAlmostEqual(landscape["duration"], 9.0, delta=1 / 24)
            self.assertAlmostEqual(vertical["duration"], 9.0, delta=1 / 24)

            refused = subprocess.run(
                [sys.executable, str(GENERATOR), "--output", str(output)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertIn("Refusing to overwrite", refused.stdout)

    @staticmethod
    def _probe(path: Path) -> dict[str, object]:
        completed = subprocess.run(
            [
                shutil.which("ffprobe") or "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        payload = json.loads(completed.stdout)
        streams = payload["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        return {
            "width": video["width"],
            "height": video["height"],
            "has_audio": any(stream["codec_type"] == "audio" for stream in streams),
            "duration": float(payload["format"]["duration"]),
        }


if __name__ == "__main__":
    unittest.main()
