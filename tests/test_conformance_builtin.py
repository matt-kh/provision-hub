"""The built-in kinds' conformance fixtures: every built-in backend passes, and the exported files stay in sync.

Static and rendered only, like every ``prov conformance`` run: nothing here installs,
builds or boots anything.
"""

from __future__ import annotations

import json

import jsonschema
import pytest
from conftest import CONFORMANCE_DIR, SCHEMA_DIR, UPDATE_GOLDENS

from prov import kinds, registry
from prov.cli import main
from prov.conformance import EXPORT_FORMAT, export_fixtures, fixtures_file_name
from prov.schema import api_versions, schema_file_name
from prov.text import json_text

BUILTIN_BACKENDS = registry.builtin()
KINDS_WITH_FIXTURES = {name: kind for name, kind in kinds.builtin().items() if getattr(kind, "FIXTURES", None)}


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def exported(kind):
    return json.loads((CONFORMANCE_DIR / fixtures_file_name(kind)).read_text(encoding="utf-8"))


def test_os_install_ships_fixtures():
    assert "os-install" in KINDS_WITH_FIXTURES
    fixtures = kinds.fixtures(KINDS_WITH_FIXTURES["os-install"])
    assert any(f["expect"]["valid"] for f in fixtures.values())
    assert any(not f["expect"]["valid"] for f in fixtures.values())


@pytest.mark.parametrize("name", sorted(BUILTIN_BACKENDS))
def test_builtin_backend_passes_conformance(capsys, name):
    code, out, err = run(capsys, "conformance", name)
    assert (code, err) == (0, ""), out
    assert "\tFAIL\t" not in out and not any(line.startswith("FAIL") for line in out.splitlines())
    code, out, _ = run(capsys, "conformance", name, "--json")
    doc = json.loads(out)
    assert code == 0 and doc["ok"] is True
    reasons = getattr(BUILTIN_BACKENDS[name], "UNSUPPORTED_FIXTURES", {})
    for fixture in doc["fixtures"]:
        if fixture["status"] == "skipped":
            (check,) = fixture["checks"]
            assert check["check"] == "supported" and reasons[fixture["name"]] in check["detail"]


@pytest.mark.parametrize("name", sorted(BUILTIN_BACKENDS))
def test_builtin_backend_covers_every_fixture(name):
    """Each fixture has a table or a reason when the SCHEMA requires keys; no stale names."""
    plugin = BUILTIN_BACKENDS[name]
    names = set(kinds.fixtures(kinds.builtin()[plugin.KIND]))
    covered = set(getattr(plugin, "FIXTURES", {})) | set(getattr(plugin, "UNSUPPORTED_FIXTURES", {}))
    assert covered <= names
    if plugin.SCHEMA.get("required"):
        assert covered == names


@pytest.mark.parametrize("name", sorted(KINDS_WITH_FIXTURES))
def test_exported_fixtures_match_code(name):
    kind = KINDS_WITH_FIXTURES[name]
    path = CONFORMANCE_DIR / fixtures_file_name(kind)
    text = json_text(export_fixtures(kind, BUILTIN_BACKENDS))
    if UPDATE_GOLDENS:
        CONFORMANCE_DIR.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.is_file(), "regenerate: PROV_UPDATE_GOLDENS=1 uv run pytest"
    assert path.read_text(encoding="utf-8") == text, "regenerate: PROV_UPDATE_GOLDENS=1 uv run pytest"


def test_no_stale_exported_fixtures():
    expected = {fixtures_file_name(k) for k in KINDS_WITH_FIXTURES.values()}
    assert {p.name for p in CONFORMANCE_DIR.glob("*")} == expected


@pytest.mark.parametrize("name", sorted(KINDS_WITH_FIXTURES))
def test_exported_header_and_backends(name):
    kind = KINDS_WITH_FIXTURES[name]
    doc = exported(kind)
    assert sorted(doc) == ["api_version", "backends", "fixtures", "format", "kind"]
    assert (doc["format"], doc["kind"], doc["api_version"]) == (EXPORT_FORMAT, kind.NAME, api_versions(kind)[-1])
    assert sorted(doc["backends"]) == sorted(n for n, b in BUILTIN_BACKENDS.items() if b.KIND == kind.NAME)
    for entry in doc["backends"].values():
        assert sorted(entry) == ["fixtures", "unsupported"]
        assert not set(entry["fixtures"]) & set(entry["unsupported"])
        assert set(entry["fixtures"]) | set(entry["unsupported"]) <= set(doc["fixtures"])


@pytest.mark.parametrize("name", sorted(KINDS_WITH_FIXTURES))
def test_exported_cases_compose_as_a_non_python_reader_would(name):
    """Envelope + fixture spec + backend table: positive cases validate against the published schema.

    What an implementation in another language does with the file (docs/kinds.md "Exported
    fixtures"); semantic rules (the negative fixtures) are beyond JSON Schema.
    """
    kind = KINDS_WITH_FIXTURES[name]
    doc = exported(kind)
    schema = json.loads((SCHEMA_DIR / schema_file_name(kind)).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    checked = 0
    for backend, entry in doc["backends"].items():
        for fixture_name, fixture in doc["fixtures"].items():
            if fixture_name in entry["unsupported"] or fixture["refs"]:
                continue
            raw = {"api_version": doc["api_version"], "kind": doc["kind"], "backend": backend, **fixture["spec"]}
            raw[backend] = entry["fixtures"].get(fixture_name, {})
            errors = [e.message for e in validator.iter_errors(raw)]
            if fixture["expect"]["valid"]:
                assert errors == [], (backend, fixture_name)
                checked += 1
    assert checked
