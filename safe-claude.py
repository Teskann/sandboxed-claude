#!/usr/bin/env python3
"""Bubblewrap-based sandbox wrapper for the `claude` CLI."""

import argparse
import json
import os
import shutil
import subprocess
import sys


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


_COLOR = _supports_color()


def _c(code: str) -> str:
    return code if _COLOR else ""


BOLD = _c("\033[1m")
DIM = _c("\033[2m")
CYAN = _c("\033[36m")
GREEN = _c("\033[32m")
RED = _c("\033[31m")
RESET = _c("\033[0m")


def banner() -> None:
    print(f"{BOLD}{CYAN}▌ safe-claude{RESET} {DIM}(sandboxed claude){RESET}", file=sys.stderr, flush=True)


def info(msg: str) -> None:
    print(f"{DIM}→ {msg}{RESET}", file=sys.stderr, flush=True)


def success(msg: str) -> None:
    print(f"{GREEN}✓ {msg}{RESET}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(f"{RED}{BOLD}✗ {msg}{RESET}", file=sys.stderr, flush=True)


def fail_and_exit(msg: str, code: int = 1) -> None:
    error(msg)
    sys.exit(code)


def resolve_claude_bin() -> str:
    found = shutil.which("claude")
    if not found:
        home = os.environ.get("HOME") or os.path.expanduser("~")
        for candidate in [
            os.path.join(home, ".local/bin/claude"),
            "/usr/local/bin/claude",
        ]:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    if not found:
        fail_and_exit("Claude binary not found in PATH")
    real = os.path.realpath(found)
    if not os.access(real, os.X_OK):
        fail_and_exit(f"Claude binary is not executable: {real}")
    return real


def ensure_claude_json(home: str) -> str:
    path = os.path.join(home, ".claude.json")
    if not os.path.exists(path):
        with open(path, "w"):
            pass
        os.chmod(path, 0o600)
    return path


def resolv_target() -> str:
    try:
        return os.path.realpath("/etc/resolv.conf")
    except OSError:
        return "/etc/resolv.conf"


def build_bwrap_argv(project: str, home: str, claude_json: str) -> list:
    home_parent = os.path.dirname(home) or "/"
    argv = [
        "/usr/bin/bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/usr/local", "/usr/local",
        "--ro-bind", "/etc", "/etc",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--dir", home_parent,
        "--perms", "0555", "--dir", home,
        "--proc", "/proc",
        "--dev", "/dev",
        "--dev-bind", "/dev/pts", "/dev/pts",
        "--tmpfs", "/tmp",
        "--dir", "/run",
        "--perms", "0700", "--dir", "/tmp/.config",
        "--perms", "0700", "--dir", "/tmp/.cache",
        "--dir", "/tmp/.local",
        "--perms", "0700", "--dir", "/tmp/.local/share",
        "--perms", "0700", "--dir", "/tmp/.local/state",
        "--clearenv",
        "--setenv", "HOME", home,
        "--setenv", "PWD", project,
        "--setenv", "PATH", f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "--setenv", "TERM", os.environ.get("TERM", "xterm-256color"),
        "--setenv", "LANG", os.environ.get("LANG", "C.UTF-8"),
        "--setenv", "XDG_CONFIG_HOME", "/tmp/.config",
        "--setenv", "XDG_CACHE_HOME", "/tmp/.cache",
        "--setenv", "XDG_DATA_HOME", "/tmp/.local/share",
        "--setenv", "XDG_STATE_HOME", "/tmp/.local/state",
        "--setenv", "DISABLE_AUTOUPDATER", "1",
        "--bind", os.path.join(home, ".claude"), os.path.join(home, ".claude"),
        "--bind", project, project,
        "--chdir", project,
        "--unshare-pid",
        "--unshare-uts",
        "--hostname", "claude-safe",
        "--unshare-ipc",
        "--new-session",
        "--die-with-parent",
        "--unshare-user",
        "--bind", claude_json, claude_json,
    ]

    local_bin = os.path.join(home, ".local/bin")
    if os.path.isdir(local_bin):
        argv += ["--ro-bind", local_bin, local_bin]
    local_share_claude = os.path.join(home, ".local/share/claude")
    if os.path.isdir(local_share_claude):
        argv += ["--ro-bind", local_share_claude, local_share_claude]

    passthrough = (
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
        "ANTHROPIC_API_KEY",
    )
    for var in passthrough:
        val = os.environ.get(var)
        if val:
            argv += ["--setenv", var, val]

    rt = resolv_target()
    if rt.startswith("/run/systemd/resolve/"):
        argv += [
            "--dir", "/run/systemd",
            "--dir", "/run/systemd/resolve",
            "--ro-bind", rt, rt,
        ]

    return argv


# Paths under $HOME and whether each is expected to be visible inside the sandbox.
VISIBILITY_EXPECTATIONS = [
    (".claude", True),
    (".claude.json", True),
    (".ssh", False),
    (".aws", False),
    (".gnupg", False),
    ("Downloads", False),
    ("Documents", False),
]

# Sensitive env vars that must not leak into the sandbox.
_SENSITIVE_VARS = [
    "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CLIENT_SECRET",
]

_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def build_check_script() -> str:
    path = os.path.join(_SCRIPT_DIR, "sandbox_check.py")
    with open(path) as f:
        return f.read()


def run_check(bwrap_argv: list, home: str, *, verbose: bool):
    """Run the validation routine inside the sandbox.

    With verbose=True, output streams live and the captured strings are empty.
    A run is considered passed iff exit code is 0 AND no stdout line begins
    with `FAIL:` (the script reports soft failures that way without exiting).
    """
    script = build_check_script()
    cmd = bwrap_argv + [
        "--setenv", "SANDBOX_CHECK_HOME", home,
        "--setenv", "SANDBOX_CHECK_VIS", json.dumps(VISIBILITY_EXPECTATIONS),
        "--setenv", "SANDBOX_CHECK_SENS", json.dumps(_SENSITIVE_VARS),
        "/usr/bin/python3", "-",
    ]
    result = subprocess.run(cmd, input=script, text=True, capture_output=not verbose)
    if result.returncode != 0 and not verbose:
        print(result.stdout, file=sys.stderr, flush=True)
    if result.returncode != 0:
        error("Sandbox check failed. Read above for details.")
    return result.returncode == 0

def parse_args(argv: list):
    """Split argv into (project, mode, inner_cmd, extras, detected_claude_bin).

    argparse alone is awkward for the `[PROJECT] -- CMD ...` shape, so we
    pre-split on `--` and let argparse handle the left side.
    """
    if "--" in argv:
        idx = argv.index("--")
        left, inner = argv[:idx], argv[idx + 1:]
    else:
        left, inner = argv, None

    parser = argparse.ArgumentParser(
        prog="safe-claude",
        description="Bubblewrap-based sandbox wrapper for the `claude` CLI.",
        epilog=(
            "Any arguments not listed above are passed to claude.\n\n"
            "Examples:\n"
            "  safe-claude                       launch claude in the sandbox\n"
            "  safe-claude --check               run the validation routine and exit\n"
            "  safe-claude -- whoami             run an arbitrary command in the sandbox\n"
            "\n"
            "When launching claude, the sandbox check is run first; if it fails, "
            "claude is not started."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check", action="store_true",
        help="run only the sandbox validation routine and exit",
    )
    args, extras = parser.parse_known_args(left)

    if args.check and inner is not None:
        parser.error("--check cannot be combined with `-- CMD`")
    if args.check and extras:
        parser.error("--check cannot be combined with extra arguments")
    if inner is not None and len(inner) == 0:
        parser.error("no command provided after `--`")

    if args.check:
        mode = "check"
    elif inner is not None:
        mode = "cmd"
    else:
        mode = "claude"

    return mode, inner or [], extras


def main():
    mode, inner_cmd, claude_extras = parse_args(sys.argv[1:])

    # Interactive: explicit check/cmd invocations, or a bare claude launch with no extras.
    interactive = mode in ("check", "cmd") or not claude_extras
    if interactive:
        banner()

    project = os.getcwd()
    home = os.environ.get("HOME") or os.path.expanduser("~")
    claude_json = ensure_claude_json(home)

    os.umask(0o077)
    bwrap_argv = build_bwrap_argv(project, home, claude_json)

    claude_bin = resolve_claude_bin()

    if mode == "check":
        info("Running sandbox check…")
        passed = run_check(bwrap_argv, home, verbose=True)
        if not passed:
            error("Sandbox check failed (see output above)")
            sys.exit(1)
        success("Sandbox check passed")
        return

    if mode == "cmd":
        os.execv(bwrap_argv[0], bwrap_argv + inner_cmd)
        return

    if interactive:
        info("Running sandbox check…")
    passed = run_check(bwrap_argv, home, verbose=interactive)
    if not passed:
        sys.exit(1)
    if interactive:
        success("Sandbox check passed")
        info("Launching claude")
    os.execv(bwrap_argv[0], bwrap_argv + [claude_bin] + claude_extras)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
