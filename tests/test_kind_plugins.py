"""Kind plugins: a third-party dotted kind and its backend, installed only through fake entry points."""

from __future__ import annotations

import ast
import json
import os
import types
from pathlib import Path

import jsonschema
import pytest
from conftest import PROVISION_HUB, REPO, fake_entry_point

from prov import kinds, plugin, registry
from prov.cli import main
from prov.registry import check_contract, discover
from prov.schema import ConfigError, api_versions, kind_group, schema_id
from prov.spec import parse_ref

KIND = "registry.example.org"
BACKEND = "example-registry-text"


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def clean(text):
    return all(ch.isprintable() or ch in "\n\t" for ch in text)


def fake_kind(name=KIND, **extra):
    """A complete third-party kind: a container registry of some size."""
    kind = types.ModuleType(name)
    kind.__doc__ = "A test-only kind: a container registry."
    kind.NAME = name
    kind.PLUGIN_API = 1
    kind.VERSIONS = ("v1alpha1",)
    kind.PROPERTIES = {
        "hostname": {"type": "string", "pattern": "^[a-z0-9.-]+$", "description": "Registry host name."},
        "replicas": {"type": "integer", "minimum": 1, "default": 1},
    }
    kind.REQUIRED = ["hostname"]
    kind.REFS = {}

    def semantic(spec):
        from prov.validate import ERROR, Finding

        return [Finding(ERROR, "replicas must be odd", "replicas")] if spec["replicas"] % 2 == 0 else []

    kind.semantic = semantic
    for key, value in extra.items():
        setattr(kind, key, value)
    return kind


def fake_backend(name=BACKEND, kind=KIND, **extra):
    mod = types.ModuleType(name)
    mod.__doc__ = "Renders a registry as a text file."
    mod.NAME = name
    mod.KIND = kind
    mod.SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 5000}},
    }
    mod.render = lambda spec, ctx: {"registry.txt": f"{spec['hostname']}:{spec[name]['port']} x{spec['replicas']} ({ctx.name})\n"}
    mod.validate = lambda rendered_dir: []
    mod.plan = lambda spec, ctx: ["# start the registry", "cat registry.txt"]
    mod.starter = lambda item: (
        f'api_version = "example.org/v1alpha1"\nkind = "{kind}"\nbackend = "{name}"\n'
        f'hostname = "{item}.example.org"\n\n[{name}]\nport = 5001\n'
    )
    for key, value in extra.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture
def registry_kind(extra_kinds, extra_backends):
    """The third-party kind and backend, registered through (fake) entry points only."""
    kind, backend = fake_kind(), fake_backend()
    extra_kinds(fake_entry_point(kind, value="prov_registry.kind"))
    extra_backends(fake_entry_point(backend, value="prov_registry.text"))
    return kind, backend


# --- the full flow through entry points only ----------------------------------------------


def test_dotted_kind_full_flow(tmp_path, capsys, registry_kind):
    root = str(tmp_path)
    code, out, _ = run(capsys, "kinds")
    assert code == 0 and out.splitlines() == [
        "os-install\tprov/v1alpha1\tOne machine's operating-system install: host identity, users, network and whole disks.",
        f"{KIND}\texample.org/v1alpha1\tA test-only kind: a container registry.",
    ]
    assert run(capsys, "backends", "--kind", KIND) == (0, f"{BACKEND}\t{KIND}\tRenders a registry as a text file.\n", "")

    assert run(capsys, "--root", root, "init", f"{KIND}/hub", "--backend", BACKEND)[0] == 0
    assert (tmp_path / KIND / "hub/spec.toml").is_file()
    code, out, _ = run(capsys, "--root", root, "render", f"{KIND}/hub")
    assert code == 0 and "wrote registry.txt" in out
    assert (tmp_path / KIND / "hub/rendered/registry.txt").read_text() == "hub.example.org:5001 x1 (hub)\n"
    assert run(capsys, "--root", root, "validate", f"{KIND}/hub") == (0, f"ok: {KIND}/hub\n", "")
    assert run(capsys, "--root", root, "diff", f"{KIND}/hub") == (0, f"{KIND}/hub: rendered output is up to date\n", "")
    code, out, _ = run(capsys, "--root", root, "plan", f"{KIND}/hub")
    assert code == 0 and out.splitlines()[0].startswith(f"# prov plan for {KIND}/hub ({BACKEND})")
    assert out.splitlines()[-1] == "cat registry.txt"
    assert run(capsys, "--root", root, "list") == (0, f"{KIND}/hub\t{BACKEND}\n", "")

    spec = tmp_path / KIND / "hub/spec.toml"
    spec.write_text(spec.read_text().replace('hostname = "hub.example.org"', 'hostname = "hub.example.org"\nreplicas = 2'))
    code, _, err = run(capsys, "--root", root, "validate", f"{KIND}/hub")
    assert code == 1 and "error: replicas: replicas must be odd" in err


