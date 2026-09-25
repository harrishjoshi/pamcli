"""pam_cli.shell_rc: saving PAM_URL and PAM_USERNAME in the shell's start-up file."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pam_cli.shell_rc import offer_to_save, rc_path, save

URL = "https://pam.example.com/login"


class RcPathTests(unittest.TestCase):
    def test_default_shell_decides_the_file(self):
        home = Path.home()
        for platform, env, expected in (
            ("linux", {"SHELL": "/bin/zsh"}, home / ".zshrc"),
            ("linux", {"SHELL": "/usr/bin/zsh", "ZDOTDIR": "/z"}, Path("/z/.zshrc")),
            ("linux", {"SHELL": "/bin/bash"}, home / ".bashrc"),
            # macOS terminals start login shells, which read ~/.bash_profile.
            ("darwin", {"SHELL": "/bin/bash"}, home / ".bash_profile"),
            ("darwin", {"SHELL": "/bin/zsh"}, home / ".zshrc"),
            ("linux", {"SHELL": "/usr/bin/fish"}, None),
            ("linux", {}, None),
            ("win32", {"SHELL": "/bin/bash"}, None),  # Windows uses setx
        ):
            with (
                self.subTest(platform=platform, env=env),
                patch("pam_cli.shell_rc.sys.platform", platform),
                patch.dict(os.environ, env, clear=True),
            ):
                self.assertEqual(rc_path(), expected)


class SaveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.rc = Path(tmp.name) / ".zshrc"

    def test_appends_and_never_changes_what_was_there(self):
        original = b'alias ll="ls -l"\nexport PATH="$HOME/bin:$PATH"\n'
        self.rc.write_bytes(original)
        save(self.rc, {"PAM_URL": URL, "PAM_USERNAME": "corp\\svc.app01"})
        text = self.rc.read_bytes()
        self.assertTrue(text.startswith(original))  # untouched, byte for byte
        self.assertEqual(
            text[len(original) :].decode(),
            "\n# pamcli settings\n"
            f"export PAM_URL={URL}\n"
            "export PAM_USERNAME='corp\\svc.app01'\n",  # quoted for the shell
        )

    def test_a_later_save_appends_again(self):
        save(self.rc, {"PAM_URL": URL})
        first = self.rc.read_bytes()
        save(self.rc, {"PAM_USERNAME": "alice"})
        text = self.rc.read_bytes()
        self.assertTrue(text.startswith(first))
        self.assertTrue(text.endswith(b"\nexport PAM_USERNAME=alice\n"))

    def test_a_file_without_a_final_newline_gets_one_first(self):
        self.rc.write_bytes(b"export A=1")
        save(self.rc, {"PAM_URL": URL})
        self.assertTrue(
            self.rc.read_bytes().startswith(b"export A=1\n\n# pamcli settings\n")
        )

    def test_creates_the_file_if_missing(self):
        save(self.rc, {"PAM_URL": URL})
        self.assertEqual(
            self.rc.read_text(), f"# pamcli settings\nexport PAM_URL={URL}\n"
        )


@patch("builtins.print")
class OfferToSaveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.rc = Path(tmp.name) / ".zshrc"
        for patcher in (
            patch("pam_cli.shell_rc.rc_path", return_value=self.rc),
            patch("pam_cli.shell_rc.sys.stdin", isatty=lambda: True),
            patch.dict(os.environ, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _offer(self, *answers):
        with patch("builtins.input", side_effect=answers) as asked:
            offer_to_save()
        return asked

    def test_not_asked_when_both_are_already_set(self, printed):
        # Set in this terminal, or saved in the file but not loaded yet.
        for env, file_text in (
            ({"PAM_URL": URL, "PAM_USERNAME": "alice"}, ""),
            ({}, f"export PAM_URL={URL}\nPAM_USERNAME=alice\n"),
            ({"PAM_URL": URL}, "export PAM_USERNAME=alice\n"),
        ):
            with self.subTest(env=env, file_text=file_text):
                self.rc.write_text(file_text)
                with patch.dict(os.environ, env):
                    self.assertEqual(self._offer().call_count, 0)
                self.assertEqual(self.rc.read_text(), file_text)  # untouched
        self.assertIn("already set", str(printed.call_args_list))

    def test_only_the_missing_setting_is_offered(self, _print):
        self.rc.write_text(f"export PAM_URL={URL}\n")
        asked = self._offer("y", "alice")
        self.assertEqual(
            asked.call_args_list[0].args[0].split(" in ")[0], "Save PAM_USERNAME"
        )
        self.assertEqual(asked.call_count, 2)  # no PAM_URL question
        text = self.rc.read_text()
        self.assertTrue(text.startswith(f"export PAM_URL={URL}\n"))  # untouched
        self.assertIn("export PAM_USERNAME=alice\n", text)

    def test_saying_no_changes_nothing(self, _print):
        for answer in ("", "n", "no"):
            with self.subTest(answer=answer):
                self.assertEqual(self._offer(answer).call_count, 1)
                self.assertFalse(self.rc.exists())

    def test_saying_yes_asks_and_saves_both(self, _print):
        # A URL that fails the checks is asked for again.
        asked = self._offer("y", "http://pam.example.com/login", URL, "alice")
        self.assertIn("Save PAM_URL and PAM_USERNAME", asked.call_args_list[0].args[0])
        self.assertEqual(asked.call_count, 4)
        text = self.rc.read_text()
        self.assertIn(f"export PAM_URL={URL}\n", text)
        self.assertIn("export PAM_USERNAME=alice\n", text)

    def test_url_can_be_skipped(self, printed):
        self._offer("y", "", "alice")
        text = self.rc.read_text()
        self.assertNotIn("PAM_URL", text)
        self.assertIn("export PAM_USERNAME=alice\n", text)
        self._offer("y", "", "")  # both skipped: nothing more is written
        self.assertEqual(self.rc.read_text(), text)
        self.assertIn("Nothing saved", str(printed.call_args_list))

    def test_username_can_be_skipped(self, _print):
        self._offer("yes", URL, "")
        self.assertNotIn("PAM_USERNAME", self.rc.read_text())

    def test_skipping_the_only_missing_setting_saves_nothing(self, printed):
        os.environ["PAM_URL"] = URL
        self._offer("y", "")
        self.assertFalse(self.rc.exists())
        self.assertIn("Nothing saved", str(printed.call_args_list))

    def test_no_terminal_or_eof_skips_it(self, _print):
        with patch("pam_cli.shell_rc.sys.stdin", isatty=lambda: False):
            self.assertEqual(self._offer().call_count, 0)
        self.assertEqual(self._offer(EOFError).call_count, 1)
        self.assertFalse(self.rc.exists())

    def test_other_shells_get_a_pointer_to_the_readme(self, printed):
        with patch("pam_cli.shell_rc.rc_path", return_value=None):
            self.assertEqual(self._offer().call_count, 0)
        self.assertIn("README", str(printed.call_args_list))

    def test_a_file_in_home_is_shown_as_tilde(self, printed):
        with patch("pam_cli.shell_rc.Path.home", return_value=self.rc.parent):
            asked = self._offer("y", URL, "")
        self.assertIn("in ~/.zshrc?", asked.call_args_list[0].args[0])
        self.assertIn("source ~/.zshrc", str(printed.call_args_list))

    def test_a_file_outside_home_is_shown_in_full(self, printed):
        self._offer("y", URL, "")
        self.assertIn(str(self.rc), str(printed.call_args_list))


if __name__ == "__main__":
    unittest.main()
