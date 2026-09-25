"""pam_cli.environments: env-file parsing and validation."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pam_cli import environments
from pam_cli.config import MAX_SESSION_HOURS
from pam_cli.environments import (
    IPTarget,
    _default_environments_dir,
    load_environment,
    parse_ip_list,
)


class LoadEnvironmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = patch.object(environments, "ENVIRONMENTS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, name, data):
        text = data if isinstance(data, str) else json.dumps(data)
        (self.dir / f"{name}.json").write_text(text)

    def test_parses_defaults_and_per_ip_overrides(self):
        self._write(
            "UAT",
            {
                "hours": 6,
                "reason": "default",
                "ips": [
                    "10.0.0.1",
                    {"ip": "10.0.0.2", "hours": MAX_SESSION_HOURS, "account": "db"},
                ],
            },
        )
        cfg = load_environment("UAT")
        self.assertEqual((cfg.hours, cfg.reason, cfg.account), (6, "default", None))
        self.assertEqual(
            cfg.targets,
            [
                IPTarget(ip="10.0.0.1"),
                IPTarget(ip="10.0.0.2", hours=MAX_SESSION_HOURS, account="db"),
            ],
        )

    def test_dotted_name_selects_project_file(self):
        self._write("UAT.abc", {"ips": ["10.0.0.1"]})
        self.assertEqual(load_environment("UAT.abc").targets, [IPTarget(ip="10.0.0.1")])
        with self.assertRaisesRegex(RuntimeError, "UAT.xyz.json"):
            load_environment("UAT.xyz")

    def test_invalid_files_raise_clear_errors(self):
        cases = {
            "not valid JSON": "{not json",
            "JSON object": [1, 2],
            'non-empty "ips"': {"ips": []},
            'non-empty string "ip"': {"ips": [{"hours": 1}]},
            "string or an object": {"ips": [123]},
            '"hours" must be an integer': {"ips": ["10.0.0.1"], "hours": "six"},
            f"between 1 and {MAX_SESSION_HOURS}": {"ips": ["10.0.0.1"], "hours": 0},
            '"reason" must be a string': {"ips": [{"ip": "10.0.0.1", "reason": 1}]},
            '"account" must not be blank': {
                "ips": [{"ip": "10.0.0.1", "account": " "}]
            },
            '"reason" must not be blank': {"ips": ["10.0.0.1"], "reason": ""},
            # Typos are errors, not silently ignored.
            "unknown key\\(s\\) 'hour'": {"ips": ["10.0.0.1"], "hour": 2},
            "unknown key\\(s\\) 'acount'": {
                "ips": [{"ip": "10.0.0.1", "acount": "db"}]
            },
            # Partial IPs and host names would let the filter match others.
            "'10.0.0' is not an IPv4 address": {"ips": ["10.0.0"]},
            "'db-host' is not an IPv4 address": {"ips": [{"ip": "db-host"}]},
        }
        for message, data in cases.items():
            with self.subTest(message):
                self._write("BAD", data)
                with self.assertRaisesRegex(RuntimeError, message):
                    load_environment("BAD")

    def test_missing_file_lists_what_exists_and_points_at_example(self):
        with self.assertRaisesRegex(
            RuntimeError, "not found.*No environment files there yet.*example.json"
        ):
            load_environment("NOPE")
        self._write("UAT", {"ips": ["x"]})
        self._write("PROD", {"ips": ["x"]})
        with self.assertRaisesRegex(RuntimeError, "Available: PROD, UAT\\."):
            load_environment("NOPE")

    def test_rejects_names_that_could_escape_the_dir(self):
        for name in (
            "",
            ".",
            "..",
            ".abc",
            "UAT.",
            "a..b",
            "../etc",
            "a/b",
            "a\\b",
            "C:x",
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(RuntimeError, "Invalid environment name"),
            ):
                load_environment(name)

    def test_shipped_example_is_valid(self):
        # The README tells every new user to copy this file.
        examples = Path(environments.__file__).parent / "environments"
        with patch.object(environments, "ENVIRONMENTS_DIR", examples):
            cfg = load_environment("example")
        self.assertTrue(cfg.targets)
        self.assertTrue(all(target.ip for target in cfg.targets))
        # It demonstrates both defaults and a per-IP override.
        self.assertIsNotNone(cfg.hours)
        self.assertTrue(any(target.hours is not None for target in cfg.targets))

    def test_unreadable_file_raises_clear_error(self):
        (self.dir / "DIR.json").mkdir()  # exists, but can't be read as a file
        with self.assertRaisesRegex(RuntimeError, "Could not read"):
            load_environment("DIR")


class ParseIpListTests(unittest.TestCase):
    def test_strips_whitespace_and_drops_empties_and_repeats(self):
        self.assertEqual(
            parse_ip_list(" 10.0.0.1 , , 10.0.0.2, 10.0.0.1,"),
            [IPTarget(ip="10.0.0.1"), IPTarget(ip="10.0.0.2")],
        )

    def test_rejects_anything_but_a_full_ipv4_address(self):
        for raw in ("10.0.0", "10.0.0.1,db-host", "10.0.0.256", "::1", "010.0.0.1"):
            with (
                self.subTest(raw=raw),
                self.assertRaisesRegex(ValueError, "is not an IPv4 address"),
            ):
                parse_ip_list(raw)


class DefaultEnvironmentsDirTests(unittest.TestCase):
    def test_resolution_order(self):
        with patch.dict(os.environ, {"PAM_ENV_DIR": "/tmp/custom"}):
            self.assertEqual(_default_environments_dir(), Path("/tmp/custom"))
        with (
            patch.dict(os.environ, {"PAM_ENV_DIR": "", "XDG_CONFIG_HOME": "/tmp/xdg"}),
            patch("pam_cli.paths.sys.platform", "linux"),
        ):
            self.assertEqual(
                _default_environments_dir(), Path("/tmp/xdg/pamcli/environments")
            )


if __name__ == "__main__":
    unittest.main()
