"""Observed state: render digests, state records and ``prov status`` (docs/state.md).

prov knows an item's *desired* state: ``spec.toml`` and the committed ``rendered/`` tree.
What a host actually runs is *observed* state: a JSON record that a host agent (never prov)
writes to ``<state-dir>/<kind>/<name>.json``, usually a checkout of a separate state
repository. This module compares the two from local files only. It never runs a command,
never reads the network and never writes a file.

- ``render_digest`` is the identity of a render: sha256 over a ``sha256sum``-style manifest
  of every regular file in ``rendered/`` (``PRESERVE`` files included), so an agent can
  reproduce it with coreutils alone (docs/state.md gives the command).
- ``load_record`` reads and checks one record against ``RECORD_SCHEMA``
  (``schema/prov.state.v1alpha1.schema.json``).
- ``item_status`` derives an item's lifecycle (planned, active, stale, decommissioned, or
  invalid) and its sync state (in-sync, pending, drifted, unknown).

Nothing here is specific to a kind or a backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .render import tree_entries
from .schema import DRAFT, KIND_PATTERN, NAME_PATTERN, SCHEMA_BASE_URL, ConfigError
from .text import short_repr, show
from .util import FileTooLargeError, NotRegularFileError, read_regular_bytes
from .validate import ERROR, WARNING, Finding, schema_errors

DIGEST_PREFIX = "sha256:"
STATE_API_VERSION = "prov.state/v1alpha1"
STATE_SCHEMA_FILE = "prov.state.v1alpha1.schema.json"
MAX_RECORD_BYTES = 256 * 1024

# Lifecycle and sync values (docs/state.md); stable strings, part of the --json format.
PLANNED, ACTIVE, STALE, DECOMMISSIONED, INVALID = "planned", "active", "stale", "decommissioned", "invalid"
IN_SYNC, PENDING, DRIFTED, UNKNOWN, NO_SYNC = "in-sync", "pending", "drifted", "unknown", "-"
DRIFT_STATUSES = ("clean", "drifted", "unknown")

# A host clock a little ahead of the reader's is normal; further ahead is worth a warning.
FUTURE_SKEW = timedelta(minutes=5)

_RFC3339 = r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"
_REF = "^(" + KIND_PATTERN.strip("^$") + ")/" + NAME_PATTERN.strip("^$") + "$"

RECORD_SCHEMA: Dict[str, Any] = {
    "$schema": DRAFT,
    "$id": SCHEMA_BASE_URL + STATE_SCHEMA_FILE,
    "title": "prov state record v1alpha1",
    "description": "Observed state of one item, written by a host agent to <state-dir>/<kind>/<name>.json and read by prov status.",
    "type": "object",
    "additionalProperties": False,
    "required": ["api_version", "ref", "last_seen", "drift"],
    "properties": {
        "api_version": {"const": STATE_API_VERSION, "description": "Record format version; required."},
        "ref": {"type": "string", "pattern": _REF, "description": "The item this record observes, <kind>/<name>; equals the record's path."},
        "last_seen": {
            "type": "string",
            "pattern": _RFC3339,
            "description": "When the agent last reported, RFC 3339 with a Z or a numeric offset.",
        },
        "applied_rev": {
            "type": "string",
            "pattern": "^[0-9a-f]{7,64}$",
            "description": "Commit of the infrastructure repository the host last applied (lowercase hex).",
        },
        "render_digest": {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$",
            "description": "prov digest of the rendered/ tree the host last applied.",
        },
        "generation": {
            "type": "string",
            "maxLength": 128,
            "description": "Backend-specific generation of the running system (e.g. a NixOS system generation).",
        },
        "drift": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status"],
            "description": "Whether the running host still matches what it applied.",
            "properties": {
                "status": {"enum": list(DRIFT_STATUSES), "description": "clean, drifted or unknown (the agent cannot tell)."},
                "detail": {"type": "string", "maxLength": 4096, "description": "Free text for a human; never parsed."},
            },
        },
        "facts": {"type": "object", "description": "Backend-specific facts about the host (hardware, disks, network); free-form."},
        "agent_version": {"type": "string", "maxLength": 128, "description": "Version of the agent that wrote the record."},
    },
}


def record_schema_json() -> str:
    """The published ``schema/prov.state.v1alpha1.schema.json`` (``prov.schema.schema_json``)."""
    from .schema import schema_json

    return schema_json(RECORD_SCHEMA)


# ---------------------------------------------------------------- digest


def render_digest(rendered_dir: Path) -> Optional[str]:
    """``sha256:<hex>`` of ``rendered_dir``, or None when it does not exist (not rendered yet).

    The digest is sha256 over the manifest ``"<sha256 hex>  <path>\\n"`` (``sha256sum``'s
    format) of every regular file, ordered by the UTF-8 bytes of its relative POSIX path
    (``LC_ALL=C sort``). File modes are not part of it. A symlink, special file or unsafely
    named entry (``prov.render`` never writes one) is a ConfigError, as is a path holding a
    backslash, which ``sha256sum`` would escape.
    """
    if rendered_dir.is_symlink():
        raise ConfigError(f"{show(str(rendered_dir))} is a symlink; prov refuses to read through symlinks")
    if not rendered_dir.is_dir():
        return None
    files, others = tree_entries(rendered_dir)
    if others:
        rel = others[0].relative_to(rendered_dir).as_posix()
        raise ConfigError(f"{show(str(rendered_dir))}: symlink, special file or unsafely named file {show(rel)}; run prov render")
    entries = []
    for path in files:
        rel = path.relative_to(rendered_dir).as_posix()
        if "\\" in rel:
            raise ConfigError(f"{show(str(rendered_dir))}: file name {show(rel)} holds a backslash; it has no stable digest")
        entries.append((rel.encode("utf-8"), rel, hashlib.sha256(read_regular_bytes(path)).hexdigest()))
    manifest = "".join(f"{digest}  {rel}\n" for _, rel, digest in sorted(entries))
    return DIGEST_PREFIX + hashlib.sha256(manifest.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- time


def parse_time(text: str) -> datetime:
    """An RFC 3339 timestamp (``Z`` or a numeric offset) as an aware datetime; ValueError otherwise."""
    m = re.fullmatch(_RFC3339.strip("^$"), text)
    if m is None:
        raise ValueError(f"not an RFC 3339 timestamp: {short_repr(text)}")
    year, month, day, hour, minute, second, frac, zone = m.groups()
    micro = int((frac[1:] + "000000")[:6]) if frac else 0
    tz = timezone.utc
    if zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        tz = timezone(sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])))
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=tz)


def format_time(when: datetime) -> str:
    """UTC, seconds precision, ``Z``: the form prov prints."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta:
    """``<n>s|m|h|d`` (``72h``, ``3d``) with n > 0; ConfigError otherwise."""
    m = re.fullmatch(r"([0-9]{1,9})([smhd])", text)
    if m is None or int(m.group(1)) == 0:
        raise ConfigError(f"invalid duration {short_repr(text)}: expected a positive number and a unit s, m, h or d (e.g. 72h)")
    return timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2)])


