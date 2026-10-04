"""Catch generic local-network and personal-path leaks in tracked files."""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
HOME_PATHS = (
    re.compile(re.escape("/" + "Users" + "/") + r"[A-Za-z0-9._-]+/"),
    re.compile(re.escape("/" + "home" + "/") + r"[A-Za-z0-9._-]+/"),
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
    if any(pattern.search(line) for pattern in HOME_PATHS):
        kinds.append("home path")
    if any(not _allowed_email(match) for match in EMAIL.finditer(line)):
        kinds.append("email")
    if any(_private_ip(match) for match in IPV4.finditer(line)):
        kinds.append("private IPv4 address")
    if any(pattern.search(line) for pattern in TAILNET_HOSTS):
        kinds.append("tailnet hostname")
    return kinds


def test_generic_detectors_with_synthetic_examples() -> None:
    def address(*octets: int) -> str:
        return ".".join(str(octet) for octet in octets)

    private_home = "/" + "Users" + "/" + "sample" + "/Project"
    private_linux_home = "/" + "home" + "/" + "sample" + "/Project"
    assert "home path" in _line_kinds(private_home)
    assert "home path" in _line_kinds(private_linux_home)
    assert _line_kinds("/coding/v1/users/me/balance") == []

    local_fixture_email = "me" + "@" + "studio.local"
    assert _line_kinds(local_fixture_email) == []
    assert _line_kinds("person" + "@" + "example.test") == []
    assert "email" in _line_kinds("person" + "@" + "private.invalid")
    assert "email" in _line_kinds("other" + "@" + "stage11.ai")

    for candidate in (address(10, 2, 3, 4), address(172, 20, 0, 1),
                      address(192, 168, 1, 2), address(100, 100, 2, 3)):
        assert "private IPv4 address" in _line_kinds(candidate)
    assert _line_kinds(address(203, 0, 113, 2)) == []

    assert "tailnet hostname" in _line_kinds(".".join(("host-name", "ts", "net")))
    assert "tailnet hostname" in _line_kinds(
        ".".join(("device", "tailabcd", "ts", "net"))
    )


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
