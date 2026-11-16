"""Backend registry: contract checks, discovery rules, call_plugin, Context."""

from __future__ import annotations

import dataclasses
import re
import types

import pytest
from conftest import fake_entry_point

from prov.registry import Context, builtin, call_plugin, check_contract, discover, for_kind, lookup
from prov.schema import ConfigError


def fake_backend(name="talos", kind="os-install", schema=None, **extra):
    """A complete backend module; override or add members with keyword arguments."""
    mod = types.ModuleType(f"fake_{name}")
    mod.NAME = name
    mod.KIND = kind
    mod.SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}} if schema is None else schema
    mod.render = lambda spec, ctx: {}
    mod.validate = lambda d: []
    mod.plan = lambda spec, ctx: []
    for key, value in extra.items():
        setattr(mod, key, value)
    return mod


def test_complete_fake_backend_passes():
    check_contract(fake_backend(), "talos = fake")


def test_contract_check_rejects_incomplete_plugin():
    fake = types.SimpleNamespace(NAME="talos", KIND="os-install", SCHEMA={}, render=lambda s, c: {})
    with pytest.raises(ConfigError, match="validate, plan"):
        check_contract(fake, "talos = fake")


def test_contract_requires_kind():
    fake = types.SimpleNamespace(NAME="talos", SCHEMA={}, render=lambda s, c: {}, validate=lambda d: [], plan=lambda s, c: [])
    with pytest.raises(ConfigError, match="lacks required member.*KIND"):
        check_contract(fake, "talos = fake")


def test_unknown_kind_is_rejected():
    with pytest.raises(ConfigError, match="unknown KIND 'gizmo' \\(known kinds: os-install\\)"):
        check_contract(fake_backend(kind="gizmo"), "talos = fake")


@pytest.mark.parametrize("name", ["hostname", "storage", "network", "users", "kind", "backend"])
def test_name_colliding_with_a_core_property_is_rejected(name):
    with pytest.raises(ConfigError, match=f"NAME '{name}' collides with a core property"):
        check_contract(fake_backend(name=name), f"{name} = fake")


@pytest.mark.parametrize("name", ["Talos", "talos_x", "1talos", "", "talos\n"])
def test_name_must_be_an_id(name):
    with pytest.raises(ConfigError, match="must match"):
        check_contract(fake_backend(name=name), "x = fake")


def test_duplicate_name_is_rejected(extra_backends):
    a, b = fake_backend("talos"), fake_backend("talos")
    extra_backends(fake_entry_point(a, value="pkg_a"), fake_entry_point(b, value="pkg_b"))
    with pytest.raises(ConfigError, match="backend 'talos' is registered twice"):
        discover()


def test_duplicate_of_a_builtin_name_is_rejected(extra_backends):
    extra_backends(fake_entry_point(fake_backend("ubuntu-autoinstall")))
    with pytest.raises(ConfigError, match="registered twice"):
        discover()


def test_entry_point_name_must_equal_name(extra_backends):
    extra_backends(fake_entry_point(fake_backend("talos"), name="talos-linux"))
    with pytest.raises(ConfigError, match="entry-point name differs from NAME 'talos'"):
        discover()


def test_build_without_build_tools_is_rejected():
    with pytest.raises(ConfigError, match="defines build but no BUILD_TOOLS"):
        check_contract(fake_backend(build=lambda spec, ctx: None), "talos = fake")
    with pytest.raises(ConfigError, match="BUILD_TOOLS"):
        check_contract(fake_backend(build=lambda spec, ctx: None, BUILD_TOOLS="xorriso"), "talos = fake")
    check_contract(fake_backend(build=lambda spec, ctx: None, BUILD_TOOLS=("xorriso",)), "talos = fake")


@pytest.mark.parametrize("member", ["check", "starter", "artifacts", "build"])
def test_optional_members_must_be_callable(member):
    with pytest.raises(ConfigError, match=f"{member} is not callable"):
        check_contract(fake_backend(**{member: "nope", "BUILD_TOOLS": ()}), "talos = fake")


def test_refs_must_be_a_dict():
    with pytest.raises(ConfigError, match="REFS must be a dict"):
        check_contract(fake_backend(REFS=["image"]), "talos = fake")


def test_discover_orders_by_kind_then_name(extra_backends):
    extra_backends(fake_entry_point(fake_backend("aaa")), fake_entry_point(fake_backend("zzz")))
    assert list(discover()) == ["aaa", "ubuntu-autoinstall", "zzz"]


def test_lookup_and_for_kind():
    backends = discover()
    assert list(for_kind(backends, "os-install")) == ["ubuntu-autoinstall"]
    assert for_kind(backends, "image") == {}
    assert lookup(backends, "os-install", "ubuntu-autoinstall") is builtin()["ubuntu-autoinstall"]
    with pytest.raises(ConfigError, match="unknown backend 'plan9' for kind 'os-install' \\(registered: ubuntu-autoinstall\\)"):
        lookup(backends, "os-install", "plan9")
    with pytest.raises(ConfigError, match="unknown backend"):
        lookup(backends, "os-install", ["not", "a", "name"])


def test_builtin_backends_are_all_registered():
    backends = discover()
    for name, mod in builtin().items():
        assert backends[name] is mod


def test_context_is_frozen_with_empty_refs():
    ctx = Context("demo")
    assert ctx.name == "demo" and dict(ctx.refs) == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.name = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        ctx.refs["x"] = 1  # type: ignore[index]
    assert Context("a").refs is not None and Context("a") == Context("a")


