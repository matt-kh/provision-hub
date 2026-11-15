"""prov command line. Exit codes: 0 ok, 1 findings or diff, 2 usage or configuration error."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence

from . import __version__
from .plan import plan_lines
from .registry import discover
from .render import atomic_write, diff_rendered, safe_host_dir, special_entries, write_rendered
from .schema import HOSTNAME_PATTERN, ConfigError, host_schema, read_toml, schema_json, tomllib
from .validate import ERROR, WARNING, Finding, check_host, has_errors, load_host

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse exits 2 already; keep its format
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def resolve_root(arg: Optional[str]) -> Path:
    """--root, else $PROV_ROOT, else the current directory."""
    return Path(arg or os.environ.get("PROV_ROOT") or os.getcwd())


def host_dir(root: Path, host: str) -> Path:
    """``<root>/hosts/<host>`` for a valid host name, refusing symlinked directories."""
    if not re.fullmatch(HOSTNAME_PATTERN.strip("^$"), host):
        raise ConfigError(f"invalid host name {host!r} (lowercase letters, digits, '-')")
    return safe_host_dir(root, host)


def call_plugin(plugin: Any, method: str, *args: Any) -> Any:
    """Run a plugin method; any exception it raises becomes a ConfigError naming it."""
    try:
        return getattr(plugin, method)(*args)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"distro plugin {getattr(plugin, 'NAME', '?')!r} failed in {method}: {type(exc).__name__}: {exc}") from exc


def _name_warning(host: Any, dirname: str) -> List[Finding]:
    if host is not None and host["hostname"] != dirname:
        return [Finding(WARNING, f"hostname {host['hostname']!r} differs from the directory name {dirname!r}", "hostname")]
    return []


def _load(args: argparse.Namespace) -> tuple:
    distros = discover()
    hdir = host_dir(args.root, args.host)
    host, findings = load_host(hdir / "host.toml", distros)
    return distros[host["distro"]], host, findings + _name_warning(host, args.host), hdir


def _print_findings(findings: List[Any]) -> None:
    for f in sorted(findings, key=lambda f: (f.level != ERROR, f.where, f.message)):
        print(f, file=sys.stderr if f.level == ERROR else sys.stdout)


def cmd_distros(args: argparse.Namespace) -> int:
    for name, plugin in discover().items():
        doc = (plugin.__doc__ or "").strip().splitlines()
        print(f"{name}\t{doc[0] if doc else ''}")
    return EXIT_OK


def cmd_schema(args: argparse.Namespace) -> int:
    sys.stdout.write(schema_json(host_schema(discover())))
    return EXIT_OK


def cmd_init(args: argparse.Namespace) -> int:
    distros = discover()
    if args.distro not in distros:
        raise ConfigError(f"unknown distro {args.distro!r} (registered: {', '.join(distros) or 'none'})")
    plugin = distros[args.distro]
    if not hasattr(plugin, "starter"):
        raise ConfigError(f"distro {args.distro!r} provides no starter host.toml")
    path = host_dir(args.root, args.host) / "host.toml"
    if path.is_symlink():
        raise ConfigError(f"{path} is a symlink; prov refuses to write through symlinks")
    if path.exists() and not args.force:
        raise ConfigError(f"{path} exists (use --force to overwrite)")
    text = call_plugin(plugin, "starter", args.host)
    host, findings = check_host(tomllib.loads(text), distros)
    if host is None:  # a broken starter is a plugin bug; never write it
        raise ConfigError("starter host.toml is invalid: " + "; ".join(str(f) for f in findings))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, text)
    print(f"wrote {path}")
    return EXIT_OK


def cmd_render(args: argparse.Namespace) -> int:
    plugin, host, findings, hdir = _load(args)
    _print_findings(findings)
    changes = write_rendered(hdir / "rendered", call_plugin(plugin, "render", host))
    for line in changes:
        print(line)
    print(f"rendered {args.host} ({plugin.NAME}): {len(changes)} change(s) in {hdir / 'rendered'}")
    return EXIT_OK


def cmd_diff(args: argparse.Namespace) -> int:
    plugin, host, _, hdir = _load(args)
    diff = diff_rendered(hdir / "rendered", call_plugin(plugin, "render", host))
    if diff:
        sys.stdout.write(diff)
        return EXIT_FINDINGS
    print(f"{args.host}: rendered output is up to date")
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    distros = discover()
    hdir = host_dir(args.root, args.host)
    host, findings = check_host(read_toml(hdir / "host.toml"), distros)
    findings += _name_warning(host, args.host)
    if host is not None:
        plugin = distros[host["distro"]]
        rendered = hdir / "rendered"
        if not rendered.is_dir():
            findings.append(Finding(ERROR, "not rendered yet; run prov render " + args.host, "rendered"))
        else:
            special = special_entries(rendered)
            for rel in special:  # never followed or read: could point anywhere
                findings.append(Finding(ERROR, "symlink or special file inside rendered/; run prov render to remove it", f"rendered/{rel}"))
            if not special:
                findings += list(call_plugin(plugin, "validate", rendered))
            if diff_rendered(rendered, call_plugin(plugin, "render", host)):
                findings.append(Finding(ERROR, "out of date with host.toml; run prov diff / prov render", "rendered"))
    _print_findings(findings)
    if has_errors(findings):
        return EXIT_FINDINGS
    print(f"ok: {args.host}")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    plugin, host, _, hdir = _load(args)
    for line in plan_lines(plugin, host, hdir / "rendered"):
        print(line)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="prov",
        description="Render, validate and plan OS install artefacts from hosts/<host>/host.toml. "
        "prov never applies anything: plan prints the steps for a human.",
    )
    parser.add_argument("--version", action="version", version=f"prov {__version__}")
    parser.add_argument("--root", help="directory holding hosts/ (default: $PROV_ROOT, else the current directory)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)
    sub.required = True

    def add(name: str, func: Any, help_: str, host: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_, description=help_)
        if host:
            p.add_argument("host", help="host name (directory under hosts/)")
        p.set_defaults(func=func)
        return p

    add("distros", cmd_distros, "list registered distro plugins", host=False)
    add("schema", cmd_schema, "print the host JSON Schema for the registered distros", host=False)
    p = sub.add_parser("init", help="write a synthetic starter hosts/<host>/host.toml", description="write a synthetic starter hosts/<host>/host.toml")
    p.add_argument("distro")
    p.add_argument("host")
    p.add_argument("--force", action="store_true", help="overwrite an existing host.toml")
    p.set_defaults(func=cmd_init)
    add("render", cmd_render, "render artefacts into hosts/<host>/rendered/")
    add("validate", cmd_validate, "validate host.toml and the rendered artefacts")
    add("diff", cmd_diff, "diff a fresh render against hosts/<host>/rendered/")
    add("plan", cmd_plan, "print the apply steps for a human (never runs them)")
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
        message = f"{exc.filename + ': ' if exc.filename else ''}{exc.strerror or exc}"
    print(f"prov: error: {message}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
