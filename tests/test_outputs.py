"""Kind outputs: OUTPUTS and outputs(spec) on a third-party dotted kind installed through fake entry points."""

from __future__ import annotations

import json

import pytest
from conftest import fake_entry_point
from test_kind_plugins import BACKEND, KIND, fake_backend, fake_kind

from prov import kinds
from prov.cli import main
from prov.schema import ConfigError

REF = f"{KIND}/hub"
HOSTILE = "ev\x1b[2Jil\nerror: forged‮"

OUTPUTS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["url", "replicas"],
    "properties": {
        "url": {"type": "string", "pattern": "^https://[a-z0-9.-]+$", "description": "Where clients pull from."},
        "replicas": {"type": "integer", "minimum": 1},
        "tls_secret_name": {"type": "string", "description": "Name of the Secret holding the TLS key; never the key."},
    },
}


def registry_outputs(spec):
    return {"url": f"https://{spec['hostname']}", "replicas": spec["replicas"], "tls_secret_name": f"{spec['hostname']}-tls"}


def outputs_kind(**extra):
    return fake_kind(**{"OUTPUTS": OUTPUTS, "outputs": registry_outputs, **extra})


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def clean(text):
    return all(ch.isprintable() or ch in "\n\t" for ch in text)


@pytest.fixture
def install(tmp_path, capsys, extra_kinds, extra_backends):
    """install(kind) registers it with the fake backend and renders <root>/registry.example.org/hub."""

    def go(kind, render=True):
        extra_kinds(fake_entry_point(kind, value="prov_registry.kind"))
        extra_backends(fake_entry_point(fake_backend(), value="prov_registry.text"))
        item = tmp_path / KIND / "hub"
        item.mkdir(parents=True, exist_ok=True)
        (item / "spec.toml").write_text(fake_backend().starter("hub"))
        if render:
            assert run(capsys, "--root", str(tmp_path), "render", REF)[0] == 0
        return str(tmp_path)

    return go


# --- computing outputs ---------------------------------------------------------------------


def test_outputs_in_validate_json(install, capsys):
    root = install(outputs_kind())
    code, out, err = run(capsys, "--root", root, "validate", REF, "--json")
    assert code == 0 and err == ""
    doc = json.loads(out)
    assert doc["outputs"] == {"url": "https://hub.example.org", "replicas": 1, "tls_secret_name": "hub.example.org-tls"}
    assert doc["ok"] is True and doc["findings"] == []
    assert run(capsys, "--root", root, "validate", REF) == (0, f"ok: {REF}\n", "")  # text output unchanged


def test_kind_without_outputs_exports_an_empty_object(install, capsys):
    root = install(fake_kind())
    assert json.loads(run(capsys, "--root", root, "validate", REF, "--json")[1])["outputs"] == {}
    assert kinds.item_outputs(fake_kind(), {"hostname": "x"}) == {}


def test_outputs_are_null_while_the_spec_is_invalid(install, capsys, tmp_path):
    calls = []
    root = install(outputs_kind(outputs=lambda spec: calls.append(spec) or registry_outputs(spec)))
    calls.clear()
    spec = tmp_path / KIND / "hub/spec.toml"
    spec.write_text(spec.read_text().replace('hostname = "hub.example.org"', 'hostname = "hub.example.org"\nreplicas = 2'))
    code, out, err = run(capsys, "--root", root, "validate", REF, "--json")
    doc = json.loads(out)
    assert (code, err) == (1, "") and doc["outputs"] is None and doc["ok"] is False
    assert calls == []  # never called on an invalid spec


def test_outputs_get_the_validated_spec_with_defaults(install, capsys):
    seen = []
    root = install(outputs_kind(outputs=lambda spec: seen.append(spec) or registry_outputs(spec)))
    seen.clear()
    assert run(capsys, "--root", root, "validate", REF, "--json")[0] == 0
    assert seen == [
        {"api_version": "example.org/v1alpha1", "kind": KIND, "backend": BACKEND, "hostname": "hub.example.org", "replicas": 1, BACKEND: {"port": 5001}}
    ]


