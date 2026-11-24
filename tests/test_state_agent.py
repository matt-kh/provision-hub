"""[state.report] and the nixos-disko report-only state agent (docs/state.md).

Core checks (kind-neutral), the rendered module and script, and the agent script itself run
against a local bare git repository and fake system paths. The agent is run by these tests
only: prov never runs it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from prov.backends import nixos_disko as nixos
from prov.backends import ubuntu_autoinstall as ubuntu
from prov.registry import Context, discover
from prov.schema import tomllib
from prov.spec import check_spec
from prov.state import load_record, render_digest
from prov.validate import ERROR, WARNING

# Looked up at import: the autouse conftest fixture stubs shutil.which.
BASH, GIT, JQ, NIX = (shutil.which(t) for t in ("bash", "git", "jq", "nix-instantiate"))
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
HOST_KEY = "git.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE"
REPORT = {"repo": "git@git.example.com:example/hosts-state.git", "known_hosts": [HOST_KEY]}
DIGEST = "sha256:" + "ab" * 32


def nixos_raw(report=None, **state):
    raw = tomllib.loads(nixos.starter("demo"))
    if report is not None or state:
        raw["state"] = dict(state)
        if report is not None:
            raw["state"]["report"] = report
    return raw


def check(raw):
    return check_spec(raw, "os-install", "demo", discover())


def messages(findings, level=ERROR):
    return [f"{f.where}: {f.message}" for f in findings if f.level == level]


def report(**edits):
    out = dict(REPORT)
    out.update(edits)
    return {k: v for k, v in out.items() if v is not None}


# --- core checks --------------------------------------------------------------------------


def test_report_is_valid_for_nixos_with_defaults():
    spec, findings = check(nixos_raw(report()))
    assert spec is not None, findings
    assert spec["state"]["report"] == {
        **REPORT,
        "branch": "main",
        "interval": "15m",
        "heartbeat": "6h",
        "identity_file": "/var/lib/prov-state/id_ed25519",
    }
    assert not messages(findings, WARNING)


def test_report_is_refused_for_a_backend_without_an_agent():
    raw = tomllib.loads(ubuntu.starter("demo"))
    raw["state"] = {"report": report()}
    spec, findings = check(raw)
    assert spec is None
    assert any("does not render a state agent" in m for m in messages(findings)), findings


@pytest.mark.parametrize(
    "edit, needle",
    [
        ({"repo": "-oProxyCommand=x@git.example.com:a"}, "repo"),
        ({"repo": "git@git.example.com:-a"}, "repo"),
        ({"repo": "https://git.example.com/a.git"}, "repo"),
        ({"known_hosts": []}, "known_hosts"),
        ({"known_hosts": ["git.example.com ssh-ed25519 AAAAEXAMPLE comment"]}, "known_hosts"),
        ({"known_hosts": ["@cert-authority *.example.com ssh-ed25519 AAAAEXAMPLE"]}, "known_hosts"),
        ({"branch": "../main"}, "branch"),
        ({"interval": "0m"}, "interval"),
        ({"interval": "15 m"}, "interval"),
        ({"identity_file": "relative/key"}, "identity_file"),
        ({"identity_file": "/var/lib/a key"}, "identity_file"),
        ({"unknown": 1}, "unknown"),
    ],
)
def test_schema_refuses_bad_report_values(edit, needle):
    spec, findings = check(nixos_raw(report(**edit)))
    assert spec is None
    assert any(needle in m for m in messages(findings)), findings


def test_heartbeat_shorter_than_interval_is_an_error():
    _, findings = check(nixos_raw(report(interval="1h", heartbeat="30m")))
    assert any("heartbeat 30m is shorter than interval 1h" in m for m in messages(findings))


def test_heartbeat_at_the_stale_threshold_warns():
    spec, findings = check(nixos_raw(report(heartbeat="3d")))
    assert spec is not None
    assert any("stale" in m for m in messages(findings, WARNING))


@pytest.mark.parametrize(
    "repo, line, ok",
    [
        ("git@git.example.com:a/b.git", HOST_KEY, True),
        ("git@GIT.example.com:a/b.git", HOST_KEY, True),
        ("git@other.example.com:a/b.git", HOST_KEY, False),
        ("git@git.example.com:a/b.git", "*.example.com ssh-ed25519 AAAAEXAMPLE", True),
        ("git@git.example.com:a/b.git", "other.example.com,git.example.com ssh-ed25519 AAAAEXAMPLE", True),
        ("ssh://git@git.example.com:2222/a/b.git", HOST_KEY, False),
        ("ssh://git@git.example.com:2222/a/b.git", "[git.example.com]:2222 ssh-ed25519 AAAAEXAMPLE", True),
        ("ssh://git@git.example.com/a/b.git", HOST_KEY, True),
        ("git@git.example.com:a/b.git", "|1|EXAMPLE=|EXAMPLE= ssh-ed25519 AAAAEXAMPLE", True),
    ],
)
def test_known_hosts_must_name_the_repository_host(repo, line, ok):
    spec, findings = check(nixos_raw(report(repo=repo, known_hosts=[line])))
    assert (spec is not None) is ok, findings
    if not ok:
        assert any("no known_hosts line names" in m for m in messages(findings))


def test_decommissioned_alone_does_not_change_the_render():
    plain, _ = check(nixos_raw())
    retired, _ = check(nixos_raw(decommissioned=True))
    assert nixos.render(plain, Context("demo")) == nixos.render(retired, Context("demo"))


def test_fake_backend_without_state_agent_is_refused():
    class Fake:
        NAME = "fake"

    from prov.state import report_findings

    findings = report_findings({"state": {"report": {}}}, Fake)
    assert [f.level for f in findings] == [ERROR]


# --- render, validate, plan ---------------------------------------------------------------


@pytest.fixture
def spec():
    out, findings = check(nixos_raw(report()))
    assert out is not None, findings
    return out


@pytest.fixture
def rendered(tmp_path, spec):
    for name, text in nixos.render(spec, Context("demo")).items():
        (tmp_path / name).write_text(text)
    return tmp_path


def test_render_adds_module_and_script_only_with_report(spec):
    files = nixos.render(spec, Context("demo"))
    assert sorted(files) == sorted(nixos.FILES + nixos.STATE_FILES)
    assert files[nixos.STATE_SCRIPT] == (Path(nixos.__file__).parent / "nixos_disko_agent.sh").read_text()
    plain, _ = check(nixos_raw())
    assert sorted(nixos.render(plain, Context("demo"))) == sorted(nixos.FILES)


def test_rendered_wiring(spec):
    files = nixos.render(spec, Context("demo"))
    conf, inst, module = files["configuration.nix"], files["installer.nix"], files[nixos.STATE_MODULE]
    assert "++ [ ./prov-state.nix ]" in conf
    for name in nixos.STATE_FILES:
        assert f'cp ${{./{name}}} "$out/{name}"' in inst
    assert 'PROV_STATE_REF = "os-install/demo";' in module
    assert 'OnUnitActiveSec = "15m";' in module
    assert f'"{HOST_KEY}"' in module
    assert "id_ed25519" in module and "BEGIN" not in module


def test_validate_accepts_the_render(rendered):
    assert not messages(nixos.validate(rendered))


@pytest.mark.parametrize("missing", nixos.STATE_FILES)
def test_validate_needs_both_state_files(rendered, missing):
    (rendered / missing).unlink()
    assert messages(nixos.validate(rendered))


def test_validate_flags_an_import_without_the_module(tmp_path):
    plain, _ = check(nixos_raw())
    files = nixos.render(plain, Context("demo"))
    files["configuration.nix"] = files["configuration.nix"].replace("++ hardware;", "++ hardware ++ [ ./prov-state.nix ];")
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    assert any("prov-state.nix, which is missing" in m for m in messages(nixos.validate(tmp_path)))


def test_validate_flags_a_relaxed_host_key_check(rendered):
    script = rendered / nixos.STATE_SCRIPT
    script.write_text(script.read_text().replace("StrictHostKeyChecking=yes", "StrictHostKeyChecking=accept-new"))
    assert any("StrictHostKeyChecking=yes" in m for m in messages(nixos.validate(rendered)))


def test_plan_prints_the_deploy_key_step(spec):
    lines = nixos.plan(spec, Context("demo"))
    assert "sudo ssh-keygen -t ed25519 -N '' -C 'prov-state os-install/demo' -f /var/lib/prov-state/id_ed25519" in lines
    assert "sudo systemctl start prov-state-agent" in lines
    plain, _ = check(nixos_raw())
    assert not any("prov-state" in line for line in nixos.plan(plain, Context("demo")))


@pytest.mark.skipif(NIX is None, reason="nix-instantiate not installed")
def test_nix_digest_equals_prov_digest(rendered):
    """The module's evaluation-time digest is prov digest (installer copies skipped)."""
    (rendered / "flake.lock").write_text('{"nodes": {}, "root": "root", "version": 7}\n')
    expected = render_digest(rendered)
    (rendered / "hardware-configuration.installer-copy.nix").write_text("{ }\n")
    expr = (
        "let lib = {"
        " filterAttrs = f: s: builtins.listToAttrs (map (n: { name = n; value = s.${n}; })"
        " (builtins.filter (n: f n s.${n}) (builtins.attrNames s)));"
        " hasSuffix = x: s: let a = builtins.stringLength x; b = builtins.stringLength s;"
        " in b >= a && builtins.substring (b - a) a s == x;"
        ' concatMapStrings = f: l: builtins.concatStringsSep "" (map f l); };'
        f" m = import {rendered}/prov-state.nix {{ inherit lib; pkgs = {{ writeShellApplication = x: x; }}; }};"
        ' in m.environment.etc."prov/render-digest".text'
    )
    out = subprocess.run([NIX, "--eval", "--strict", "--json", "-E", expr], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == expected + "\n"


# --- the agent script ---------------------------------------------------------------------

needs_tools = pytest.mark.skipif(None in (BASH, GIT, JQ), reason="bash, git and jq are needed to run the agent")


class Host:
    """A fake NixOS host for the agent: system profiles, /run links, /proc files, a fake ip."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = tmp / "root"
        self.store = tmp / "store"
        self.profiles = self.root / "nix/var/nix/profiles"
        self.profiles.mkdir(parents=True)
        (self.root / "run").mkdir()
        kernel = self.root / "proc/sys/kernel"
        kernel.mkdir(parents=True)
        (kernel / "hostname").write_text("demo\n")
        (kernel / "osrelease").write_text("6.12.0\n")
        for gen in (1, 2):
            system = self.store / f"system-{gen}"
            (system / "etc/prov").mkdir(parents=True)
            (system / "etc/prov/render-digest").write_text(DIGEST + "\n")
            (system / "nixos-version").write_text("26.05.20261122.0dc498b (Example)\n")
            for part in ("kernel", "initrd", "kernel-modules"):
                (system / part).write_text(f"{part}-{gen}\n")
            (self.profiles / f"system-{gen}-link").symlink_to(system)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        ip = self.bin / "ip"
        ip.write_text(
            "#!/bin/sh\n"
            'echo \'[{"ifname":"lan0","addr_info":[{"local":"192.0.2.10","prefixlen":24},{"local":"2001:db8::10","prefixlen":64}]}]\'\n'
        )
        ip.chmod(0o755)
        self.remote = tmp / "state.git"
        subprocess.run([GIT, "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        self.key = tmp / "id_ed25519"
        self.key.write_text("PRIVATE-KEY-MATERIAL-EXAMPLE\n")
        self.known = tmp / "known_hosts"
        self.known.write_text(HOST_KEY + "\n")
        self.state_dir = tmp / "agent"
        self.set_generation(2, current=2, booted=2)

    def link(self, path: Path, target: Path):
        if path.is_symlink():
            path.unlink()
        path.symlink_to(target)

    def set_generation(self, profile: int, current: int, booted: int):
        self.link(self.profiles / "system", Path(f"system-{profile}-link"))
        self.link(self.root / "run/current-system", self.store / f"system-{current}")
        self.link(self.root / "run/booted-system", self.store / f"system-{booted}")

    def env(self, **extra):
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "PROV_STATE_REF": "os-install/demo",
            "PROV_STATE_REPO": str(self.remote),
            "PROV_STATE_BRANCH": "main",
            "PROV_STATE_HEARTBEAT": "6h",
            "PROV_STATE_IDENTITY": str(self.key),
            "PROV_STATE_KNOWN_HOSTS": str(self.known),
            "PROV_STATE_DIR": str(self.state_dir),
            "PROV_STATE_ROOT": str(self.root),
        }
        env.update(extra)
        return env

    def run(self, **extra):
        script = Path(nixos.__file__).parent / "nixos_disko_agent.sh"
        return subprocess.run([BASH, str(script)], env=self.env(**extra), capture_output=True, text=True)

    def commits(self) -> int:
        out = subprocess.run([GIT, "-C", str(self.remote), "rev-list", "--count", "main"], capture_output=True, text=True)
        return int(out.stdout) if out.returncode == 0 else 0

    def record(self):
        checkout = self.tmp / f"checkout-{time.monotonic_ns()}"
        subprocess.run([GIT, "clone", "-q", str(self.remote), str(checkout)], check=True)
        record, findings = load_record(checkout, "os-install", "demo")
        assert record is not None, findings
        return record, checkout


@pytest.fixture
def host_(tmp_path):
    return Host(tmp_path)


@needs_tools
def test_agent_writes_a_valid_record(host_):
    out = host_.run()
    assert out.returncode == 0, out.stderr
    assert "pushed" in out.stdout
    record, _ = host_.record()
    assert record["api_version"] == "prov.state/v1alpha1"
    assert record["ref"] == "os-install/demo"
    assert record["render_digest"] == DIGEST
    assert record["generation"] == "2"
    assert record["drift"] == {"status": "clean"}
    assert record["agent_version"] == "prov-state-agent/1"
    assert record["facts"] == {
        "hostname": "demo",
        "kernel": "6.12.0",
        "nixos_version": "26.05.20261122.0dc498b (Example)",
        "reboot_pending": False,
        "addresses": ["192.0.2.10/24", "2001:db8::10/64"],
    }
    assert "applied_rev" not in record


@needs_tools
def test_agent_skips_unchanged_records_until_the_heartbeat(host_):
    assert host_.run().returncode == 0
    again = host_.run()
    assert again.returncode == 0 and "unchanged" in again.stdout
    assert host_.commits() == 1
    time.sleep(1.1)
    beat = host_.run(PROV_STATE_HEARTBEAT="1s")
    assert beat.returncode == 0 and "pushed" in beat.stdout, beat.stderr
    assert host_.commits() == 2


@needs_tools
def test_agent_pushes_a_change_at_once(host_):
    assert host_.run().returncode == 0
    host_.set_generation(2, current=2, booted=1)
    assert host_.run().returncode == 0
    assert host_.commits() == 2
    record, _ = host_.record()
    assert record["facts"]["reboot_pending"] is True


@needs_tools
@pytest.mark.parametrize(
    "profile, current, status, detail",
    [
        (2, 1, "drifted", "not the system profile"),
        (1, 1, "drifted", "rolled back: generation 1 is active, generation 2 is the newest"),
    ],
)
def test_agent_reports_drift(host_, profile, current, status, detail):
    host_.set_generation(profile, current=current, booted=current)
    assert host_.run().returncode == 0
    record, _ = host_.record()
    assert record["drift"]["status"] == status
    assert detail in record["drift"]["detail"]


@needs_tools
def test_agent_reports_unknown_without_a_digest(host_):
    (host_.store / "system-2/etc/prov/render-digest").unlink()
    assert host_.run().returncode == 0
    record, _ = host_.record()
    assert record["drift"]["status"] == "unknown"
    assert "render_digest" not in record


@needs_tools
def test_agent_keeps_other_hosts_records(host_, tmp_path):
    seed = tmp_path / "seed"
    subprocess.run([GIT, "clone", "-q", str(host_.remote), str(seed)], check=True, capture_output=True)
    (seed / "os-install").mkdir()
    (seed / "os-install/other.json").write_text("{}\n")
    git = [GIT, "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "other"], check=True)
    subprocess.run(git + ["push", "-q", "origin", "HEAD:main"], check=True)
    assert host_.run().returncode == 0
    _, checkout = host_.record()
    assert (checkout / "os-install/other.json").exists()


@needs_tools
def test_agent_replaces_unsafe_characters(host_):
    (host_.root / "proc/sys/kernel/hostname").write_text("de\x1b[31mmo‮\n")
    assert host_.run().returncode == 0
    record, _ = host_.record()
    assert record["facts"]["hostname"] == "de?[31mmo?"


@needs_tools
@pytest.mark.parametrize("missing, message", [("known", "refusing to connect"), ("key", "deploy key")])
def test_agent_refuses_without_known_hosts_or_key(host_, missing, message):
    getattr(host_, missing).unlink()
    out = host_.run()
    assert out.returncode == 1 and message in out.stderr
    assert host_.commits() == 0


@needs_tools
def test_agent_never_prints_or_stores_the_key(host_):
    out = host_.run()
    assert out.returncode == 0
    _, checkout = host_.record()
    stored = "".join(p.read_text() for p in checkout.rglob("*.json"))
    assert "PRIVATE-KEY-MATERIAL" not in out.stdout + out.stderr + stored


@needs_tools
@pytest.mark.parametrize("ref", ["demo", "os-install/../x", "a/b/c", "/abs"])
def test_agent_refuses_a_bad_ref(host_, ref):
    out = host_.run(PROV_STATE_REF=ref)
    assert out.returncode == 1 and "PROV_STATE_REF" in out.stderr


def test_example_renders_the_agent():
    rendered = EXAMPLES / "os-install/demo-nixos-state/rendered"
    assert (rendered / nixos.STATE_SCRIPT).read_text() == (Path(nixos.__file__).parent / "nixos_disko_agent.sh").read_text()