def test_dotted_kind_schema(capsys, registry_kind):
    code, out, _ = run(capsys, "schema", KIND)
    assert code == 0
    schema = json.loads(out)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$id"] == f"urn:prov:schema:{KIND}:v1alpha1"
    assert schema["properties"]["api_version"] == {"const": "example.org/v1alpha1", "description": "Spec format version; required."}
    assert schema["properties"]["kind"]["const"] == KIND
    assert schema["properties"]["backend"]["enum"] == [BACKEND]
    good = {"api_version": "example.org/v1alpha1", "kind": KIND, "backend": BACKEND, "hostname": "r.example.org", BACKEND: {}}
    jsonschema.Draft202012Validator(schema).validate(good)
    assert not jsonschema.Draft202012Validator(schema).is_valid(dict(good, api_version="prov/v1alpha1"))


def test_schema_url_is_a_base_url(extra_kinds, capsys):
    extra_kinds(fake_entry_point(fake_kind(SCHEMA_URL="https://schemas.example.org/prov/")))
    assert json.loads(run(capsys, "schema", KIND)[1])["$id"] == f"https://schemas.example.org/prov/{KIND}.v1alpha1.schema.json"


@pytest.mark.parametrize(
    "url",
    ["https://schemas.example.org/prov", "http://schemas.example.org/", "https://schemas.example.org/a b/", "https:///", "urn:example:x/", 42],
)
def test_schema_url_must_be_an_https_base(url):
    with pytest.raises(ConfigError, match="SCHEMA_URL must be an https base URL ending in '/'"):
        kinds.check_kind(fake_kind(SCHEMA_URL=url))


def test_builtin_kind_keeps_its_group_and_id():
    os_install = kinds.get("os-install")
    assert api_versions(os_install) == ["prov/v1alpha1"]
    assert schema_id(os_install) == "https://raw.githubusercontent.com/matt-kh/provision-hub/main/schema/os-install.v1alpha1.schema.json"
    assert kind_group("os-install") == "prov" and kind_group(KIND) == "example.org"


@pytest.mark.parametrize("verb", ["render", "validate", "diff", "plan"])
def test_prov_group_is_rejected_for_a_dotted_kind(tmp_path, capsys, registry_kind, verb):
    assert run(capsys, "--root", str(tmp_path), "init", f"{KIND}/hub", "--backend", BACKEND)[0] == 0
    spec = tmp_path / KIND / "hub/spec.toml"
    spec.write_text(spec.read_text().replace("example.org/v1alpha1", "prov/v1alpha1"))
    code, _, err = run(capsys, "--root", str(tmp_path), verb, f"{KIND}/hub")
    assert code == 2 and f"unsupported api_version 'prov/v1alpha1' for kind '{KIND}' (this prov supports: example.org/v1alpha1)" in err


@pytest.mark.parametrize("name", ["registry.prov", "registry.example", "a.b"])
def test_group_must_be_dotted(name):
    with pytest.raises(ConfigError, match=f"NAME '{name}' needs a dotted group \\(Kubernetes CRD style\\)"):
        kinds.check_kind(fake_kind(name))


# --- discovery rules -----------------------------------------------------------------------


LONG_KIND = "registry." + ".".join(["a" * 60] * 4) + ".example.org"  # pattern-valid, over 253 characters

BAD_KINDS = {
    "bare_third_party_name": (lambda: fake_entry_point(fake_kind("registry"), value="prov_registry"), "bare kind names are reserved for the provision-hub distribution"),
    "entry_point_name_differs": (lambda: fake_entry_point(fake_kind(), name="hub.example.org", value="prov_registry"), f"entry-point name differs from NAME '{KIND}'"),
    "plugin_api_missing": (lambda: fake_entry_point(_without(fake_kind(), "PLUGIN_API"), value="prov_registry"), "lacks required member(s): PLUGIN_API"),
    "plugin_api_2": (lambda: fake_entry_point(fake_kind(PLUGIN_API=2), value="prov_registry"), "PLUGIN_API 2 is not supported (this prov supports PLUGIN_API = 1)"),
    "plugin_api_str": (lambda: fake_entry_point(fake_kind(PLUGIN_API="1"), value="prov_registry"), "PLUGIN_API '1' is not supported"),
    "plugin_api_bool": (lambda: fake_entry_point(fake_kind(PLUGIN_API=True), value="prov_registry"), "PLUGIN_API True is not supported"),
    "group_not_dotted": (lambda: fake_entry_point(fake_kind("registry.example"), value="prov_registry"), "NAME 'registry.example' needs a dotted group (Kubernetes CRD style)"),
    "name_too_long": (lambda: fake_entry_point(fake_kind(LONG_KIND), value="prov_registry"), "at most 253 characters"),
    "bad_name": (lambda: fake_entry_point(fake_kind("Registry.example.org"), value="prov_registry"), "NAME must match"),
    "bad_schema_url": (lambda: fake_entry_point(fake_kind(SCHEMA_URL="not a url"), value="prov_registry"), "SCHEMA_URL must be an https base URL"),
}


