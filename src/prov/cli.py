"""prov command line. Exit codes: 0 ok, 1 findings or diff, 2 usage or configuration error.

``--json`` (kinds, backends, schema, validate, plan) prints exactly one JSON document on
stdout through ``prov.text.json_text``; its shapes are documented in docs/kinds.md
("Machine-readable output"), and every document but ``schema``'s carries
``"format": JSON_FORMAT``. Exit codes do not change with it, and an exit-2 error stays text
on stderr (``prov: error: …``, possibly with indented continuation lines) with nothing on
stdout.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from . import __version__, kinds
from .kinds.common import DEFS
from .plan import plan_lines
from .registry import builtin_names, call_plugin, discover, for_kind, lookup, preserved
from .render import atomic_write, diff_rendered, safe_item_dir, special_entries, write_rendered
from .schema import ID_PATTERN, KIND_PATTERN, NAME_PATTERN, SPEC_FILE, ConfigError, api_versions, kind_group, read_toml, schema_json, spec_schema, tomllib
from .spec import check_item, check_spec, load_item, parse_ref
from .text import esc, json_text, short_repr, show
from .validate import ERROR, WARNING, Finding, has_errors

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2
# The version of the --json document formats (kinds, backends, validate, plan), versioned
# separately from the spec api_version; bump policy in docs/kinds.md "Machine-readable output".
JSON_FORMAT = "prov.cli/v1alpha1"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse exits 2 already; keep its format
        # argparse echoes argv ("unrecognized arguments", "invalid choice"), which a shell glob
        # over hostile names can fill: escape it like every other message.
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {esc(message)}\n")


def resolve_root(arg: Optional[str]) -> Path:
    """--root, else $PROV_ROOT, else the current directory."""
    return Path(arg or os.environ.get("PROV_ROOT") or os.getcwd())


def item_dir(root: Path, ref: str) -> Tuple[str, str, Path]:
    """(kind, name, ``<root>/<kind>/<name>``) for a valid ref, refusing symlinked directories."""
    kind, name = parse_ref(ref)
    return kind, name, safe_item_dir(root, kind, name)


def _load(args: argparse.Namespace) -> tuple:
    """(backend, spec, findings, item dir, ctx) of a valid item, its references resolved (exit 2 otherwise)."""
    backends = discover()
    kind, name, idir = item_dir(args.root, args.ref)
    plugin, spec, findings, ctx = load_item(args.root, kind, name, backends)
    return plugin, spec, findings, idir, ctx


def _sorted_findings(findings: List[Any]) -> List[Any]:
    """Errors first, then by (where, message): the order of both the text and the JSON output."""
    return sorted(findings, key=lambda f: (f.level != ERROR, f.where, f.message))


def _print_findings(findings: List[Any], warnings_to_stderr: bool = False) -> None:
    for f in _sorted_findings(findings):
        print(f, file=sys.stderr if f.level == ERROR or warnings_to_stderr else sys.stdout)


def _print_json(document: Any) -> None:
    """The one JSON document of a ``--json`` call, stamped with ``format``; raw values, ASCII-escaped by ``json_text``."""
    sys.stdout.write(json_text({**document, "format": JSON_FORMAT}))


def _doc_line(obj: Any) -> str:
    """The first docstring line of a plugin, raw (plugin text: escape before printing)."""
    doc = (getattr(obj, "__doc__", None) or "").strip().splitlines()
    return doc[0] if doc else ""


def _summary(obj: Any) -> str:
    return esc(_doc_line(obj))  # plugin text


def cmd_kinds(args: argparse.Namespace) -> int:
    found = kinds.discover()
    if args.json:
        own = kinds.builtin_names()
        _print_json({
            "kinds": [
                {
                    "name": name,
                    "group": kind_group(name),
                    "api_versions": api_versions(kind),
                    "summary": _doc_line(kind),
                    "builtin": name in own,
                    "outputs": getattr(kind, "OUTPUTS", None),
                    "refs": kind.REFS,
                }
                for name, kind in found.items()
            ]
        })  # fmt: skip
        return EXIT_OK
    for name, kind in found.items():
        print(f"{name}\t{', '.join(api_versions(kind))}\t{_summary(kind)}")
    return EXIT_OK


def cmd_backends(args: argparse.Namespace) -> int:
    backends = discover()
    if args.kind is not None:
        kinds.get(args.kind)
        backends = for_kind(backends, args.kind)
    if args.json:
        own = builtin_names()
        _print_json({
            "backends": [
                {
                    "name": name,
                    "kind": plugin.KIND,
                    "summary": _doc_line(plugin),
                    "builtin": name in own,
                    "starter": hasattr(plugin, "starter"),
                    "check": hasattr(plugin, "check"),
                    "preserve": list(preserved(plugin)),
                }
                for name, plugin in backends.items()
            ]
        })  # fmt: skip
        return EXIT_OK
    for name, plugin in backends.items():
        print(f"{name}\t{plugin.KIND}\t{_summary(plugin)}")
    return EXIT_OK


def _entry_kind(path: Path) -> str:
    """"dir", "symlink" or "other", by lstat (never follows a link)."""
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    return "dir" if stat.S_ISDIR(mode) else "other"


def cmd_list(args: argparse.Namespace) -> int:
    """Every item under the root, as ``<ref>\\t<backend>`` on stdout.

    Problems are findings on stderr, warnings included, so stdout stays the listing: errors
    (symlinks, unreadable entries, invalid names or spec files) exit 1, warnings (a dotted
    directory whose kind is not installed) exit 0. Only names matching the item name pattern are ever printed raw; any other name appears as
    a capped repr, so a planted file name cannot forge output lines or terminal escapes.
    """
    root = Path(args.root)
    if not root.is_dir():
        raise ConfigError(f"root {show(str(root))} does not exist or is not a directory")
    root = root.resolve()
    known = kinds.discover()
    problems: List[Finding] = _unknown_dotted_kinds(root, known)
    for kind in known:
        kdir = root / kind
        try:
            if not os.path.lexists(kdir):
                continue
            what = _entry_kind(kdir)
            if what == "symlink":
                problems.append(Finding(ERROR, "is a symlink; refused", kind))
            elif what != "dir":
                problems.append(Finding(ERROR, "is not a directory; a kind directory holds <name>/ items", kind))
            if what != "dir":
                continue
            entries = sorted(kdir.iterdir())
        except OSError as exc:
            problems.append(Finding(ERROR, f"cannot read: {exc.strerror or exc}", kind))
            continue
        for entry in entries:
            valid = re.fullmatch(NAME_PATTERN.strip("^$"), entry.name) is not None
            ref = f"{kind}/{entry.name}" if valid else f"{kind}/{show(entry.name)}"
            try:
                what = _entry_kind(entry)
                if what == "symlink":
                    problems.append(Finding(ERROR, "is a symlink; refused", ref))
                    continue
                if what != "dir" or not os.path.lexists(entry / SPEC_FILE):
                    continue  # not an item (notes, empty directories)
            except OSError as exc:
                problems.append(Finding(ERROR, f"cannot read: {exc.strerror or exc}", ref))
                continue
            if not valid:
                problems.append(Finding(ERROR, f"invalid item name '{show(entry.name)}' (lowercase letters, digits, '-'; one DNS label)", ref))
                continue
            try:
                raw = read_toml(entry / SPEC_FILE)
            except ConfigError as exc:
                problems.append(Finding(ERROR, str(exc).replace(show(str(entry / SPEC_FILE)) + ": ", ""), ref))
                continue
            backend = raw.get("backend")
            print(f"{ref}\t{backend if isinstance(backend, str) and re.fullmatch(ID_PATTERN.strip('^$'), backend) else '-'}")
    # stdout is the listing (ref, backend); every finding, warnings included, goes to stderr
    _print_findings(problems, warnings_to_stderr=True)
    return EXIT_FINDINGS if has_errors(problems) else EXIT_OK


def _unknown_dotted_kinds(root: Path, known: Any) -> List[Finding]:
    """A warning per dotted directory (a third-party kind's shape) that holds items but whose kind is not installed."""
    out: List[Finding] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out  # the kind loop below reports nothing either; an unreadable root is not a kind problem
    missing = "no kind of this name is installed; its items are not listed (install the plugin that provides it)"
    for entry in entries:
        name = entry.name
        if "." not in name or name in known or not re.fullmatch(KIND_PATTERN.strip("^$"), name):
            continue
        try:
            what = _entry_kind(entry)
            if what == "symlink":  # never followed
                out.append(Finding(WARNING, "is a symlink; refused (and no kind of this name is installed)", name))
                continue
            if what != "dir":
                continue
            # an item is a real directory holding spec.toml; a symlinked item is never followed
            holds_items = any(_entry_kind(item) == "dir" and os.path.lexists(item / SPEC_FILE) for item in entry.iterdir())
        except OSError:
            holds_items = True  # unreadable: say so rather than hide it
        if holds_items:
            out.append(Finding(WARNING, missing, name))
    return out


def cmd_schema(args: argparse.Namespace) -> int:
    """The schema is JSON already: ``--json`` changes nothing."""
    kind = kinds.get(args.kind)
    backends = for_kind(discover(), args.kind)
    if args.backend is not None:
        backends = {args.backend: lookup(backends, args.kind, args.backend)}
    sys.stdout.write(schema_json(spec_schema(kind, backends, DEFS)))
    return EXIT_OK


def cmd_init(args: argparse.Namespace) -> int:
    backends = discover()
    kind, name = parse_ref(args.ref)
    plugin = lookup(backends, kind, args.backend)
    if not hasattr(plugin, "starter"):
        raise ConfigError(f"backend {args.backend!r} provides no starter spec.toml")
    path = safe_item_dir(args.root, kind, name) / SPEC_FILE
    if path.is_symlink():
        raise ConfigError(f"{show(str(path))} is a symlink; prov refuses to write through symlinks")
    if path.exists() and not args.force:
        raise ConfigError(f"{show(str(path))} exists (use --force to overwrite)")
    text = call_plugin(plugin, "starter", name)
    try:
        spec, findings = check_spec(tomllib.loads(text), kind, name, backends)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"starter spec.toml of backend {args.backend!r} is invalid: {esc(str(exc))}") from None
    if spec is None:  # a broken starter is a plugin bug; never write it
        raise ConfigError("starter spec.toml is invalid: " + "; ".join(str(f) for f in findings))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, text)
    print(f"wrote {show(str(path))}")
    return EXIT_OK