@pytest.mark.parametrize(
    "method,value,needle",
    [
        ("render", ["x"], "failed in render: returned list, expected a dict of str paths"),
        ("validate", None, "failed in validate: returned NoneType"),
        ("check", ("a",), "failed in check: returned tuple"),
        ("plan", "echo hi", "failed in plan: returned str, expected a list of str"),
        ("starter", b"x", "failed in starter: returned bytes, expected a str"),
    ],
)
def test_call_plugin_checks_return_types(method, value, needle):
    plugin = fake_backend(**{method: lambda *a: value})
    with pytest.raises(ConfigError, match=re.escape(f"backend 'talos' {needle}")):
        call_plugin(plugin, method, None, None) if method not in ("validate", "starter") else call_plugin(plugin, method, None)


def test_call_plugin_wraps_exceptions_but_passes_config_errors():
    def boom(*a):
        raise KeyError("oops")

    def config(*a):
        raise ConfigError("plugin says no")

    with pytest.raises(ConfigError, match="backend 'talos' failed in render: KeyError"):
        call_plugin(fake_backend(render=boom), "render", {}, Context("x"))
    with pytest.raises(ConfigError, match="^plugin says no$"):
        call_plugin(fake_backend(render=config), "render", {}, Context("x"))


@pytest.mark.parametrize(
    "schema,needle",
    [
        ({"type": "object", "properties": {"x": {"type": "string", "pattern": "^[a-"}}}, "invalid regular expression"),
        ({"type": "object", "format": "uri"}, "unsupported keyword"),
        ({"type": "object", "properties": {"x": {"$ref": "https://example.com/s.json"}}}, "$ref"),
        ({"type": "object", "properties": {"x": "string"}}, "must be an object"),
    ],
)
def test_contract_check_rejects_bad_schema(schema, needle):
    with pytest.raises(ConfigError, match=re.escape(needle)):
        check_contract(fake_backend(schema=schema), "talos = fake")


BAD_SCHEMAS = {
    "type_typo": ({"type": "strin"}, "type: must be one of"),
    "type_empty_list": ({"type": []}, "type: must be one of"),
    "minLength_string": ({"type": "string", "minLength": "3"}, "minLength: must be a non-negative integer"),
    "minItems_bool": ({"type": "array", "minItems": True}, "minItems: must be a non-negative integer"),
    "maxLength_negative": ({"type": "string", "maxLength": -1}, "maxLength: must be a non-negative integer"),
    "minimum_string": ({"type": "integer", "minimum": "1"}, "minimum: must be a number"),
    "properties_list": ({"type": "object", "properties": []}, "properties: must be an object"),
    "required_string": ({"type": "object", "required": "x"}, "required: must be a list of strings"),
    "enum_empty": ({"enum": []}, "enum: must be a non-empty list"),
    "allOf_dict": ({"allOf": {}}, "allOf: must be a list"),
    "uniqueItems_string": ({"type": "array", "uniqueItems": "yes"}, "uniqueItems: must be a boolean"),
    "additionalProperties_string": ({"type": "object", "additionalProperties": "no"}, "additionalProperties: must be a boolean"),
    "own_defs": ({"type": "object", "$defs": {"x": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/x"}}}, "may not define $defs"),
    "unresolved_ref": ({"type": "object", "properties": {"a": {"$ref": "#/$defs/disk"}}}, "does not resolve"),
    "items_not_schema": ({"type": "array", "items": "string"}, "must be an object"),
}


@pytest.mark.parametrize("name", sorted(BAD_SCHEMAS))
def test_schema_values_are_validated_at_load(name):
    schema, needle = BAD_SCHEMAS[name]
    with pytest.raises(ConfigError, match=re.escape(needle)):
        check_contract(fake_backend(schema=schema), "talos = fake")


def test_shared_defs_are_referenceable():
    schema = {"type": "object", "properties": {"admins": {"type": "array", "items": {"$ref": "#/$defs/user"}}}}
    check_contract(fake_backend(schema=schema), "talos = fake")


def test_composed_schema_passes_its_own_check():
    from prov import kinds
    from prov.kinds.common import DEFS
    from prov.schema import spec_schema
    from prov.validate import check_schema_fragment

    for name, kind in kinds.KINDS.items():
        composed = spec_schema(kind, for_kind(builtin(), name), DEFS)
        check_schema_fragment(composed, f"<{name}>", defs=composed["$defs"])


def test_broken_third_party_plugin_breaks_no_command_with_a_traceback(monkeypatch, capsys):
    import prov.registry as registry
    from prov.cli import main

    bad = fake_backend(schema={"type": "object", "properties": []})
    monkeypatch.setattr(registry, "entry_points", lambda group: [fake_entry_point(bad, value="prov_talos")])
    assert main(["backends"]) == 2
    assert "backend plugin talos = prov_talos: invalid SCHEMA" in capsys.readouterr().err


def test_unloadable_plugin_exits_2(monkeypatch, capsys):
    import prov.registry as registry
    from prov.cli import main

    def load():
        raise ImportError("no module named prov_talos")

    ep = types.SimpleNamespace(name="talos", value="prov_talos", load=load)
    monkeypatch.setattr(registry, "entry_points", lambda group: [ep])
    assert main(["backends"]) == 2
    assert "cannot load backend plugin talos = prov_talos" in capsys.readouterr().err


@pytest.mark.parametrize(
    "pattern,text,ok",
    [
        ("^[0-9]+$", "٣", False),
        ("^\\d+$", "٣", False),  # re.ASCII: \d is ASCII-only, as in ECMA-262
        ("^\\d+$", "3", True),
    ],
)
def test_patterns_are_ascii(pattern, text, ok):
    from prov.validate import ecma_search

    assert ecma_search(pattern, text) is ok