def _without(obj, member):
    delattr(obj, member)
    return obj


@pytest.mark.parametrize("case", sorted(BAD_KINDS))
@pytest.mark.parametrize("argv", [["kinds"], ["backends"], ["schema", "os-install"]])
def test_bad_kind_plugins_exit_2_with_one_line(extra_kinds, capsys, case, argv):
    make, needle = BAD_KINDS[case]
    extra_kinds(make())
    code, out, err = run(capsys, *argv)
    assert (code, out) == (2, "") and needle in err and err.count("\n") == 1 and clean(err), err
    assert "kind plugin" in err


def test_duplicate_kind_is_rejected(extra_kinds, capsys):
    extra_kinds(fake_entry_point(fake_kind(), value="pkg_a"), fake_entry_point(fake_kind(), value="pkg_b"))
    code, _, err = run(capsys, "kinds")
    assert code == 2 and f"kind '{KIND}' is registered twice: {KIND} = pkg_a and {KIND} = pkg_b" in err


def test_third_party_cannot_shadow_a_builtin_kind(extra_kinds, capsys):
    shadow = fake_kind("os-install")
    extra_kinds(fake_entry_point(shadow, value="evil"))
    code, _, err = run(capsys, "kinds")
    assert code == 2 and "bare kind names are reserved" in err


def test_unloadable_kind_plugin_exits_2(extra_kinds, capsys):
    def load():
        raise ImportError("no module named prov_registry\x1b[2J")

    extra_kinds(types.SimpleNamespace(name=KIND, value="prov_registry", load=load, dist=None))
    code, _, err = run(capsys, "kinds")
    assert code == 2 and f"cannot load kind plugin {KIND} = prov_registry: no module named prov_registry\\x1b[2J" in err


def test_bare_kind_from_provision_hub_is_accepted(extra_kinds, capsys):
    extra_kinds(fake_entry_point(fake_kind("widget"), dist=PROVISION_HUB))
    assert "widget\tprov/v1alpha1\t" in run(capsys, "kinds")[1]
    assert sorted(kinds.builtin()) == ["os-install", "widget"]


@pytest.mark.parametrize("case", ["bare_third_party_name", "plugin_api_2", "bad_name"])
def test_builtin_kinds_ignore_broken_third_party_kinds(extra_kinds, case):
    extra_kinds(BAD_KINDS[case][0]())
    assert list(kinds.builtin()) == ["os-install"]
    with pytest.raises(ConfigError):
        kinds.discover()


def test_builtin_kinds_are_checked(extra_kinds):
    extra_kinds(fake_entry_point(fake_kind("widget", PLUGIN_API=2), dist=PROVISION_HUB))
    with pytest.raises(ConfigError, match="PLUGIN_API 2 is not supported"):
        kinds.builtin()


def test_kind_name_length_limit():
    assert len(LONG_KIND) > 253
    with pytest.raises(ConfigError, match="at most 253 characters"):
        kinds.check_kind(fake_kind(LONG_KIND))


def test_no_kinds_installed_hints_at_the_installation(monkeypatch, capsys):
    monkeypatch.setattr(kinds, "entry_points", lambda group: [])
    kinds.reset()
    code, _, err = run(capsys, "schema", "os-install")
    assert code == 2 and "unknown kind 'os-install' (known: none; is provision-hub installed? run uv sync)" in err


@pytest.mark.parametrize("dist", ["provision-hub", "provision_hub", "Provision.Hub", "PROVISION__HUB"])
def test_distribution_names_are_normalised(dist):
    assert plugin.from_distribution(fake_entry_point(fake_kind(), dist=dist))


@pytest.mark.parametrize("dist", [None, "provision-hub-extras", "prov"])
def test_other_distributions_are_third_party(dist):
    assert not plugin.from_distribution(fake_entry_point(fake_kind(), dist=dist))


