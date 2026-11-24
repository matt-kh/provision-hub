"""Kinds and the spec envelope; a fake kind + backend proving core holds nothing os-install specific."""

from __future__ import annotations

import copy
import json
import types

import jsonschema
import pytest
from conftest import PROVISION_HUB, fake_entry_point

from prov import kinds
from prov.backends import ubuntu_autoinstall as ubuntu
from prov.cli import main
from prov.kinds.common import DEFS
from prov.registry import check_contract, discover
from prov.schema import ConfigError, spec_schema, tomllib
from prov.spec import check_spec, parse_ref

STARTER = tomllib.loads(ubuntu.starter("demo"))


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# --- built-in kinds ---------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(kinds.builtin()))
def test_builtin_kinds_satisfy_the_contract(name):
    kinds.check_kind(kinds.get(name))
    assert kinds.get(name).NAME == name


def test_os_install_core_has_no_backend_keys():
    props = kinds.get("os-install").PROPERTIES
    assert "distro" not in props and "schema_version" not in props and "backend" not in props
    assert kinds.get("os-install").REQUIRED == ["hostname", "storage"]
    assert kinds.get("os-install").REFS == {}


@pytest.mark.parametrize(
    "member,value,needle",
    [
        ("NAME", "Os_Install", "NAME must match"),
        ("VERSIONS", ["v1alpha1"], "VERSIONS must be"),
        ("VERSIONS", ("1",), "VERSIONS must be"),
        ("REQUIRED", ["nope"], "REQUIRED must be"),
        ("PROPERTIES", {"kind": {"type": "string"}}, "may not redefine envelope keys"),
        ("PROPERTIES", {"x": {"type": "strin"}}, "invalid PROPERTIES"),
        ("NAME_KEY", "nope", "NAME_KEY 'nope'"),
        ("semantic", None, "semantic is not callable"),
    ],
)
def test_kind_contract_rejects(member, value, needle):
    kind = types.ModuleType("broken")
    for key in kinds.REQUIRED_MEMBERS:
        setattr(kind, key, getattr(kinds.get("os-install"), key))
    if member == "PROPERTIES" or member == "REQUIRED":
        kind.REQUIRED = [] if member == "PROPERTIES" else value
    setattr(kind, member, value)
    with pytest.raises(ConfigError, match=needle):
        kinds.check_kind(kind)


# --- envelope ---------------------------------------------------------------------------


def check(raw, kind="os-install"):
    return check_spec(raw, kind, "demo", discover())


def mutate(fn):
    raw = copy.deepcopy(STARTER)
    fn(raw)
    return raw


ENVELOPE_ERRORS = {
    "missing_api_version": (lambda r: r.pop("api_version"), "api_version is required (this prov supports: prov/v1alpha1)"),
    "unknown_api_version": (lambda r: r.update(api_version="prov/v9"), "unsupported api_version 'prov/v9' for kind 'os-install'"),
    "api_version_not_a_string": (lambda r: r.update(api_version=1), "unsupported api_version 1"),
    "legacy_schema_version": (lambda r: r.update(schema_version=1), "`schema_version` is a pre-v1alpha1 key; remove it (see CHANGELOG migration)"),
    "legacy_schema_version_without_api_version": (
        lambda r: (r.pop("api_version"), r.update(schema_version=1)),
        "schema_version: this file predates prov/v1alpha1; see CHANGELOG migration",
    ),
    "legacy_wins_over_missing_api_version": (lambda r: (r.pop("api_version"), r.update(schema_version=1)), "predates prov/v1alpha1"),
    "missing_kind": (lambda r: r.pop("kind"), "kind is required (the directory says 'os-install')"),
    "kind_differs_from_directory": (lambda r: r.update(kind="image"), "kind 'image' does not match the directory kind 'os-install'"),
    "missing_backend": (lambda r: r.pop("backend"), "backend is required (registered for os-install: nixos-disko, ubuntu-autoinstall)"),
    "unknown_backend": (lambda r: r.update(backend="plan9"), "unknown backend 'plan9' for kind 'os-install' (registered: nixos-disko, ubuntu-autoinstall)"),
    "backend_not_a_string": (lambda r: r.update(backend=["ubuntu-autoinstall"]), "unknown backend"),
    "old_distro_name": (lambda r: r.update(backend="ubuntu"), "unknown backend 'ubuntu'"),
    "legacy_distro_key": (lambda r: r.update(distro="ubuntu"), "`distro` is a pre-v1alpha1 key; remove it (see CHANGELOG migration)"),
    "legacy_distro_key_without_api_version": (
        lambda r: (r.pop("api_version"), r.update(distro="ubuntu")),
        "distro: this file predates prov/v1alpha1; see CHANGELOG migration",
    ),
    "legacy_ubuntu_table_without_api_version": (
        lambda r: (r.pop("api_version"), r.update(ubuntu=r.pop("ubuntu-autoinstall"))),
        "ubuntu: this file predates prov/v1alpha1; see CHANGELOG migration",
    ),
    "long_api_version_is_capped": (lambda r: r.update(api_version="v" * 500), "unsupported api_version '" + "v" * 80 + "'…"),
    "long_kind_is_capped": (lambda r: r.update(kind="k" * 500), "kind '" + "k" * 80 + "'… does not match"),
    "long_backend_is_capped": (lambda r: r.update(backend="b" * 500), "unknown backend '" + "b" * 80 + "'… for kind"),
}