def test_same_spec_gives_equal_outputs(install, capsys):
    root = install(outputs_kind())
    first = run(capsys, "--root", root, "validate", REF, "--json")
    assert first == run(capsys, "--root", root, "validate", REF, "--json")
    kind, spec = outputs_kind(), {"hostname": "hub.example.org", "replicas": 3}
    assert kinds.item_outputs(kind, spec) == kinds.item_outputs(kind, spec)


def test_item_outputs_copies_in_and_out():
    def mutating(spec):
        spec["hostname"] = "changed"  # a kind cannot change the caller's spec
        return {"url": "https://r.example.org", "replicas": 1}

    spec = {"hostname": "hub.example.org", "replicas": 1}
    assert kinds.item_outputs(outputs_kind(outputs=mutating), spec) == {"url": "https://r.example.org", "replicas": 1}
    assert spec["hostname"] == "hub.example.org"

    kept = {"items": ["a"]}
    schema = {"type": "object", "additionalProperties": False, "properties": {"items": {"type": "array"}}}
    result = kinds.item_outputs(outputs_kind(OUTPUTS=schema, outputs=lambda spec: kept), spec)
    result["items"].append("b")  # the caller's copy is its own
    assert kept == {"items": ["a"]}


# --- misbehaving outputs: exit 2 naming the kind, escaped ------------------------------------


def _cyclic():
    value = []
    value.append(value)
    return {"url": "https://r.example.org", "replicas": 1, "loop": value}


MISBEHAVING = {
    "raises": (lambda spec: (_ for _ in ()).throw(RuntimeError(HOSTILE)), "failed in outputs: RuntimeError: ev\\x1b[2Jil\\x0aerror: forged\\u202e"),
    "config_error": (lambda spec: (_ for _ in ()).throw(ConfigError(HOSTILE)), "failed in outputs: ev\\x1b[2Jil\\x0aerror: forged\\u202e"),
    "exits": (lambda spec: (_ for _ in ()).throw(SystemExit(3)), "failed in outputs: SystemExit: 3"),
    "none": (lambda spec: None, "failed in outputs: returned NoneType, expected a dict"),
    "list": (lambda spec: [1], "failed in outputs: returned list, expected a dict"),
    "dict_subclass": (lambda spec: type("D", (dict,), {})(url="x"), "failed in outputs: returned D, expected a dict"),
    "set_value": (lambda spec: {"url": {"a"}}, "failed in outputs: url: set is not a JSON value"),
    "tuple_value": (lambda spec: {"url": ("a",)}, "failed in outputs: url: tuple is not a JSON value"),
    "nan": (lambda spec: {"replicas": float("nan")}, "failed in outputs: replicas: nan is not a finite number"),
    "inf_nested": (lambda spec: {"x": [{"y": float("inf")}]}, "failed in outputs: x[0].y: inf is not a finite number"),
    "int_key": (lambda spec: {"x": {1: "a"}}, "failed in outputs: x: key 1 is not a string"),
    "cyclic": (lambda spec: _cyclic(), "failed in outputs: loop[0][0]"),
    "hostile_key": (lambda spec: {HOSTILE: {"a"}}, "failed in outputs: ev\\x1b[2Jil\\x0aerror: forged\\u202e: set is not a JSON value"),
    "wrong_type": (lambda spec: {"url": "https://r.example.org", "replicas": "one"}, "failed in outputs: does not match OUTPUTS: replicas: expected integer, got str"),
    "missing": (lambda spec: {"url": "https://r.example.org"}, "does not match OUTPUTS: <root>: missing required key 'replicas'"),
    "unknown_key": (lambda spec: {"url": "https://r.example.org", "replicas": 1, "admin_password": "x"}, "does not match OUTPUTS: <root>: unknown key 'admin_password'"),
    "control_value": (lambda spec: {"url": "https://r.example.org", "replicas": 1, "tls_secret_name": HOSTILE}, "does not match OUTPUTS: tls_secret_name: control characters"),
    "pattern": (lambda spec: {"url": "http://‮evil", "replicas": 1}, "does not match OUTPUTS: url: 'http://\\u202eevil' does not match"),
}


