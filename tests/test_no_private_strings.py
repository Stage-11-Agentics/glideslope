"""Catch generic local-network and personal-path leaks in tracked files."""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
HOME_PATHS = (
    re.compile(r"(?i)(?<!/coding/v1)(?<![\w.])/(?:users|home)/[A-Za-z0-9._-]+/"),
    re.compile(r"(?<![\w])~(?:[A-Za-z0-9._-]+)?/[A-Za-z0-9._-]{2,}(?=/|$|[\s`\"'<>|,;:!?)}\]])"),
)
# Generic installation examples name a product's conventional config directory,
# not an expanded path from one user's home.
PUBLIC_HOME_PATH_PREFIXES = (
    "~/.claude", "~/.claude-profiles", "~/.claude.json", "~/.codex",
    "~/.glideslope", "~/.grok", "~/.kimi-code", "~/.local", "~/.ssh",
    "~/.zshrc",
)
EMAIL = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])(?P<local>[A-Z0-9._%+-]+)@"
    r"(?P<domain>[A-Z0-9.-]+\.[A-Z]{2,})(?![A-Z0-9.-])"
)
RESERVED_DOMAINS = {"example.com", "example.org", "example.net"}
LOCAL_EMAIL_EXCEPTIONS = {
    "me" + "@" + "studio.local": "local-only identity used by fixture material"
}
STAGE11_CONTACT = "hello" + "@" + "stage11.ai"
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
TAILNET_HOSTS = (
    re.compile(r"(?i)\b[a-z0-9-]+\.ts\.net\b"),
    re.compile(r"(?i)\b[a-z0-9-]+\.tail[0-9a-f]+\.ts\.net\b"),
)


def _allowed_email(match: re.Match[str]) -> bool:
    address = match.group(0).lower()
    local = match.group("local").lower()
    domain = match.group("domain").lower()
    if domain in RESERVED_DOMAINS or domain.endswith((".example", ".test")):
        return True
    if local == "noreply" or domain == "users.noreply.github.com":
        return True
    if address == STAGE11_CONTACT.lower() or address in LOCAL_EMAIL_EXCEPTIONS:
        return True
    return False


def _private_ip(match: re.Match[str]) -> bool:
    try:
        address = ipaddress.ip_address(match.group(0))
    except ValueError:
        return False
    if not isinstance(address, ipaddress.IPv4Address):
        return False
    network_specs = (
        (("10", "0", "0", "0"), "8"),
        (("172", "16", "0", "0"), "12"),
        (("192", "168", "0", "0"), "16"),
        (("100", "64", "0", "0"), "10"),
    )
    networks = tuple(
        ipaddress.ip_network(".".join(octets) + "/" + mask)
        for octets, mask in network_specs
    )
    return any(address in network for network in networks)


def _line_kinds(line: str) -> list[str]:
    kinds: list[str] = []
    home_matches = [match for pattern in HOME_PATHS for match in pattern.finditer(line)]
    private_home = False
    for match in home_matches:
        if match.group(0).startswith("~"):
            candidate = re.match(r"~[^\s`\"'<>|]+", line[match.start():])
            token = candidate.group(0).rstrip(".,;:!?)]}") if candidate else ""
            if any(token == prefix or token.startswith(prefix + "/")
                   for prefix in PUBLIC_HOME_PATH_PREFIXES):
                continue
        private_home = True
        break
    if private_home:
        kinds.append("home path")
    if any(not _allowed_email(match) for match in EMAIL.finditer(line)):
        kinds.append("email")
    if any(_private_ip(match) for match in IPV4.finditer(line)):
        kinds.append("private IPv4 address")
    if any(pattern.search(line) for pattern in TAILNET_HOSTS):
        kinds.append("tailnet hostname")
    return kinds


def _address(*octets: int) -> str:
    return ".".join(str(octet) for octet in octets)


GENERIC_FIXTURES = (
    ("/" + "Users" + "/" + "sample" + "/Project", ("home path",)),
    ("/" + "home" + "/" + "sample" + "/Project", ("home path",)),
    ("/" + "USERS" + "/" + "sample" + "/Project", ("home path",)),
    ("~" + "/Projects/App", ("home path",)),
    ("~sample" + "/Projects/App", ("home path",)),
    ("~" + "/.glideslope/config.toml", ()),
    ("~" + "/.claude-profiles/sample/.claude.json", ()),
    ("~" + "/.claude.json", ()),
    ("/coding/v1/users/me/balance", ()),
    ("me" + "@" + "studio.local", ()),
    ("person" + "@" + "example.test", ()),
    ("person" + "@" + "private.invalid", ("email",)),
    ("other" + "@" + "stage11.ai", ("email",)),
    ("hello" + "@" + "stage11.ai", ()),
    ("agent" + "@" + "users.noreply.github.com", ()),
    ("noreply" + "@" + "private.invalid", ()),
    (_address(10, 2, 3, 4), ("private IPv4 address",)),
    (_address(172, 20, 0, 1), ("private IPv4 address",)),
    (_address(192, 168, 1, 2), ("private IPv4 address",)),
    (_address(100, 100, 2, 3), ("private IPv4 address",)),
    (_address(203, 0, 113, 2), ()),
    (".".join(("host-name", "ts", "net")), ("tailnet hostname",)),
    (".".join(("device", "tailabcd", "ts", "net")), ("tailnet hostname",)),
)


def test_generic_detectors_with_synthetic_examples() -> None:
    for line, expected in GENERIC_FIXTURES:
        assert _line_kinds(line) == list(expected), line


def test_tracked_files_have_no_private_machine_or_contact_strings() -> None:
    result = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("not inside a Git checkout; tracked-file privacy scan is unavailable")

    paths = [REPO / path.decode("utf-8", "surrogateescape") for path in result.stdout.split(b"\0") if path]
    findings: list[str] = []
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(REPO).as_posix()
        for number, line in enumerate(lines, 1):
            kinds = _line_kinds(line)
            if kinds:
                findings.append(f"{relative}:{number}: {', '.join(kinds)}")
    if findings:
        pytest.fail("tracked files contain private-looking strings:\n" + "\n".join(findings[:80]))