def test_builtins_come_from_the_provision_hub_distribution(registry_kind):
    assert list(kinds.builtin()) == ["os-install"]
    assert list(registry.builtin()) == ["nixos-disko", "ubuntu-autoinstall"]
    assert KIND in kinds.discover() and BACKEND in discover()


def test_discovery_is_cached_until_reset(extra_kinds):
    first = kinds.discover()
    assert kinds.discover() == first
    extra_kinds(fake_entry_point(fake_kind()))  # the fixture resets the cache
    assert KIND in kinds.discover()


# --- backends of plugin kinds --------------------------------------------------------------


@pytest.mark.parametrize("api,ok", [(1, True), (2, False), ("1", False), (True, False), (None, False)])
def test_backend_plugin_api_is_optional_but_checked(registry_kind, api, ok):
    backend = fake_backend(PLUGIN_API=api)
    if ok:
        check_contract(backend, "x = tests")
    else:
        with pytest.raises(ConfigError, match="is not supported \\(this prov supports PLUGIN_API = 1\\)"):
            check_contract(backend, "x = tests")
    check_contract(_without(fake_backend(PLUGIN_API=1), "PLUGIN_API"), "x = tests")


def test_builtin_plugins_declare_plugin_api():
    for mod in list(kinds.builtin().values()) + list(registry.builtin().values()):
        assert mod.PLUGIN_API == plugin.PLUGIN_API, mod.NAME


def test_backend_name_collides_only_with_its_own_kind(registry_kind):
    # 'replicas' is a core property of the registry kind, not of os-install
    check_contract(fake_backend("replicas", kind="os-install"), "replicas = tests")
    with pytest.raises(ConfigError, match=f"NAME 'replicas' collides with a core property of kind '{KIND}'"):
        check_contract(fake_backend("replicas"), "replicas = tests")
    with pytest.raises(ConfigError, match="collides with a core property of the spec envelope"):
        check_contract(fake_backend("backend"), "backend = tests")


def test_backend_of_an_uninstalled_dotted_kind_is_rejected(extra_backends, capsys):
    extra_backends(fake_entry_point(fake_backend()))
    code, _, err = run(capsys, "backends")
    assert code == 2 and f"unknown KIND '{KIND}' (known kinds: os-install)" in err


# --- a raising or misbehaving kind ---------------------------------------------------------


HOSTILE = "ev\x1b[2Jil\nerror: forged‮"


@pytest.mark.parametrize(
    "semantic,needle",
    [
        (lambda spec: (_ for _ in ()).throw(RuntimeError(HOSTILE)), f"kind '{KIND}' failed in semantic: RuntimeError: ev\\x1b[2Jil\\x0aerror: forged\\u202e"),
        (lambda spec: (_ for _ in ()).throw(ConfigError(HOSTILE)), f"kind '{KIND}' failed in semantic: ev\\x1b[2Jil\\x0aerror: forged\\u202e"),
        (lambda spec: None, f"kind '{KIND}' failed in semantic: returned NoneType, expected a list of prov.validate.Finding"),
        (lambda spec: ["no"], f"kind '{KIND}' failed in semantic: returned list, expected a list of prov.validate.Finding"),
    ],
)
@pytest.mark.parametrize("verb", ["render", "validate", "diff", "plan"])
def test_misbehaving_semantic_exits_2_escaped(tmp_path, capsys, extra_kinds, extra_backends, semantic, needle, verb):
    extra_kinds(fake_entry_point(fake_kind(semantic=semantic)))
    extra_backends(fake_entry_point(fake_backend()))
    item = tmp_path / KIND / "hub"
    item.mkdir(parents=True)
    (item / "spec.toml").write_text(fake_backend().starter("hub"))
    code, out, err = run(capsys, "--root", str(tmp_path), verb, f"{KIND}/hub")
    assert code == 2 and needle in err and err.count("\n") == 1 and clean(out + err), err


# --- refs and list -------------------------------------------------------------------------


def test_parse_ref_accepts_a_dotted_kind(registry_kind):
    assert parse_ref(f"{KIND}/hub-1") == (KIND, "hub-1")


@pytest.mark.parametrize("verb", ["render", "validate", "init"])
def test_over_long_kind_in_a_ref_is_unknown_not_an_os_error(tmp_path, capsys, verb):
    argv = [verb, f"{LONG_KIND}/x"] + (["--backend", BACKEND] if verb == "init" else [])
    code, out, err = run(capsys, "--root", str(tmp_path), *argv)
    assert (code, out) == (2, "") and "unknown kind 'registry." in err and err.count("\n") == 1
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize(
    "ref",
    ["registry.example/x", "registry..example.org/x", ".example.org/x", "registry.example.org./x", "Registry.example.org/x", "registry.-example.org/x", "registry.example_org/x", "1registry.example.org/x"],
)
def test_parse_ref_rejects_malformed_dotted_kinds(ref):
    with pytest.raises(ConfigError, match="invalid ref"):
        parse_ref(ref)