def cmd_render(args: argparse.Namespace) -> int:
    plugin, spec, findings, idir, ctx = _load(args)
    _print_findings(findings)
    changes = write_rendered(idir / "rendered", call_plugin(plugin, "render", spec, ctx), preserved(plugin))
    for line in changes:
        print(line)
    print(f"rendered {args.ref} ({plugin.NAME}): {len(changes)} change(s) in {show(str(idir / 'rendered'))}")
    return EXIT_OK


def cmd_diff(args: argparse.Namespace) -> int:
    plugin, spec, _, idir, ctx = _load(args)
    diff = diff_rendered(idir / "rendered", call_plugin(plugin, "render", spec, ctx), preserved(plugin))
    if diff:
        sys.stdout.write(diff)
        return EXIT_FINDINGS
    print(f"{args.ref}: rendered output is up to date")
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    backends = discover()
    kind, name, idir = item_dir(args.root, args.ref)
    raw, spec, findings, ctx = check_item(args.root, kind, name, backends)  # references resolved into ctx.refs
    outputs = refs = None  # null in --json while the spec is invalid
    if spec is not None:
        outputs = kinds.item_outputs(kinds.get(kind), spec)  # a broken outputs is exit 2, like a broken semantic
        refs = {alias: {"ref": entry["ref"], "kind": entry["kind"]} for alias, entry in ctx.refs.items()}
        plugin = backends[spec["backend"]]
        rendered = idir / "rendered"
        if not rendered.is_dir():
            findings.append(Finding(ERROR, "not rendered yet; run prov render " + args.ref, "rendered"))
        else:
            special = special_entries(rendered)
            for rel in special:  # never followed or read: could point anywhere
                findings.append(Finding(ERROR, "symlink, special file or unsafely named file inside rendered/; run prov render to remove it", f"rendered/{show(rel)}"))
            if not special:
                findings += call_plugin(plugin, "validate", rendered)
            if diff_rendered(rendered, call_plugin(plugin, "render", spec, ctx), preserved(plugin)):
                findings.append(Finding(ERROR, "out of date with spec.toml; run prov diff / prov render", "rendered"))
    failed = has_errors(findings)
    if args.json:  # every finding on stdout, nothing on stderr; the exit code says the same as without --json
        backend = raw.get("backend")  # the spec's own value, even when other keys fail validation
        _print_json({
            "ref": args.ref,
            "backend": backend if isinstance(backend, str) and backend in for_kind(backends, kind) else None,
            "ok": not failed,
            "outputs": outputs,
            "refs": refs,
            "findings": [{"level": str(f.level), "where": str(f.where), "message": str(f.message)} for f in _sorted_findings(findings)],
        })  # fmt: skip
        return EXIT_FINDINGS if failed else EXIT_OK
    _print_findings(findings)
    if failed:
        return EXIT_FINDINGS
    print(f"ok: {args.ref}")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    plugin, spec, _, idir, ctx = _load(args)
    lines = plan_lines(plugin, spec, ctx, args.ref, idir / "rendered")  # refuses unsafe lines (exit 2) before printing any
    if args.json:
        _print_json({"ref": args.ref, "backend": plugin.NAME, "rendered_dir": str(idir / "rendered"), "lines": lines})
        return EXIT_OK
    for line in lines:
        print(line)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="prov",
        description="Render, validate and plan provisioning artefacts from <root>/<kind>/<name>/spec.toml. "
        "prov never applies anything: plan prints the steps for a human.",
    )
    parser.add_argument("--version", action="version", version=f"prov {__version__}")
    parser.add_argument("--root", help="directory holding <kind>/<name>/ items (default: $PROV_ROOT, else the current directory)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)
    sub.required = True

    def add(name: str, func: Any, help_: str, ref: bool = True, json_help: Optional[str] = None) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_, description=help_)
        if ref:
            p.add_argument("ref", help="item ref <kind>/<name>, e.g. os-install/demo")
        if json_help is not None:
            p.add_argument("--json", action="store_true", help=json_help)
        p.set_defaults(func=func, json=False)
        return p

    as_json = "print one JSON document instead of text (shapes: docs/kinds.md, Machine-readable output)"
    add("kinds", cmd_kinds, "list the kinds prov knows and their api versions", ref=False, json_help=as_json)
    p = add("backends", cmd_backends, "list registered backends (NAME, KIND)", ref=False, json_help=as_json)
    p.add_argument("--kind", help="only backends of this kind")
    add("list", cmd_list, "list every item under the root (ref, backend)", ref=False)
    p = add("schema", cmd_schema, "print the JSON Schema of a kind for its registered backends", ref=False, json_help="accepted for symmetry; the schema is JSON already")
    p.add_argument("kind")
    p.add_argument("--backend", help="only this backend's table")
    p = add("init", cmd_init, "write a synthetic starter <kind>/<name>/spec.toml")
    p.add_argument("--backend", required=True, help="backend that renders the item")
    p.add_argument("--force", action="store_true", help="overwrite an existing spec.toml")
    add("render", cmd_render, "render artefacts into <kind>/<name>/rendered/")
    add("validate", cmd_validate, "validate spec.toml and the rendered artefacts", json_help=as_json + "; findings on stdout only")
    add("diff", cmd_diff, "diff a fresh render against <kind>/<name>/rendered/")
    add("plan", cmd_plan, "print the apply steps for a human (never runs them)", json_help=as_json)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.root = resolve_root(args.root)
    try:
        return args.func(args)
    except ConfigError as exc:
        message = str(exc)
    except tomllib.TOMLDecodeError as exc:
        message = f"invalid TOML: {exc}"
    except UnicodeDecodeError as exc:
        message = f"not valid UTF-8: {exc}"
    except subprocess.TimeoutExpired as exc:
        message = f"timed out: {exc}"
    except re.error as exc:
        message = f"invalid regular expression: {exc}"
    except ValueError as exc:  # e.g. an unsupported schema keyword or $ref
        message = str(exc)
    except OSError as exc:  # IsADirectoryError, PermissionError, ...
        filename = show(os.fsdecode(exc.filename)) + ": " if exc.filename else ""
        message = f"{filename}{exc.strerror or exc}"
    # The printing boundary: plugin and filesystem text was escaped where it entered the
    # message; escape again here so nothing unsafe can reach the terminal. Only core's own
    # multi-line messages (a spec's list of errors) keep their newlines.
    print(f"prov: error: {esc(message, keep=chr(10))}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