@pytest.mark.parametrize("case", sorted(MISBEHAVING))
@pytest.mark.parametrize("json_flag", [False, True])
def test_misbehaving_outputs_exit_2_escaped(install, capsys, case, json_flag):
    fn, needle = MISBEHAVING[case]
    root = install(outputs_kind(outputs=fn))
    argv = ["--root", root, "validate", REF] + (["--json"] if json_flag else [])
    code, out, err = run(capsys, *argv)
    assert (code, out) == (2, ""), (case, out, err)
    assert err.startswith(f"prov: error: kind '{KIND}' ") and needle in err, err
    assert err.count("\n") == 1 and clean(err), err


def test_deeply_nested_outputs_are_refused_not_recursed():
    deep = "x"
    for _ in range(kinds.OUTPUTS_MAX_DEPTH + 5):
        deep = [deep]
    kind = outputs_kind(OUTPUTS={"type": "object", "additionalProperties": False, "properties": {"d": {"type": "array"}}}, outputs=lambda spec: {"d": deep})
    with pytest.raises(ConfigError, match=f"nested deeper than {kinds.OUTPUTS_MAX_DEPTH} levels"):
        kinds.item_outputs(kind, {})


# --- hostile values round-trip through --json ----------------------------------------------


def test_hostile_output_values_round_trip_as_ascii_json(install, capsys):
    """Values without control characters but with bidi marks, zero-width and non-ASCII text are raw in the document."""
    value = "Op ‮erator​ ü 日本 \U0001f600"
    schema = {"type": "object", "additionalProperties": False, "properties": {"note": {"type": "string"}, "list": {"type": "array"}}}
    root = install(outputs_kind(OUTPUTS=schema, outputs=lambda spec: {"note": value, "list": [value, 1.5, None, True, {"k": value}]}))
    code, out, err = run(capsys, "--root", root, "validate", REF, "--json")
    assert (code, err) == (0, "")
    assert all(" " <= ch <= "~" or ch == "\n" for ch in out)  # printable ASCII only
    assert json.loads(out)["outputs"] == {"note": value, "list": [value, 1.5, None, True, {"k": value}]}


# --- the contract: check_kind --------------------------------------------------------------


def _schema(props, **extra):
    return {"type": "object", "additionalProperties": False, "properties": props, **extra}


@pytest.mark.parametrize(
    "schema, name",
    [
        (_schema({"admin_password": {"type": "string"}}), "admin_password"),
        (_schema({"root_passwd": {"type": "string"}}), "root_passwd"),
        (_schema({"join_token": {"type": "string"}}), "join_token"),
        (_schema({"ClientSecret": {"type": "string"}}), "ClientSecret"),
        (_schema({"ssh_private_key": {"type": "string"}}), "ssh_private_key"),
        (_schema({"cloud_api_key": {"type": "string"}}), "cloud_api_key"),
        (_schema({"db_credentials": {"type": "object"}}), "db_credentials"),
        (_schema({"token_path_old": {"type": "string"}}), "token_path_old"),
        (_schema({"admin": {"type": "object", "properties": {"password": {"type": "string"}}}}), "password"),
        (_schema({"nodes": {"type": "array", "items": {"type": "object", "properties": {"bootstrap_token": {"type": "string"}}}}}), "bootstrap_token"),
        (_schema({}, allOf=[{"properties": {"secret": {"type": "string"}}}]), "secret"),
        (_schema({"ops": {"$ref": "#/$defs/user"}}), "password_hash"),  # through the shared definitions
    ],
)
def test_secret_looking_output_names_are_rejected(schema, name):
    with pytest.raises(ConfigError, match=f"OUTPUTS property '{name}' looks like a secret value; outputs never carry secrets"):
        kinds.check_kind(outputs_kind(OUTPUTS=schema))


