"""pam_cli.paths: per-platform config dir."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pam_cli import paths

HOME = Path("/home/u")


class ConfigDirTests(unittest.TestCase):
    def test_per_platform(self):
        cases = [
            ("linux", {}, HOME / ".config/pamcli"),
            ("linux", {"XDG_CONFIG_HOME": "/x/config"}, Path("/x/config/pamcli")),
            ("darwin", {}, HOME / "Library/Application Support/pamcli"),
            ("win32", {}, HOME / "AppData/Roaming/pamcli"),
            ("win32", {"APPDATA": "/w/Roaming"}, Path("/w/Roaming/pamcli")),
        ]
        for platform, env, expected in cases:
            clean = {
                k: v
                for k, v in os.environ.items()
                if k not in ("XDG_CONFIG_HOME", "APPDATA")
            }
            with (
                self.subTest(platform=platform, env=env),
                patch.dict(os.environ, {**clean, "HOME": str(HOME), **env}, clear=True),
                patch("pam_cli.paths.sys.platform", platform),
                patch.object(Path, "home", return_value=HOME),
            ):
                self.assertEqual(paths.config_dir(), expected)


if __name__ == "__main__":
    unittest.main()