@pytest.mark.parametrize("name", sorted(ENVELOPE_ERRORS))
def test_envelope_errors_are_config_errors(name):
    fn, needle = ENVELOPE_ERRORS[name]
    with pytest.raises(ConfigError) as exc:
        check(mutate(fn))
    assert needle in str(exc.value)


@pytest.mark.parametrize("name", sorted(ENVELOPE_ERRORS))
@pytest.mark.parametrize("verb", ["render", "validate", "diff", "plan"])
def test_envelope_errors_exit_2_on_every_verb(tmp_path, capsys, name, verb):
    fn, needle = ENVELOPE_ERRORS[name]
    assert run(capsys, "--root", str(tmp_path), "init", "os-install/demo", "--backend", "ubuntu-autoinstall")[0] == 0
    path = tmp_path / "os-install/demo/spec.toml"
    raw = tomllib.loads(path.read_text())
    fn(raw)
    path.write_text(_toml(raw))
    code, _, err = run(capsys, "--root", str(tmp_path), verb, "os-install/demo")
    assert code == 2 and needle in err and str(path) in err, err


def _toml(raw):
    """Minimal TOML writer for the envelope tests (top-level scalars + nested tables via JSON-compatible inline tables)."""
    lines = []
    for key, value in raw.items():
        lines.append(f'"{key}" = {_value(value)}')
    return "\n".join(lines) + "\n"


def _value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    return "{" + ", ".join(f'"{k}" = {_value(v)}' for k, v in value.items()) + "}"


def test_toml_helper_round_trips():
    assert tomllib.loads(_toml(STARTER)) == STARTER


def test_backend_of_another_kind_is_refused(extra_kinds, extra_backends):
    extra_kinds(fake_entry_point(_widget_kind("image"), dist=PROVISION_HUB))
    other = _widget_backend("image-builder", "image")
    extra_backends(fake_entry_point(other))
    with pytest.raises(ConfigError, match="backend 'image-builder' renders kind 'image', not 'os-install'"):
        check(mutate(lambda r: r.update(backend="image-builder")))


def test_init_refuses_a_backend_of_another_kind(tmp_path, capsys, widget):
    code, _, err = run(capsys, "--root", str(tmp_path), "init", "os-install/demo", "--backend", "widget-text")
    assert code == 2 and "backend 'widget-text' renders kind 'widget', not 'os-install'" in err
    code, _, err = run(capsys, "--root", str(tmp_path), "init", "widget/demo", "--backend", "ubuntu-autoinstall")
    assert code == 2 and "backend 'ubuntu-autoinstall' renders kind 'os-install', not 'widget'" in err
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("ref", ["os-install", "/demo", "os-install/", "os_install/demo", "os-install/a/b", "os-install/..", "../demo", "os-install/demo\n"])
def test_parse_ref_rejects(ref):
    with pytest.raises(ConfigError):
        parse_ref(ref)


