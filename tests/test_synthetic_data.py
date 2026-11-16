"""Examples and fixtures hold only documentation addresses, reserved domains and fake keys."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from conftest import EXAMPLES, REPO

from prov.backends import ubuntu_autoinstall as ubuntu

DOC_NETS = [
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
]
RESERVED_DOMAINS = ("example.com", "example.org", "example.net")
# Public project references allowed in docs and the schema; never host names.
PUBLIC_REFERENCES = (
    "json-schema.org",
    "raw.githubusercontent.com",
    "github.com",
    "ghcr.io",
    "openssh.com",  # part of the sk-* key type names
)
SCANNED = [EXAMPLES, REPO / "tests", REPO / "docs", REPO / "schema", REPO / "README.md", REPO / "CLAUDE.md", REPO / "CHANGELOG.md"]
SELF = Path(__file__).resolve()

IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")
DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|my|local|lan|internal|cloud)\b", re.I)
SSH_KEY = re.compile(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/=]+")


def _files():
    for base in SCANNED:
        for path in [base] if base.is_file() else sorted(base.rglob("*")):
            # This file holds deliberate real addresses to prove the detector works.
            if path.is_file() and "__pycache__" not in path.parts and path != SELF:
                yield path, path.read_text(encoding="utf-8")
    # The `prov init` starter is synthetic data too.
    yield REPO / "src/prov/backends/ubuntu_autoinstall.py", ubuntu.starter("demo")


def _non_doc_addresses(text):
    bad = []
    for token in IPV4.findall(text) + IPV6.findall(text):
        try:
            addr = ipaddress.ip_address(token)
        except ValueError:
            continue  # times, MACs, version strings
        if not any(addr in net for net in DOC_NETS):
            bad.append(token)
    return bad


def test_scanned_paths_exist():
    assert [p for p in SCANNED if not p.exists()] == []


def test_detector_catches_real_addresses():
    assert _non_doc_addresses("10.0.0.1 8.8.8.8 2a00:1450::1 fe80::1") == ["10.0.0.1", "8.8.8.8", "2a00:1450::1", "fe80::1"]
    assert _non_doc_addresses("192.0.2.1 2001:db8::1 02:00:00:00:00:01 12:30") == []


def test_only_documentation_addresses():
    offenders = {str(p.relative_to(REPO)): bad for p, text in _files() if (bad := _non_doc_addresses(text))}
    assert not offenders


def test_only_reserved_domains():
    offenders = {}
    for path, text in _files():
        bad = [d for d in DOMAIN.findall(text) if not d.lower().endswith(RESERVED_DOMAINS + PUBLIC_REFERENCES)]
        if bad:
            offenders[str(path.relative_to(REPO))] = bad
    assert not offenders


def test_ssh_keys_are_obviously_fake():
    texts = [text for _, text in _files()]
    keys = [k for text in texts for k in SSH_KEY.findall(text)]
    assert keys
    assert all("EXAMPLE" in k for k in keys), [k for k in keys if "EXAMPLE" not in k]