def test_parse_ref_of_an_uninstalled_dotted_kind(registry_kind):
    with pytest.raises(ConfigError, match=f"unknown kind 'other.example.org' \\(known: os-install, {KIND}\\)"):
        parse_ref("other.example.org/x")


def test_list_warns_about_a_dotted_directory_without_an_installed_kind(tmp_path, capsys):
    root = str(tmp_path)
    assert run(capsys, "--root", root, "init", "os-install/web", "--backend", "ubuntu-autoinstall")[0] == 0
    (tmp_path / KIND / "hub").mkdir(parents=True)
    (tmp_path / KIND / "hub/spec.toml").write_text("x = 1\n")
    (tmp_path / "notes.d/empty").mkdir(parents=True)  # dotted, but holds no item: no warning
    (tmp_path / "docs").mkdir()  # not dotted: never a warning
    code, out, err = run(capsys, "--root", root, "list")
    assert (code, out) == (0, "os-install/web\tubuntu-autoinstall\n")
    assert err == f"warning: {KIND}: no kind of this name is installed; its items are not listed (install the plugin that provides it)\n"


def test_list_refuses_a_symlinked_dotted_directory(tmp_path, capsys):
    target = tmp_path / "elsewhere"
    (target / "hub").mkdir(parents=True)
    (target / "hub/spec.toml").write_text("x = 1\n")
    root = tmp_path / "root"
    root.mkdir()
    (root / KIND).symlink_to(target)
    assert run(capsys, "--root", str(root), "list") == (0, "", f"warning: {KIND}: is a symlink; refused (and no kind of this name is installed)\n")


def test_list_never_follows_a_symlinked_item_in_a_dotted_directory(tmp_path, capsys):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "spec.toml").write_text("x = 1\n")
    root = tmp_path / "root"
    (root / KIND).mkdir(parents=True)
    (root / KIND / "hub").symlink_to(target)  # only a symlinked item: holds no real item
    assert run(capsys, "--root", str(root), "list") == (0, "", "")


def test_list_ignores_a_dotted_plain_file(tmp_path, capsys):
    (tmp_path / KIND).write_text("x\n")
    assert run(capsys, "--root", str(tmp_path), "list") == (0, "", "")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_list_warns_about_an_unreadable_dotted_directory(tmp_path, capsys):
    (tmp_path / KIND).mkdir()
    (tmp_path / KIND).chmod(0)
    try:
        code, out, err = run(capsys, "--root", str(tmp_path), "list")
    finally:
        (tmp_path / KIND).chmod(0o755)
    assert (code, out) == (0, "") and err.startswith(f"warning: {KIND}: no kind of this name is installed")


def test_list_with_the_kind_installed_lists_its_items(tmp_path, capsys, registry_kind):
    (tmp_path / KIND / "hub").mkdir(parents=True)
    (tmp_path / KIND / "hub/spec.toml").write_text(fake_backend().starter("hub"))
    assert run(capsys, "--root", str(tmp_path), "list") == (0, f"{KIND}/hub\t{BACKEND}\n", "")


# --- core stays kind- and backend-neutral ---------------------------------------------------

CORE_MODULES = ["cli", "spec", "registry", "render", "plan", "schema", "validate", "text", "util", "plugin", "kinds/__init__"]
SPECIFIC = ("prov.backends", "prov.kinds.os_install")


def _imports(path: Path, package: str):
    """Absolute names of every module ``path`` imports, ``from x import y`` counted as x and x.y."""
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join(parts + ([node.module] if node.module else []))
            names.append(base)
            names += [f"{base}.{alias.name}" for alias in node.names]
    return names


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_imports_no_specific_kind_or_backend(module):
    path = REPO / "src/prov" / f"{module}.py"
    package = "prov.kinds" if module.startswith("kinds/") else "prov"
    bad = [name for name in _imports(path, package) if any(name == s or name.startswith(s + ".") for s in SPECIFIC)]
    assert bad == [], f"{module} imports {bad}"


def test_import_scan_catches_a_specific_import(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("from .backends import ubuntu_autoinstall\nfrom . import os_install\nimport prov.backends.nixos_disko\n")
    assert "prov.backends.ubuntu_autoinstall" in _imports(probe, "prov")
    assert "prov.kinds.os_install" in _imports(probe, "prov.kinds")
    assert "prov.backends.nixos_disko" in _imports(probe, "prov")
