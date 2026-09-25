# pamcli

Logs into a PAM (privileged access management) portal and requests SSH
access to servers.
Every run starts fresh — nothing carries over from an earlier run, and
the password is never saved.

## Install

With Python 3.10+ and pipx installed, run this in a clone of this repository:

```bash
pipx install .
```

The first time it runs, pamcli downloads the Chromium browser it logs
in with.

## Set up

pamcli reads its settings from environment variables. On Linux and macOS,
add them to `~/.bashrc` or `~/.zshrc` so they're set in every terminal:

```bash
export PAM_URL='https://pam.example.com/login'                 # the portal's login page
export PAM_USERNAME='<username>'                               # optional: skips the prompt
export PAM_TIMEOUT='60'                                        # optional: seconds, for a slow VPN (default 30)
```

On Windows, set each one once with `setx` (for example,
`setx PAM_URL "https://pam.example.com/login"`) and open a
new terminal.

The password can also be given in `PAM_PASSWORD`, but it shouldn't be
saved in a file.

## Use

```bash
pamcli login                  # log in and keep the Chromium window open
pamcli req                    # log in, then request access (pamcli asks for IPs)
pamcli req -e UAT             # request every IP in the UAT environment file
pamcli req -i 10.0.0.1 -H 2   # request one IP for 2 hours
```

- pamcli asks for anything not set (username, password, TOTP code).
- It logs in through its own Chromium window, separate from the everyday
  browser.
- A mistyped TOTP code is asked for again, up to 3 tries.
- `req` only requests the access; it never starts the SSH session. If the
  portal opens a tab to launch it, the tab is closed.
- If an account already has an active request, it's reused: nothing new
  is requested, and the result shows how long it stays valid.

`req` is short for `request`. Its options:

| Option | What it does |
|---|---|
| `-e`, `--env NAME` | Request every IP in the environment file `NAME` |
| `-i`, `--ips IPs` | Request these IPs, comma-separated (not together with `-e`) |
| `-a`, `--account NAME` | PAM account to request, e.g. `svc.app01` |
| `-H`, `--hours N` | Session length, 1–12 hours |
| `-r`, `--reason TEXT` | Reason for the access request |

Add `-v` to any command for detailed logs. `pamcli -h` and `pamcli req -h`
print the full help.

## Environment files

An environment file lists the servers to request together. pamcli reads
them from this folder (or from `PAM_ENV_DIR`, if set):

| System | Folder |
|---|---|
| Linux | `~/.config/pamcli/environments/` |
| macOS | `~/Library/Application Support/pamcli/environments/` |
| Windows | `%APPDATA%\pamcli\environments\` |

To create the first one, copy the example from this repository into that
folder. On Linux:

```bash
mkdir -p ~/.config/pamcli/environments
cp src/pam_cli/environments/example.json ~/.config/pamcli/environments/UAT.json
```

The file looks like this:

```json
{
  "hours": 6,
  "reason": "UAT testing",
  "ips": [
    "198.51.100.10",
    { "ip": "198.51.100.11", "hours": 2, "reason": "Quick check", "account": "svc.app01" }
  ]
}
```

Replace the IPs with the target servers, then run `pamcli req -e UAT`.

- Top-level `hours`, `reason` and `account` apply to every IP. An IP can
  set its own, which always wins.
- Command-line options replace the top-level values.
- pamcli asks once per run for anything still unset (hours, reason).
- To keep several projects separate, name files like `UAT.abc.json` and
  use `-e UAT.abc`.

## Troubleshooting

- **Login stopped working after a portal update** ("Could not find the
  login form"): run `pamcli discover`. It writes the login page's
  fields to `login_page_fields.txt` and a screenshot to `login_page.png`.
  Update the matching labels in `src/pam_cli/config.py`, then reinstall
  with `pipx install --force .`.
