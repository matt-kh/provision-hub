# 0003. Kind / backend split; the hub champions no backend

## Status

accepted

Decided in: #5 (issue #4). See CHANGELOG.md, Unreleased, "Changed" and "Migration".

## Context

The first release (#3) described one host's OS install in one `host.toml`
(`schema_version = 1`, `distro = "ubuntu"`, an `[ubuntu]` table), with renderers as
"distro" plugins (`prov.distros`). That shape fits OS installs of one family and nothing
else.

provision-hub is meant to be generic across domains: OS installs, OS images, VMs and
Kubernetes distributions. Each of these has several competing tools (autoinstall, NixOS
with disko, Talos; Packer or cloud-image customisation; k3s, RKE2, kubeadm). The format
should let an item change tools without being rewritten, and let new domains arrive without
core changes.

## Decision

Every item is `<root>/<kind>/<name>/spec.toml` and names two things:

- a **kind**: what the item is. Its core keys are a tool-neutral contract
  (`os-install`: hostname, users, network, whole disks) that every backend of the kind
  reads.
- a **backend**: how one tool renders it. The backend owns one table named after itself
  (`[ubuntu-autoinstall]`) for that tool's native input, and nothing else.

The spec starts with the envelope `api_version = "prov/v1alpha1"`, `kind` and `backend`.
Core owns the envelope, the CLI, file writing and the plan header, and never imports a
particular backend; kinds were core modules until #17
([0007](0007-uniform-entry-point-plugins.md)).

provision-hub ships kinds and backends but champions none: a kind's core keys describe the
item, not one tool's input format.

## Alternatives considered

- **The single `host.toml` / `distro` schema** of the first release — replaced, because it
  tied the format to OS installs and to one tool per file shape. Its migration is in
  CHANGELOG.md; a file that still uses it fails with "predates prov/v1alpha1".

No other options were recorded at the time.

## Consequences

- A kind is a shared public format: changing its core keys affects every backend, so such
  changes are reviewed where the kind lives.
- Adding a tool means adding a backend, not changing core
  ([adding-a-backend.md](../adding-a-backend.md)); `prov conformance` checks a backend
  against the kind's tool-neutral fixtures, and a backend lists what it cannot express in
  `UNSUPPORTED_FIXTURES`.
- Core neutrality is tested: `tests/test_kinds.py` and `tests/test_kind_plugins.py` run
  core with fake kinds and backends and scan its imports.
- In #5 kinds were core modules and only backends were plugins; #17 made kinds plugins too
  ([0007](0007-uniform-entry-point-plugins.md)).
- `v1alpha*` formats may still break, always with a Migration paragraph; from 1.0
  `prov/v1` is additive only.

The rules: [kinds.md](../kinds.md) (envelope, kind contract, versioning policy) and
[adding-a-backend.md](../adding-a-backend.md).