# ---------------------------------------------------------------- records


def record_path(state_dir: Path, kind: str, name: str) -> Path:
    """``<state-dir>/<kind>/<name>.json``; ``kind`` and ``name`` are validated ref parts."""
    return state_dir / kind / f"{name}.json"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {short_repr(key)}")
        out[key] = value
    return out


def load_record(state_dir: Path, kind: str, name: str) -> Tuple[Optional[Dict[str, Any]], List[Finding]]:
    """(record or None, findings) for one item. No file is (None, []): the item has no record yet.

    The kind directory and the record must be real (no symlinks) and the record a regular
    file of at most MAX_RECORD_BYTES holding one JSON object that matches RECORD_SCHEMA, whose
    ``ref`` is the item's ref and whose ``last_seen`` is a real date. Every problem is an error
    finding at ``state``; the record is then None.
    """
    ref = f"{kind}/{name}"
    where = "state"
    kdir = state_dir / kind
    path = record_path(state_dir, kind, name)

    def bad(message: str) -> Tuple[None, List[Finding]]:
        return None, [Finding(ERROR, f"{show(str(path))}: {message}", where)]

    try:
        if os.path.lexists(kdir) and not stat.S_ISDIR(os.lstat(kdir).st_mode):
            return None, [Finding(ERROR, f"{show(str(kdir))} is not a directory (symlinks are refused)", where)]
        if not os.path.lexists(path):
            return None, []
        data = read_regular_bytes(path, MAX_RECORD_BYTES)
    except NotRegularFileError:
        return bad("not a regular file; symlinks and special files are refused")
    except IsADirectoryError:
        return bad("is a directory, expected a file")
    except FileTooLargeError:
        return bad(f"larger than {MAX_RECORD_BYTES} bytes")
    except OSError as exc:
        return bad(f"cannot read: {exc.strerror or exc}")
    try:
        record = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except UnicodeDecodeError:
        return bad("not valid UTF-8")
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        return bad(f"invalid JSON: {exc}")
    if not isinstance(record, dict):
        return bad("expected one JSON object")
    errors = schema_errors(record, RECORD_SCHEMA)
    if errors:
        return None, [Finding(ERROR, f"{show(str(path))}: {e}", where) for e in errors]
    if record["ref"] != ref:
        return bad(f"ref {short_repr(record['ref'])} does not match its path ({ref})")
    try:
        parse_time(record["last_seen"])
    except ValueError:  # matches the pattern but is not a date (month 13, hour 25 ...)
        return bad(f"last_seen {short_repr(record['last_seen'])} is not a valid date and time")
    return record, []


