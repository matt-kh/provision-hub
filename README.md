# provision-hub

Render and validate install artefacts for Linux distros (Ubuntu, Proxmox VE, NixOS, Talos) from one host schema

## Overview

- Kind: cli
- Languages: python (uv)
- Notes: prov CLI: versioned host.toml schema, per-distro renderer plugins (render/validate/plan), never applies; synthetic examples only (RFC 5737/3849 IPs, example.com); Python 3.10+ with uv.

## Development

Install the git hooks once per clone:

```sh
pre-commit install
```

## Contributing

Work on a branch and open a pull request against
`main`; direct pushes to it are blocked. Changes are squash-merged.
Tickets are GitHub Issues; mention them as `#N` in the PR body.

## License

MIT — see [LICENSE](LICENSE).
