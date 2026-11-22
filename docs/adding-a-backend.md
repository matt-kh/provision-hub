# Adding a backend

A backend is a plugin: one Python module registered under the `prov.backends` entry-point
group. It renders items of exactly one **kind** ([kinds.md](kinds.md)). prov core owns the
CLI, the spec envelope, the kind's core schema, defaults, semantic checks, file writing,
diffing and the plan header; the backend only turns a validated spec into files, checks
those files and lists the apply steps. This page is the whole contract — you do not need to
read `src/prov/backends/ubuntu_autoinstall.py`, though it is a working reference.

A Talos backend for the `os-install` kind is used as the running example below.

## 1. The contract

The module (`src/prov/backends/talos.py` for a built-in, any importable module for a third
party) exposes:

| Member | Required | Signature | Purpose |
|---|---|---|---|
| `NAME` | yes | `str` | Backend id, `[a-z][a-z0-9-]*`, globally unique. Equals the entry-point name and the spec's `backend` value, and names its table (`[talos]`). May not equal an envelope key or one of its own kind's core properties (`hostname`, `storage`, ...). Third parties: see [Naming](#naming-third-party-backends). |
| `KIND` | yes | `str` | The kind it renders: `"os-install"`, or a third-party kind plugin such as `"registry.example.org"` ([kinds.md](kinds.md#kind-plugins)); must be an installed kind (`prov kinds`). |
| `PLUGIN_API` | no | `int` | The backend contract version it implements; `1` today, and `1` when absent. Any other value is refused. Declare it: prov's own backends do. |
| `SCHEMA` | yes | `dict` | JSON Schema fragment for the backend table (section 2). |
| `render` | yes | `render(spec: dict, ctx: Context) -> dict[str, str]` | Relative POSIX path → file text. Pure: no I/O, no clock, no randomness. |
| `validate` | yes | `validate(rendered_dir: pathlib.Path) -> list[Finding]` | Check the files on disk. |
| `plan` | yes | `plan(spec: dict, ctx: Context) -> list[str]` | Apply steps for a human, as printed lines. |
| `check` | no | `check(spec: dict, ctx: Context) -> list[Finding]` | Cross-field rules JSON Schema cannot express. |
| `starter` | no | `starter(name: str) -> str` | `spec.toml` text for `prov init <kind>/<name> --backend <NAME>`; without it `init` refuses. |
| `PRESERVE` | no | `tuple[str, ...]` | Relative paths of files a human may create in `rendered/` that `render` must keep and `diff`/`validate` must not report as stale (e.g. `("flake.lock",)`), written normalised (no `./`, `//` or trailing `/`). Only regular files are kept; symlinks and special files are removed whatever their name. A listed name that `render` also returns, or that is a parent directory of a file it returns, is not preserved (render's output wins), so list only names that are never a file or a parent directory of a file `render` produces. |
| `REFS` | no | `dict` | Reserved; must be empty (`{}`) if present. References are declared by the kind (its `REFS`) and reach your backend as `ctx.refs`. |
| `FIXTURES` | no | `dict[str, dict]` | Your backend table for each conformance fixture of the kind (`{"basic": {"port": 8081}}`), negative ones (`expect.valid = false`) included; see [Conformance](#6-conformance). |
| `UNSUPPORTED_FIXTURES` | no | `dict[str, str]` | Fixtures of the kind your backend cannot express, each with a reason; `prov conformance` reports them as skipped. |
| `artifacts` | no | `artifacts(spec: dict) -> ...` | Reserved for `prov build` (a later release). |
| `build` + `BUILD_TOOLS` | no | `build(spec, ctx)`, `BUILD_TOOLS: tuple[str, ...]` | Reserved for `prov build`, which will write only under the item's `build/`. A backend that defines `build` must list the local tools it runs in `BUILD_TOOLS`. |

The module docstring's first line is shown by `prov backends`.

### Naming third-party backends

Backend names are not namespaced: one flat, global set of ids. A third-party backend
should carry a vendor prefix, `<vendor>-<tool>` (`acme-talos`, not `talos`), so it does
not collide with a backend another package, or provision-hub itself, ships later. This is a
convention, not a check; it will be revisited before 1.0.

`Finding` is `prov.validate.Finding(level, message, where="")`, a frozen dataclass; `level` is
one of `prov.validate.ERROR` (`"error"`), `WARNING` (`"warning"`) or `SKIPPED` (`"skipped"`).
`where` names the file or spec key (`"machineconfig.yaml"`, `"storage.disks"`). Only errors
make `prov validate` exit 1. Printed form: `error: storage.disks: needs one disk`.

The registry (`prov.registry.discover`) rejects a backend that lacks a required member, whose
optional members are not callable, whose `PLUGIN_API` is not `1`, whose `KIND` is unknown,
whose `NAME` is not an id, differs from its entry-point name, is already registered or
collides with an envelope key (`refs` included) or a core property of its kind, whose `REFS` is not empty,
that defines `build` without `BUILD_TOOLS`, whose `FIXTURES` or `UNSUPPORTED_FIXTURES` are not a dict of str names to
plain JSON tables / to non-empty reasons (or name a fixture in both; a name the kind does not
have is a `FAIL` of `prov conformance`, not a load error), whose `PRESERVE` is not a list of safe, normalised relative
paths, or whose `SCHEMA` uses an unsupported keyword, an
invalid keyword value or an invalid `pattern`. Every call into a backend goes through
`prov.registry.call_plugin` (the same guarded call prov uses for kinds, `prov.plugin`): an exception escaping it, or a result of the wrong type (`render`
not a `dict[str, str]`, `validate`/`check` not a `list[Finding]`, `plan` not a `list[str]`,
`starter` not a `str`), is reported as `backend '<NAME>' failed in <method>: ...` with exit
code 2; a `ConfigError` your backend raises is reported the same way, with your text.

Core escapes all of your text where it is printed: finding `level`, `where` and `message`,
exception and `ConfigError` text, and the module docstring shown by `prov backends`. Every
character that is not printable (`str.isprintable`: controls, newlines, bidi and zero-width
marks, lone surrogates ...) appears as `\xNN`, `\uNNNN` or `\UNNNNNNNN`, so a finding is
always one line. Escape tool output yourself only if you want to; never rely on newlines in
a message. Plan lines are the exception: they are refused, not escaped (section 3).

### `ctx`

`ctx` is `prov.registry.Context`, a frozen dataclass:

- `ctx.name` — the item name, i.e. the directory `<root>/<kind>/<name>/` (`"demo-cp"`).
- `ctx.refs` — the item's resolved references ([kinds.md](kinds.md#references)): a
  read-only mapping from each alias present in the spec's `[refs]` table to
  `{"ref": "registry.example.org/hub", "kind": "registry.example.org", "name": "hub",
  "outputs": {...}}`, where `outputs` are the target's outputs (its kind's `OUTPUTS`, `{}`
  for a kind without). Deeply read-only: mappings are `types.MappingProxyType`, arrays
  tuples; copy before changing or serialising (`json.dumps(dict(...))` fails on a mapping
  proxy). `check`, `render` and `plan` get it populated; they are called only when every
  reference resolved. An optional alias the spec leaves out is absent: test with `in`.
  Empty for a kind without references.

Use `ctx.name` only where the item name is meant; values the user wrote (such as
`hostname`) come from `spec`.

### What `spec` contains

`render`, `plan` and `check` receive the parsed `spec.toml` as a plain `dict` **after** the
envelope check, the full JSON Schema and the kind's semantic checks, **with every schema
`default` filled in** (core keys and your table). Keys without a default may be absent: test
with `in`. The envelope keys are present too: `api_version`, `kind`, `backend` (your `NAME`). For a kind
with references, `spec["refs"]` holds the raw `[refs]` table (alias to ref string) when the
spec has one; use `ctx.refs` for the resolved targets.

For the `os-install` kind (authoritative definition:
`schema/os-install.v1alpha1.schema.json`, described in [kinds.md](kinds.md)):

```text
hostname        str (one DNS label)          domain    str, optional
arch            "amd64" | "arm64" (amd64)    firmware  "uefi" | "bios" (uefi)
timezone        str (Etc/UTC)                locale    str (en_US.UTF-8)
keyboard        str (us)
users[] ([])    name, gecos?, groups [], shell (/bin/bash), sudo (false),
                ssh_authorized_keys [], password_hash? (crypt hash, never plain text)
network?        optional; when present: interfaces[] (at least one): name, macaddress?,
                              dhcp4 (false), dhcp6 (false), addresses [] (CIDR),
                              gateway4?, gateway6?, mtu?
                dns: nameservers [], search []
storage         disks [] (whole-disk device paths; first = boot disk; all may be wiped)
<NAME>          your table
```

Already guaranteed by the kind: addresses are valid CIDR, gateways and nameservers are valid
IPs of the right family, interface and user names are unique, every interface has an address
or DHCP. Login rules (e.g. Ubuntu requires a user with an ssh key) belong in your `check`.
`network` is optional: test `"network" in spec` before reading it in `render`, `plan` and
`check`. A backend that cannot work without it reports an error in `check` (which runs
before `render` and `plan`), e.g. ubuntu-autoinstall's "ubuntu-autoinstall needs [network]
with at least one interface (netplan is rendered from it)"; one that cannot use it (a
machine that joins networks interactively) may reject it the same way. If
a core key cannot be honoured by your backend (Talos has no local users), say so in `check`
with an error or warning rather than silently ignoring it.

## 2. The schema fragment

`SCHEMA` describes only your table. Core inserts it as `properties.<NAME>` of the kind's
schema and makes the table required when `backend = "<NAME>"`. Rules:

- `"type": "object"`, `"additionalProperties": false` on every object.
- Keyword values are checked too (e.g. `type` must name a JSON type, counts are
  non-negative integers, `required` is a list of strings, `enum` is non-empty). A fragment
  may not define its own `$defs`; `$ref` may only point at the shared definitions in
  `prov.kinds.common.DEFS` (`#/$defs/user`, `#/$defs/interface`).
- Only these keywords (the built-in validator rejects others): `type`, `enum`, `const`,
  `pattern`, `minLength`, `maxLength`, `minimum`, `maximum`, `properties`, `required`,
  `additionalProperties`, `items`, `minItems`, `maxItems`, `uniqueItems`, `allOf`, `if`,
  `then`, `$ref` (to `#/$defs/...` only), `title`, `description`, `default`.
- `pattern` uses ECMA-262 semantics, as in every JSON Schema validator: `$` matches only at
  the very end (prov translates it for Python, where `$` also matches before a trailing
  newline). **Anchor every pattern with `^...$`** and use explicit character classes instead
  of `\S` or `.`; an unanchored `^https://` accepts anything after the scheme.
- Patterns are compiled with `re.ASCII`, so `\d`, `\w` and `\s` are ASCII-only. Prefer
  explicit classes; `[^\s]` would be looser than ECMA for non-ASCII spaces.
- prov rejects control characters (C0, DEL, C1, U+2028/U+2029) in every string, so spec
  values are always single-line.
- Numbers follow JSON: `1.0` is a valid `integer` and is normalised to `1`; `true` never
  equals `1`.
- Use `default` for optional values so `render` can index them directly.
- No secrets: no plain passwords, tokens or private keys. Reference them by path or hash, or
  leave them to the apply step.
- It is public API and follows the versioning policy in [kinds.md](kinds.md): while the
  kind is `v1alpha*` a breaking change needs a CHANGELOG migration note; from `prov/v1` on,
  only additive optional changes.

```python
"""Talos Linux machine configuration for one node."""

NAME = "talos"
KIND = "os-install"
PLUGIN_API = 1

SCHEMA = {
    "type": "object",
    "description": "Talos Linux machine configuration.",
    "additionalProperties": False,
    "required": ["version", "role", "cluster"],
    "properties": {
        "version": {"type": "string", "pattern": "^v1\\.[0-9]+\\.[0-9]+$"},
        "role": {"enum": ["controlplane", "worker"]},
        "cluster": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "endpoint"],
            "properties": {
                "name": {"type": "string", "pattern": "^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"},
                "endpoint": {"type": "string", "pattern": "^https://[a-z0-9.-]+(:[0-9]{1,5})?$"},
            },
        },
        "install_image": {
            "type": "string",
            "pattern": "^[a-z0-9.-]+(/[a-z0-9._-]+)+(:[A-Za-z0-9._-]+)?$",
            "default": "ghcr.io/siderolabs/installer",
        },
    },
}
```

## 3. render, validate, plan

```python
import shlex
from pathlib import Path

from prov.util import dump_yaml, read_regular_text   # sorted keys, block style, no aliases
from prov.validate import ERROR, SKIPPED, Finding

def check(spec, ctx):
    out = []
    if len(spec["storage"]["disks"]) != 1:
        out.append(Finding(ERROR, "talos installs to exactly one disk", "storage.disks"))
    if spec["users"]:
        out.append(Finding(ERROR, "talos has no local users; remove [[users]]", "users"))
    return out

def render(spec, ctx):
    config = {"machine": {"type": spec[NAME]["role"], "install": {"disk": spec["storage"]["disks"][0]}}}
    return {"machineconfig.yaml": dump_yaml(config)}

def validate(rendered_dir: Path):
    findings = []
    try:
        read_regular_text(rendered_dir / "machineconfig.yaml")
    except FileNotFoundError:
        findings.append(Finding(ERROR, "missing; run prov render", "machineconfig.yaml"))
    # external tool: look it up with shutil.which; absent -> SKIPPED, never an error
    findings.append(Finding(SKIPPED, "talosctl not installed (talosctl validate)"))
    return findings

def plan(spec, ctx):
    q = shlex.quote
    return [
        f"# 1. Boot {q(spec['hostname'])} from the Talos ISO.",
        "talosctl apply-config --insecure --nodes <node-ip> --file machineconfig.yaml",
    ]
```

Requirements:

- **render** returns every file the backend needs; paths are relative, without `..` or
  control characters. Core writes them to `<root>/<kind>/<name>/rendered/` and removes files
  it did not return, except the regular files named in `PRESERVE`. Output must be byte-for-byte deterministic ([determinism.md](determinism.md); use `prov.util.dump_yaml`
  or sorted JSON), text only, end with a newline and carry no trailing whitespace. Never
  embed secrets that are not in `spec`.
- **validate** parses what is on disk (it may have been edited by hand): required files,
  syntax, required keys, cross-references. Read files with `prov.util.read_regular_text`
  (refuses symlinks and special files, never follows links); core already reports any
  symlink or special file in `rendered/` and then skips your `validate`. Guard every access
  with `isinstance`: hand-edited YAML can have any shape. It may run a local, read-only
  validator found via `shutil.which`; when missing report
  `Finding(SKIPPED, "<tool> not installed (<what>)")`. No network, no writes. Core separately
  reports staleness against a fresh render.
- **plan** only returns text; it must not run, write or download anything. Wrap every spec
  value in `shlex.quote`, in comments too, so a pasted line never splits into extra
  commands. Core refuses (exit 2, naming your backend) any line containing a line break or
  another unprintable character other than a tab — escape sequences, bidi or zero-width
  marks — so a pasted line is exactly the line shown. Core prints a header and
  `cd <root>/<kind>/<name>/rendered` first, so write commands relative to the rendered
  directory. Make them exact and copy-pastable, comment lines with `#`, warn about
  destructive steps (which disks are wiped), and use `<placeholders>` for values prov cannot
  know.
- **starter** (optional) returns a complete, valid `spec.toml` with synthetic values only,
  starting with the envelope (`api_version = "prov/v1alpha1"`, `kind`, `backend`);
  `prov init` validates it before writing.

## 4. Register the entry point

The entry-point name must equal `NAME`. Built-in, in this repository's `pyproject.toml`:

```toml
[project.entry-points."prov.backends"]
ubuntu-autoinstall = "prov.backends.ubuntu_autoinstall"
talos = "prov.backends.talos"
```

Third-party package, in its own `pyproject.toml` (then `uv add` it next to prov), with a
vendor-prefixed name:

```toml
[project.entry-points."prov.backends"]
acme-talos = "prov_acme_talos"
```

Run `uv sync` so the entry point is installed; `uv run prov backends --kind os-install`
should list it. Built-in and third-party backends register the same way; prov never imports
a backend by name. `prov.registry.builtin()` is the set the `provision-hub` distribution
registers, and the published schemas are generated from it, so a built-in needs nothing
beyond its entry point.

## 5. Schema file, example, goldens, tests

1. Example item with synthetic data only — documentation addresses (192.0.2.0/24,
   198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32), example.com/org/net, fake ssh keys
   containing `EXAMPLE` (checked by `tests/test_synthetic_data.py`):
   `examples/os-install/demo-talos-cp/spec.toml`. All examples share the one root
   `examples/`; add one item per materially different output (layout, role, firmware).
2. `PROV_UPDATE_GOLDENS=1 uv run pytest -q` regenerates
   `schema/<kind>.<version>.schema.json` and writes each example's `rendered/` directory;
   review the diff and commit both. From then on `tests/test_goldens.py` and
   `tests/test_schema.py` pick the example up automatically (golden equality, determinism,
   `validate` without errors, round trip against the published schema with the `jsonschema`
   reference validator). Golden changes to existing examples need a reason in the PR.
3. Add `tests/test_talos.py` for what is specific to the backend: each `check` rule, broken
   rendered files that `validate` must catch, the external validator call (monkeypatch
   `shutil.which` and `subprocess.run`), and that `plan` lists the expected commands.
   `tests/test_plan.py` style: monkeypatch `subprocess`/`os` exec functions to fail and
   assert plan still succeeds.
4. Document the backend in the README table.

Checklist: `uv run pytest -q`, `uv run --python 3.10 pytest -q`,
`uv run prov --root examples validate os-install/demo-talos-cp`, `uv run prov conformance <NAME>`
(exit 0: every fixture of the kind ok or skipped with a reason), then
`PROV_UPDATE_GOLDENS=1 uv run pytest -q` to regenerate `conformance/<kind>.<version>.fixtures.json`
(a built-in backend's tables and reasons are exported there; review the diff), and
`pre-commit run --all-files`.

## 6. Conformance

A kind may ship conformance fixtures ([kinds.md](kinds.md#conformance-fixtures)): small
specs and what every backend must make of them. `uv run prov conformance <NAME>` renders
each one through your backend and checks it statically: the spec is valid (or, for a
negative fixture, rejected), outputs match, two renders are equal, the files written to a
temporary `rendered/` diff clean and rewrite with no change, your `validate` reports no
error, expected strings are present or absent, and your plan passes the plan refusal
checks. It writes only to a fresh temporary directory from `$TMPDIR`, never in the item
tree, removes it afterwards, and runs nothing.

```python
FIXTURES = {"basic": {"role": "controlplane"}}           # your table per fixture
UNSUPPORTED_FIXTURES = {"two-disks": "talos installs to exactly one disk"}
```

- Give a table for each fixture that needs one, negative fixtures (`expect.valid = false`)
  included; without a table prov uses `{}`, which only works when your `SCHEMA` requires no
  keys (else the fixture fails with `no backend table for fixture`). A negative fixture's
  table must be valid: the fixture must be rejected by an error outside your table (the
  kind's rules, or your `check` about core keys); an error in your table does not count.
- A name in `FIXTURES` or `UNSUPPORTED_FIXTURES` that the kind does not have (it renamed or
  dropped a fixture) fails `prov conformance` but does not stop your backend loading.
- List a fixture in `UNSUPPORTED_FIXTURES` only when your tool truly cannot express it, with
  a reason a reader can check; never relax `validate` or `check` to make a fixture pass.
- os-install ships fixtures ([kinds.md](kinds.md#os-install-v1alpha1)); an os-install
  backend gives a table (or a reason) for each. A built-in backend's tables and reasons are
  exported with the kind's fixtures to `conformance/<kind>.<version>.fixtures.json`
  ([Exported fixtures](kinds.md#exported-fixtures)); regenerate it with
  `PROV_UPDATE_GOLDENS=1 uv run pytest -q` after changing them.
- Conformance is static: it proves the rendered files are consistent, never that an install
  works.
- Behavioural tests (does the installed host answer on its port, are the disks laid out?)
  are files you render, such as `goss.yaml`, and a command your `plan` prints for a human
  (`goss --gossfile goss.yaml validate`). `prov conformance` and every other prov command
  only check that the plan lines are printable; prov never runs them.
