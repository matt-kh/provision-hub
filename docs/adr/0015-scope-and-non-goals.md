# 0015. Scope: a layered catalog, and its non-goals

## Status

accepted

Decided in: the #16 design discussion, delivered as #17.

## Context

After the kind/backend split ([0003](0003-kind-backend-split.md)) the format could describe
more than OS installs. Without a stated scope it could also grow into every neighbouring
tool's job: application deployment, cloud resources, applying and reconciling. Those tools
exist, and most of them keep state, which prov does not.

prov's consumers are mainly AI agents working through an agent harness.

## Decision

provision-hub is a **layered catalog of tool-neutral infrastructure definitions**. Layers
arrive one kind at a time, each with a schema, a verifier and an example before it is
published:

1. `os-install` (shipped);
2. OS images;
3. profile: host software (packages, services, fonts, shell, toolchains) as per-backend
   native sections, with no universal package-name map;
4. layer-zero platform kinds (gitops-controller, git-server, registry), bounded by "what
   must exist before GitOps can manage the rest".

Non-goals:

- Application deployment and Helm charts: GitOps tools (Argo CD, Flux) own them.
- Cloud resources such as DNS, buckets and databases: Terraform/OpenTofu and
  Crossplane cover them, with state and providers.
- Applying anything and day-2 reconciliation: prov renders, humans apply
  ([0001](0001-render-only-never-apply.md)). Stateful tools can only be render targets.
- Agents generating native config per item at render time
  ([0002](0002-deterministic-rendering-agents-as-authors.md)).

## Alternatives considered

Not recorded at the time as a comparison of options.

## Consequences

- A new kind is judged against the layers and the non-goals before it is written.
- Profiles keep each tool's native package names; there is no mapping table to maintain.
- The layer-zero boundary hands over to GitOps; what lies beyond it belongs to other tools.
- Still open from #16: catalog naming, profile merge semantics, and the GitOps hand-off
  contract.
- [kinds.md](../kinds.md#planned-kinds-sketches-not-implemented) sketches the roadmap kinds
  (`os-image`, `profile`, `gitops-controller`, `git-server`, `registry`); their keys will
  change before they land, and each gets its own issue.

The rules: [kinds.md](../kinds.md) and [determinism.md](../determinism.md#outside-prov).
