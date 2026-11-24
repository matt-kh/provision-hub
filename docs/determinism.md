# Determinism

`prov` is a renderer, not an agent: the same `spec.toml` with the same pinned inputs
always renders the same `rendered/` tree, byte for byte, on any machine and at any time.
Reviewers read a diff of `spec.toml` and a diff of `rendered/`, and nothing else can change
what gets installed.

## The guarantee

- `render` is a pure function of the spec and the backend's code: no I/O, no clock, no
  randomness, no network ([adding-a-backend.md](adding-a-backend.md), section 3).
- Output is stable: sorted keys (`prov.util.dump_yaml`, sorted JSON), stable ordering of
  lists the spec does not order, no timestamps, no random ids or generated passwords, text
  only, a final newline and no trailing whitespace.
- Rendering twice is a no-op, and `prov diff` against a fresh render is empty.
- The committed `examples/<kind>/<name>/rendered/` directories are goldens:
  `tests/test_goldens.py` renders every example twice and compares both results with the
  committed files. A golden that changes needs a reason in the PR.

## What is pinned

A rendered tree is only as reproducible as what it points at:

- **Backend-pinned inputs.** A backend fixes the versions of the tools and sources it
  renders references to (nixos-disko: the nixpkgs release from the spec and a disko tag
  fixed in the backend's code). Upgrading a pin is a code change with its own golden diff.
- **Spec-pinned inputs.** Versions the spec names (`release = "nixos-25.11"`, an extra
  flake input `github:example/tool/v1.2.3`) are part of the spec. A backend warns when a
  URL pins no tag or commit.
- **Lock files** (`PRESERVE`). Files a human creates in `rendered/` with the upstream
  tool, such as nixos-disko's `flake.lock`, pin exact commits. `render` keeps them and never
  writes them; they are committed and reviewed like any other file.

## Who authors what

| Part | Author | Changed through |
|---|---|---|
| `spec.toml` | a human, or an agent the human reviews | a PR to the repository that holds the items |
| Kinds and backends | code in prov or a backend package | a reviewed PR with goldens and tests |
| `rendered/` | `prov render`, never by hand | committed with the spec change that produced it |
| Escape-hatch fragments (below) | a human, or an agent the human reviews | a PR to the repository that holds the items |
| Lock files (`PRESERVE`) | the upstream tool, run by a human | committed next to the rendered files |

Nothing is generated at render time by an agent, a model or a network service. When an
item needs something no backend can express, the fix is a spec key or a backend change,
reviewed as code, or a host-owned fragment.

## Escape-hatch fragments

Some details belong in the target's own language rather than in a spec key. A backend may
let an item import a hand-written fragment, following the nixos-disko `local.nix` pattern
([backends/nixos-disko.md](backends/nixos-disko.md), "Host-owned module and extra inputs"):

- The fragment is **host-owned**: it sits next to `spec.toml`, outside `rendered/`, and is
  committed with the item. `render` never writes, copies into `rendered/` or deletes it.
- The spec opts in explicitly (`local_module = true`), so the rendered files and their
  goldens say that the fragment is imported.
- The backend checks it: the rendered artefacts require the file once the spec opts in
  (nixos-disko stops the build when `../local.nix` is missing or not git-tracked), and the
  flake inputs the fragment uses are pinned by the same lock as everything else.

A fragment is the place for what is specific to one host; anything several items need
belongs in the spec format or a backend.

## Outside `prov`

What depends on a live target is not deterministic and stays outside `prov`, done by a
human with the commands `prov plan` prints:

- **Apply**: writing disks, booting installers, creating VMs, `nixos-rebuild switch`.
- **Behavioural tests**: installing in a VM or on hardware and checking that the result
  boots and works. `prov validate` checks files statically; it never claims an artefact
  works.

## Kinds and backends

Kinds and backends are both plugins ([kinds.md](kinds.md#kind-plugins),
[adding-a-backend.md](adding-a-backend.md)), shipped by provision-hub or by a third-party
package. Whoever writes either carries the same guarantee, and proves it the same way:
examples are goldens, rendered twice and compared with the committed files.