def test_parse_ref():
    assert parse_ref("os-install/demo-1") == ("os-install", "demo-1")
    with pytest.raises(ConfigError, match="unknown kind 'image'"):
        parse_ref("image/demo")


# --- a fake kind and backend: the core is generic ----------------------------------------


def _widget_kind(name="widget"):
    kind = types.ModuleType(name)
    kind.__doc__ = "A test-only kind: a widget of some size."
    kind.NAME = name
    kind.PLUGIN_API = 1
    kind.VERSIONS = ("v1alpha1",)
    kind.PROPERTIES = {
        "size": {"type": "integer", "minimum": 1, "description": "Widget size."},
        "owners": {"type": "array", "items": {"$ref": "#/$defs/user"}, "default": []},
    }
    kind.REQUIRED = ["size"]
    kind.REFS = {}

    def semantic(spec):
        from prov.validate import ERROR, Finding

        return [Finding(ERROR, "size 13 is unlucky", "size")] if spec["size"] == 13 else []

    kind.semantic = semantic
    return kind


def _widget_backend(name="widget-text", kind="widget"):
    mod = types.ModuleType(name)
    mod.__doc__ = "Renders a widget as a text file."
    mod.NAME = name
    mod.KIND = kind
    mod.SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"greeting": {"type": "string", "pattern": "^[a-z ]+$", "default": "hello"}},
    }
    mod.render = lambda spec, ctx: {"widget.txt": f"{spec[name]['greeting']} {ctx.name} size {spec['size']}\n"}

    def validate(rendered_dir):
        from prov.util import read_regular_text
        from prov.validate import ERROR, Finding

        text = read_regular_text(rendered_dir / "widget.txt")
        return [] if text.endswith("\n") else [Finding(ERROR, "no trailing newline", "widget.txt")]

    mod.validate = validate
    mod.plan = lambda spec, ctx: ["# hand the widget over", f"cat widget.txt  # {ctx.name}"]
    mod.starter = lambda item: f'api_version = "prov/v1alpha1"\nkind = "{kind}"\nbackend = "{name}"\nsize = 3\n\n[{name}]\ngreeting = "hi there"\n'
    return mod


@pytest.fixture
def widget(extra_kinds, extra_backends):
    """Register a test-only kind (bare, so registered as if by provision-hub) and one backend for it."""
    kind = _widget_kind()
    kinds.check_kind(kind)
    extra_kinds(fake_entry_point(kind, dist=PROVISION_HUB))
    backend = _widget_backend()
    check_contract(backend, "widget-text = tests")
    extra_backends(fake_entry_point(backend))
    return kind, backend


def test_fake_kind_full_flow(tmp_path, capsys, widget):
    root = str(tmp_path)
    code, out, _ = run(capsys, "kinds")
    assert code == 0 and "widget\tprov/v1alpha1\tA test-only kind: a widget of some size.\n" in out
    code, out, _ = run(capsys, "backends", "--kind", "widget")
    assert (code, out) == (0, "widget-text\twidget\tRenders a widget as a text file.\n")

    assert run(capsys, "--root", root, "init", "widget/w1", "--backend", "widget-text")[0] == 0
    assert run(capsys, "--root", root, "init", "os-install/h1", "--backend", "ubuntu-autoinstall")[0] == 0
    code, out, _ = run(capsys, "--root", root, "render", "widget/w1")
    assert code == 0 and "wrote widget.txt" in out
    assert (tmp_path / "widget/w1/rendered/widget.txt").read_text() == "hi there w1 size 3\n"
    code, out, _ = run(capsys, "--root", root, "validate", "widget/w1")
    assert (code, out) == (0, "ok: widget/w1\n")
    assert run(capsys, "--root", root, "diff", "widget/w1")[0] == 0
    code, out, _ = run(capsys, "--root", root, "plan", "widget/w1")
    assert code == 0 and out.splitlines()[0].startswith("# prov plan for widget/w1 (widget-text)")
    assert out.splitlines()[-1] == "cat widget.txt  # w1"
    code, out, _ = run(capsys, "--root", root, "list")
    assert (code, out) == (0, "os-install/h1\tubuntu-autoinstall\nwidget/w1\twidget-text\n")

    spec = tmp_path / "widget/w1/spec.toml"
    spec.write_text(spec.read_text().replace("size = 3", "size = 13"))
    code, _, err = run(capsys, "--root", root, "validate", "widget/w1")
    assert code == 1 and "error: size: size 13 is unlucky" in err
    spec.write_text(spec.read_text().replace("size = 13", "size = 0"))
    code, _, err = run(capsys, "--root", root, "render", "widget/w1")
    assert code == 2 and "0 is less than 1" in err
    spec.write_text(spec.read_text().replace('kind = "widget"', 'kind = "os-install"'))
    code, _, err = run(capsys, "--root", root, "validate", "widget/w1")
    assert code == 2 and "kind 'os-install' does not match the directory kind 'widget'" in err