def orphan_findings(state_dir: Path, kinds_known: List[str], refs: List[str]) -> List[Finding]:
    """A warning per ``<state-dir>/<kind>/<name>.json`` of an installed kind whose item does not exist."""
    known = set(refs)
    out: List[Finding] = []
    for kind in sorted(kinds_known):
        kdir = state_dir / kind
        try:
            if not os.path.lexists(kdir) or not stat.S_ISDIR(os.lstat(kdir).st_mode):
                continue
            entries = sorted(kdir.iterdir())
        except OSError:
            continue
        for entry in entries:
            stem, dot, ext = entry.name.rpartition(".")
            if not dot or ext != "json" or not re.fullmatch(NAME_PATTERN.strip("^$"), stem):
                continue
            ref = f"{kind}/{stem}"
            if ref not in known:
                out.append(Finding(WARNING, f"state record {show(str(entry))} has no item; remove or archive it", ref))
    return out


# ---------------------------------------------------------------- status


@dataclass
class Status:
    """One row of ``prov status``."""

    ref: str
    backend: Optional[str]
    lifecycle: str
    sync: str
    desired_digest: Optional[str] = None
    decommissioned: bool = False
    record: Optional[Dict[str, Any]] = None
    findings: List[Finding] = field(default_factory=list)

    def document(self) -> Dict[str, Any]:
        """The ``--json`` form; ``record`` raw, as the agent wrote it."""
        return {
            "ref": self.ref,
            "backend": self.backend,
            "lifecycle": self.lifecycle,
            "sync": self.sync,
            "desired_digest": self.desired_digest,
            "decommissioned": self.decommissioned,
            "record": self.record,
        }


def lifecycle_of(record: Optional[Mapping[str, Any]], decommissioned: bool, now: datetime, stale_after: timedelta) -> str:
    """decommissioned (declared) wins; then planned (no record), stale (last_seen too old) or active."""
    if decommissioned:
        return DECOMMISSIONED
    if record is None:
        return PLANNED
    return STALE if now - parse_time(record["last_seen"]) > stale_after else ACTIVE


def sync_of(record: Optional[Mapping[str, Any]], desired: Optional[str]) -> str:
    """drifted (the host says so) wins; then pending (it applied another render), unknown or in-sync."""
    if record is None:
        return NO_SYNC
    drift = record["drift"]["status"]
    if drift == "drifted":
        return DRIFTED
    applied = record.get("render_digest")
    if applied is None or desired is None:
        return UNKNOWN
    if applied != desired:
        return PENDING
    return UNKNOWN if drift == "unknown" else IN_SYNC


def item_status(
    ref: str,
    spec: Optional[Mapping[str, Any]],
    spec_findings: List[Finding],
    rendered_dir: Path,
    state_dir: Optional[Path],
    now: datetime,
    stale_after: timedelta,
) -> Status:
    """The status of one item from its checked spec (None when invalid), ``rendered/`` and its record."""
    kind, _, name = ref.partition("/")
    if spec is None:
        errors = [f for f in spec_findings if f.level == ERROR] or [Finding(ERROR, "invalid spec.toml; run prov validate " + ref, "spec.toml")]
        return Status(ref, None, INVALID, NO_SYNC, findings=errors)
    findings: List[Finding] = []
    decommissioned = bool(spec.get("state", {}).get("decommissioned", False))
    try:
        desired = render_digest(rendered_dir)
    except ConfigError as exc:
        desired = None
        findings.append(Finding(ERROR, str(exc), "rendered"))
    record, record_findings = (None, []) if state_dir is None else load_record(state_dir, kind, name)
    findings += record_findings
    if record_findings:
        return Status(ref, spec["backend"], INVALID, NO_SYNC, desired, decommissioned, None, findings)
    lifecycle = lifecycle_of(record, decommissioned, now, stale_after)
    if record is not None:
        seen = parse_time(record["last_seen"])
        if seen - now > FUTURE_SKEW:
            findings.append(Finding(WARNING, f"last_seen {format_time(seen)} is in the future (clock skew on the host?)", "state"))
        if decommissioned and now - seen <= stale_after:
            findings.append(Finding(WARNING, "decommissioned, but its host still reports; stop the agent or reinstall the host", "state"))
    return Status(ref, spec["backend"], lifecycle, sync_of(record, desired), desired, decommissioned, record, findings)


def text_line(row: Status) -> str:
    """``<ref>\\t<lifecycle>\\t<sync>\\t<applied rev, 12 chars>\\t<last seen>``; every field is validated or prov's own."""
    rec = row.record or {}
    rev = rec.get("applied_rev", "-")[:12]
    seen = format_time(parse_time(rec["last_seen"])) if "last_seen" in rec else "-"
    return f"{row.ref}\t{row.lifecycle}\t{row.sync}\t{rev}\t{seen}"


def digest_or_reason(rendered_dir: Path) -> Tuple[Optional[str], str]:
    """(digest, "") or (None, why there is none): for ``prov plan``, which never fails on it."""
    try:
        digest = render_digest(rendered_dir)
    except ConfigError:
        return None, "rendered/ holds a symlink, special or unsafely named file; run prov render"
    return (digest, "") if digest is not None else (None, "not rendered yet")