@pytest.mark.parametrize(
    "name",
    ["admin_password_path", "join_token_ref", "tls_secret_name", "db_credential_ref", "API_KEY_PATH", "Private_Key_Name", "hostname", "address"],
)
def test_reference_suffixes_and_plain_names_are_allowed(name):
    kinds.check_kind(outputs_kind(OUTPUTS=_schema({name: {"type": "string"}})))


@pytest.mark.parametrize(
    "extra, needle",
    [
        ({"outputs": None, "OUTPUTS": OUTPUTS}, "outputs is not callable"),
        ({"OUTPUTS": []}, "OUTPUTS must be an object schema"),
        ({"OUTPUTS": {"type": "object", "properties": {}}}, 'OUTPUTS must be an object schema with properties and "additionalProperties": false'),
        ({"OUTPUTS": {"type": "array", "additionalProperties": False, "properties": {}}}, "OUTPUTS must be an object schema"),
        ({"OUTPUTS": {"type": "object", "additionalProperties": False}}, "OUTPUTS must be an object schema"),
        ({"OUTPUTS": _schema({"a": {"type": "string", "format": "uri"}})}, "invalid OUTPUTS: <outputs registry.example.org>.properties.a: unsupported keyword(s) ['format']"),
        ({"OUTPUTS": _schema({"a": {"type": "str"}})}, "invalid OUTPUTS: <outputs registry.example.org>.properties.a.type: must be one of"),
        ({"OUTPUTS": _schema({}, **{"$defs": {"x": {}}})}, "fragments may not define $defs"),
        ({"OUTPUTS": _schema({"a": {"$ref": "#/$defs/nope"}})}, "'#/$defs/nope' does not resolve"),
    ],
)
def test_invalid_outputs_contract(extra, needle):
    with pytest.raises(ConfigError) as exc:
        kinds.check_kind(outputs_kind(**extra))
    assert needle in str(exc.value)


@pytest.mark.parametrize("member", ["OUTPUTS", "outputs"])
def test_outputs_and_schema_come_together(member):
    kind = fake_kind(**({"OUTPUTS": OUTPUTS} if member == "OUTPUTS" else {"outputs": registry_outputs}))
    with pytest.raises(ConfigError, match=f"OUTPUTS and outputs come together; it defines {member} only"):
        kinds.check_kind(kind)


@pytest.mark.parametrize("argv", [["kinds"], ["kinds", "--json"], ["schema", "os-install"]])
def test_bad_outputs_contract_is_a_one_line_plugin_error(extra_kinds, capsys, argv):
    extra_kinds(fake_entry_point(outputs_kind(OUTPUTS=_schema({"admin_password": {"type": "string"}})), value="prov_registry.kind"))
    code, out, err = run(capsys, *argv)
    assert (code, out) == (2, "") and f"kind plugin {KIND} = prov_registry.kind: OUTPUTS property 'admin_password'" in err
    assert err.count("\n") == 1 and clean(err)


# --- prov kinds --json ---------------------------------------------------------------------


def test_kinds_json_shows_the_outputs_schema(extra_kinds, capsys):
    extra_kinds(fake_entry_point(outputs_kind(), value="prov_registry.kind"))
    code, out, err = run(capsys, "kinds", "--json")
    assert (code, err) == (0, "")
    by_name = {k["name"]: k for k in json.loads(out)["kinds"]}
    assert by_name[KIND]["outputs"] == OUTPUTS
    assert by_name["os-install"]["outputs"] is None  # os-install declares no outputs
    assert not hasattr(kinds.get("os-install"), "OUTPUTS") and not hasattr(kinds.get("os-install"), "outputs")
    code, text, _ = run(capsys, "kinds")
    assert code == 0 and "outputs" not in text.lower()  # text output unchanged