def test_fake_kind_schema(capsys, widget):
    code, out, _ = run(capsys, "schema", "widget")
    assert code == 0
    schema = json.loads(out)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$id"].endswith("/widget.v1alpha1.schema.json")
    assert schema["required"] == ["api_version", "kind", "backend", "size"]
    assert set(schema["properties"]) == {"api_version", "kind", "backend", "size", "owners", "widget-text"}
    good = {"api_version": "prov/v1alpha1", "kind": "widget", "backend": "widget-text", "size": 2, "widget-text": {}}
    jsonschema.Draft202012Validator(schema).validate(good)
    assert not jsonschema.Draft202012Validator(schema).is_valid(dict(good, kind="os-install"))
    # the os-install schema does not know the widget backend
    os_schema = spec_schema(kinds.get("os-install"), {"ubuntu-autoinstall": ubuntu}, DEFS)
    assert "widget-text" not in os_schema["properties"]


def test_backend_name_colliding_with_a_fake_kind_property(widget):
    with pytest.raises(ConfigError, match="NAME 'size' collides with a core property of kind 'widget'"):
        check_contract(_widget_backend("size"), "size = tests")


def test_kind_without_backends(capsys, extra_kinds):
    extra_kinds(fake_entry_point(_widget_kind(), dist=PROVISION_HUB))
    code, out, _ = run(capsys, "schema", "widget")
    assert code == 0 and "enum" not in json.loads(out)["properties"]["backend"]
    assert run(capsys, "backends", "--kind", "widget") == (0, "", "")
    with pytest.raises(ConfigError, match="backend is required \\(registered for widget: none\\)"):
        check_spec({"api_version": "prov/v1alpha1", "kind": "widget", "size": 1}, "widget", "w", discover())


def test_old_host_toml_without_schema_version_gets_the_migration_hint(tmp_path, capsys):
    path = tmp_path / "os-install/demo/spec.toml"
    path.parent.mkdir(parents=True)
    path.write_text('hostname = "demo"\n\n[ubuntu]\nrelease = "24.04"\n')
    code, _, err = run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert code == 2 and "ubuntu: this file predates prov/v1alpha1; see CHANGELOG migration" in err


def test_ubuntu_table_with_api_version_is_a_schema_error_not_a_hint():
    host, findings = check(mutate(lambda r: r.update(ubuntu={})))
    assert host is None and any("unknown key 'ubuntu'" in f.message for f in findings)


@pytest.mark.parametrize("value", ["x" * 500, ["y"] * 200])
def test_long_values_are_capped_in_messages(value):
    for key in ("api_version", "kind", "backend"):
        with pytest.raises(ConfigError) as exc:
            check(mutate(lambda r: r.update({key: value})))
        assert len(str(exc.value)) < 300, (key, str(exc.value))


def test_registry_reserves_legacy_keys_as_backend_names():
    with pytest.raises(ConfigError, match="NAME 'distro' collides with a core property of the spec envelope"):
        check_contract(_widget_backend("distro", "os-install"), "distro = tests")
