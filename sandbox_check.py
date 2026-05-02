#!/usr/bin/env python3
"""Sandbox validation script.

Executed inside the bwrap sandbox by safe-claude.py. Never run standalone —
configuration is passed through environment variables set by the wrapper:
  SANDBOX_CHECK_HOME  path of $HOME inside the sandbox
  SANDBOX_CHECK_VIS   JSON list of [rel_path, expected_visible] pairs
  SANDBOX_CHECK_SENS  JSON list of sensitive env-var names to probe
"""
import json
import os
import pathlib
import socket
import subprocess

# ── configuration ──────────────────────────────────────────────────────────────
_HOME = os.environ.get("SANDBOX_CHECK_HOME", "/")
_VISIBILITY = json.loads(os.environ.get("SANDBOX_CHECK_VIS", "[]"))
_SENSITIVE_VARS = json.loads(os.environ.get("SANDBOX_CHECK_SENS", "[]"))

# ── output helpers ─────────────────────────────────────────────────────────────
_TTY = os.isatty(1)

_SUCCESS = True


def _c(code: str) -> str:
    return code if _TTY else ""


BOLD  = _c("\033[1m")
DIM   = _c("\033[2m")
RESET = _c("\033[0m")
GREEN = _c("\033[32m")
RED   = _c("\033[1;31m")
CYAN  = _c("\033[36m")


def _section(title: str) -> None:
    pad = "─" * max(0, 50 - len(title) - 4)
    print(f"\n{CYAN}{BOLD}── {title} ──{RESET}{DIM}{pad}{RESET}", flush=True)


def _info(text: str) -> None:
    print(f"  {DIM}{text}{RESET}", flush=True)


def _check(label: str, passed: bool, detail: str = "") -> None:
    if not passed:
        global _SUCCESS
        _SUCCESS = False
    mark  = f"{GREEN}✓{RESET}" if passed else f"{RED}✗{RESET}"
    tag   = "PASS" if passed else "FAIL"
    extra = f"  {DIM}[{detail}]{RESET}" if detail else ""
    print(f"  {mark} {tag}: {label}{extra}", flush=True)


# ── basic identity ─────────────────────────────────────────────────────────────
_section("basic identity")
_uid      = os.getuid()
_hostname = socket.gethostname()
_cwd      = os.getcwd()
try:
    _tty_name = os.ttyname(1)
except OSError:
    _tty_name = "none"
_info(f"uid={_uid}  gid={os.getgid()}  hostname={_hostname}  tty={_tty_name}")
_info(f"cwd={_cwd}")
_check("hostname is claude-safe", _hostname == "claude-safe", _hostname)
_check("not running as root", _uid != 0, f"uid={_uid}")

# ── environment ────────────────────────────────────────────────────────────────
_section("environment")
for _v in ("HOME", "PWD", "PATH", "TERM", "LANG",
           "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
    _val = os.environ.get(_v)
    if _val:
        _info(f"{_v}={_val}")
for _var in _SENSITIVE_VARS:
    _val = os.environ.get(_var)
    _check(f"{_var} not leaked", _val is None)

# ── filesystem visibility ──────────────────────────────────────────────────────
_section("filesystem visibility")
for _rel, _expected in _VISIBILITY:
    _path    = pathlib.Path(_HOME) / _rel
    _visible = _path.exists() or _path.is_symlink()
    if _expected:
        _check(f"{_path} visible", _visible)
    else:
        _check(f"{_path} hidden", not _visible)
_check("/etc/resolv.conf readable", os.access("/etc/resolv.conf", os.R_OK))
for _xdg_var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
    _xdg_dir = os.environ.get(_xdg_var)
    if not _xdg_dir:
        _check(f"{_xdg_var} writable", False, "not set")
        continue
    _probe = pathlib.Path(_xdg_dir) / ".write-probe"
    try:
        _probe.touch()
        _probe.unlink()
        _check(f"{_xdg_var} writable", True)
    except OSError as _e:
        _check(f"{_xdg_var} writable", False, str(_e))

# ── write isolation ────────────────────────────────────────────────────────────
_section("write isolation")
_probe_home = pathlib.Path(_HOME) / ".sandbox-write-probe"
try:
    _probe_home.touch()
    _probe_home.unlink()
    _check("HOME root write blocked", False)
except OSError:
    _check("HOME root write blocked", True)

_probe_project = pathlib.Path(_cwd) / ".sandbox-write-probe"
try:
    _probe_project.touch()
    _probe_project.unlink()
    _check("project write succeeded", True)
except OSError as _e:
    _check("project write succeeded", False, str(_e))

# ── dns + connectivity ─────────────────────────────────────────────────────────
_section("dns + connectivity")
try:
    _res = socket.getaddrinfo("api.anthropic.com", 443, type=socket.SOCK_STREAM)
    _ips = sorted({r[4][0] for r in _res})
    _info(f"api.anthropic.com → {', '.join(_ips)}")
except socket.gaierror as _e:
    _info(f"api.anthropic.com: {_e}  (expected when HTTP_PROXY is set)")
try:
    _r = subprocess.run(
        ["curl", "-sS", "-o", "/dev/null", "-w", "HTTP %{http_code}  %{time_total}s",
         "--max-time", "10", "https://api.anthropic.com"],
        capture_output=True, text=True, timeout=15,
    )
    _status = _r.stdout.strip() if _r.returncode == 0 else f"exit {_r.returncode}"
    _info(f"https://api.anthropic.com: {_status}")
    _check("https://api.anthropic.com is reachable", True)
except FileNotFoundError:
    _info("curl: not found")
    _check("https://api.anthropic.com is reachable", False)
except subprocess.TimeoutExpired:
    _info("https://api.anthropic.com: timed out")
    _check("https://api.anthropic.com is reachable", False)

exit(0 if _SUCCESS else 1)