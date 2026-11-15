# Adding a distro

A distro is a plugin: one Python module registered under the `prov.distros` entry-point
group. prov core owns the CLI, the common host schema, defaults, semantic checks, file
writing, diffing and the plan header; the plugin only turns a validated host into files,
checks those files and lists the apply steps. This page is the whole contract — you do not
need to read `src/prov/distros/ubuntu.py`, though it is a working reference.

Talos is used as the running example below.

## 1. The contract

The module (`src/prov/distros/talos.py` for a built-in, any importable module for a third
party) exposes:

| Member | Required | Signature | Purpose |
|---|---|---|---|
| `NAME` | yes | `str` | Distro id; equals the entry-point name and the host's `distro` value and names its table (`[talos]`). Lowercase, `[a-z0-9-]`. |
| `SCHEMA` | yes | `dict` | JSON Schema fragment for the distro table (section 2). |
| `render` | yes | `render(host: dict) -> dict[str, str]` | Relative POSIX path → file text. Pure: no I/O, no clock, no randomness. |
| `validate` | yes | `validate(rendered_dir: pathlib.Path) -> list[Finding]` | Check the files on disk. |
| `plan` | yes | `plan(host: dict) -> list[str]` | Apply steps for a human, as printed lines. |
| `check` | no | `check(host: dict) -> list[Finding]` | Cross-field host rules JSON Schema cannot express. |
| `starter` | no | `starter(hostname: str) -> str` | `host.toml` text for `prov init <NAME> <host>`; without it `init` refuses. |

The module docstring's first line is shown by `prov distros`.

`Finding` is `prov.validate.Finding(level, message, where="")`, a frozen dataclass; `level` is
one of `prov.validate.ERROR` (`"error"`), `WARNING` (`"warning"`) or `SKIPPED` (`"skipped"`).
`where` names the file or host key (`"machineconfig.yaml"`, `"storage.disks"`). Only errors
make `prov validate` exit 1. Printed form: `error: storage.disks: needs one disk`.

The registry (`prov.registry.discover`) rejects a plugin that lacks a required member, whose
`NAME` differs from its entry-point name, or whose name is already registered.

### What `host` contains

`render`, `plan` and `check` receive the parsed `host.toml` as a plain `dict` **after** it
passed the full JSON Schema and prov's semantic checks, **with every schema `default`
filled in** (common keys and your table). So `host["arch"]` is always present. Keys without
a default (e.g. `domain`, `gateway4`, `macaddress`, `mtu`, `password_hash`) may be absent:
test with `in`.

Common keys (schema v1; the authoritative definition is `schema/host.v1.schema.json`):

```text
schema_version  1
hostname        str (one DNS label)          domain    str, optional
distro          NAME                         arch      "amd64" | "arm64" (default amd64)
firmware        "uefi" | "bios" (uefi)       timezone  str (Etc/UTC)
locale          str (en_US.UTF-8)            keyboard  str (us)
users[] ([])    name, gecos?, groups [], shell (/bin/bash), sudo (false),
                ssh_authorized_keys [], password_hash? (crypt hash, never plain text)
network         interfaces[]: name, macaddress?, dhcp4 (false), dhcp6 (false),
                              addresses [] (CIDR), gateway4?, gateway6?, mtu?
                dns: nameservers [], search []
storage         disks [] (whole-disk device paths; first = boot disk; all may be wiped)
<NAME>          your table
```

Already guaranteed by core: addresses are valid CIDR, gateways and nameservers are valid IPs
of the right family, interface and user names are unique, every interface has an address or DHCP.
Login rules (e.g. Ubuntu requires a user with an ssh key) belong in your `check`. If a common key cannot be honoured by your distro
(Talos has no local users), say so in `check` with an error or warning rather than silently
ignoring it.

## 2. The schema fragment

`SCHEMA` describes only your table. Core inserts it as `properties.<NAME>` of the host schema
and makes the table required when `distro = "<NAME>"`. Rules:

- `"type": "object"`, `"additionalProperties": false` on every object.
- Only these keywords (the built-in validator rejects others): `type`, `enum`, `const`,
  `pattern`, `minLength`, `maxLength`, `minimum`, `maximum`, `properties`, `required`,
  `additionalProperties`, `items`, `minItems`, `maxItems`, `uniqueItems`, `allOf`, `if`,
  `then`, `$ref` (to `#/$defs/...` of the host schema only), `title`, `description`,
  `default`.
- Use `default` for optional values so `render` can index them directly.
- No secrets: no plain passwords, tokens or private keys. Reference them by path or hash, or
  leave them to the apply step.
- It is public API: within `schema_version = 1` only additive optional changes later.

```python
NAME = "talos"

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
                "name": {"type": "string", "pattern": "^[a-z0-9-]+$"},
                "endpoint": {"type": "string", "pattern": "^https://"},
            },
        },
        "install_image": {"type": "string", "default": "ghcr.io/siderolabs/installer"},
    },
}
```

## 3. render, validate, plan

```python
from pathlib import Path
from prov.util import dump_yaml            # sorted keys, block style, no aliases
from prov.validate import ERROR, SKIPPED, Finding

def check(host):
    out = []
    if len(host["storage"]["disks"]) != 1:
        out.append(Finding(ERROR, "talos installs to exactly one disk", "storage.disks"))
    return out

def render(host):
    config = {"machine": {"type": host["talos"]["role"], "install": {"disk": host["storage"]["disks"][0]}}}
    return {"machineconfig.yaml": dump_yaml(config)}

def validate(rendered_dir: Path):
    findings = []
    if not (rendered_dir / "machineconfig.yaml").is_file():
        findings.append(Finding(ERROR, "missing; run prov render", "machineconfig.yaml"))
    # external tool: look it up with shutil.which; absent -> SKIPPED, never an error
    findings.append(Finding(SKIPPED, "talosctl not installed (talosctl validate)"))
    return findings

def plan(host):
    return [
        "# 1. Boot the target from the Talos ISO.",
        "talosctl apply-config --insecure --nodes <node-ip> --file machineconfig.yaml",
    ]
```

Requirements:

- **render** returns every file the distro needs; paths are relative, without `..`. Core
  writes them to `hosts/<host>/rendered/` and removes files it did not return. Output must be
  byte-for-byte deterministic (use `prov.util.dump_yaml` or sorted JSON), end with a newline
  and carry no trailing whitespace. Never embed secrets that are not in `host`.
- **validate** parses what is on disk (it may have been edited by hand): required files,
  syntax, required keys, cross-references. It may run a local, read-only validator found via
  `shutil.which`; when missing report `Finding(SKIPPED, "<tool> not installed (<what>)")`.
  No network, no writes. Core separately reports staleness against a fresh render.
- **plan** only returns text; it must not run, write or download anything. Core prints a
  header and `cd <root>/hosts/<host>/rendered` first, so write commands relative to the
  rendered directory. Make them exact and copy-pastable, comment lines with `#`, warn about
  destructive steps (which disks are wiped), and use `<placeholders>` for values prov cannot
  know.
- **starter** (optional) returns a complete, valid `host.toml` with synthetic values only;
  `prov init` validates it before writing.

## 4. Register the entry point

Built-in, in this repository's `pyproject.toml`:

```toml
[project.entry-points."prov.distros"]
ubuntu = "prov.distros.ubuntu"
talos = "prov.distros.talos"
```

Third-party package, in its own `pyproject.toml` (then `uv add` it next to prov):

```toml
[project.entry-points."prov.distros"]
talos = "prov_talos"
```

Run `uv sync` so the entry point is installed; `uv run prov distros` should list it. Add a
built-in to `prov.registry.builtin()` too, because the published schema is generated from the
built-ins.

## 5. Schema file, example, goldens, tests

1. Example root with synthetic data only — documentation addresses (192.0.2.0/24,
   198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32), example.com/org, fake ssh keys containing
   `EXAMPLE` (checked by `tests/test_synthetic_data.py`):
   `examples/talos-controlplane/hosts/demo-cp/host.toml`. One example per materially
   different output (layout, role, firmware).
2. `PROV_UPDATE_GOLDENS=1 uv run pytest -q` regenerates `schema/host.v1.schema.json` and
   writes each example's `rendered/` directory; review the diff and commit both. From then
   on `tests/test_goldens.py` and `tests/test_schema.py` pick the example up automatically
   (golden equality, determinism, `validate` without errors, round trip against the
   published schema with the `jsonschema` reference validator).
3. Add `tests/test_talos.py` for what is specific to the distro: each `check` rule, broken
   rendered files that `validate` must catch, the external validator call (monkeypatch
   `shutil.which` and `subprocess.run`), and that `plan` lists the expected commands.
   `tests/test_plan.py` style: monkeypatch `subprocess`/`os` exec functions to fail and
   assert plan still succeeds.
4. Document the distro in the README table.

Checklist: `uv run pytest -q`, `uv run --python 3.10 pytest -q`,
`uv run prov --root examples/talos-controlplane validate demo-cp`, `pre-commit run --all-files`.
