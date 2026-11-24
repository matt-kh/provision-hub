"""Observed state: prov digest, state records, prov status, and the [state] envelope table."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import timedelta

import jsonschema
import pytest
from conftest import SCHEMA_DIR, UPDATE_GOLDENS

from prov import state
from prov.cli import main
from prov.schema import ConfigError

ITEM = "os-install/demo"
NOW = "2026-01-10T12:00:00Z"
REV = "0123456789abcdef0123456789abcdef01234567"


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def root(tmp_path, capsys):
    """A root with one rendered ubuntu-autoinstall item, os-install/demo."""
    r = tmp_path / "root"
    r.mkdir()
    assert run(capsys, "--root", str(r), "init", ITEM, "--backend", "ubuntu-autoinstall")[0] == 0
    assert run(capsys, "--root", str(r), "render", ITEM)[0] == 0
    return r


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    (d / "os-install").mkdir(parents=True)
    return d


def digest_of(root, ref=ITEM):
    return state.render_digest(root / ref / "rendered")


def write_record(state_dir, path_ref=ITEM, **fields):
    """Write a valid record to ``<state_dir>/<path_ref>.json``; ``fields`` override its keys (``ref`` included)."""
    record = {"api_version": state.STATE_API_VERSION, "ref": path_ref, "last_seen": "2026-01-10T11:00:00Z", "drift": {"status": "clean"}}
    record.update(fields)
    path = state_dir / f"{path_ref}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def status(capsys, root, state_dir, *extra):
    argv = ["--root", str(root), "status", "--now", NOW]
    if state_dir is not None:
        argv += ["--state-dir", str(state_dir)]
    return run(capsys, *argv, *extra)


def status_json(capsys, root, state_dir, *extra):
    code, out, err = status(capsys, root, state_dir, "--json", *extra)
    assert err == ""
    return code, json.loads(out)


# ---------------------------------------------------------------- digest


def test_digest_is_stable_and_changes_with_any_file(root, capsys):
    code, out, _ = run(capsys, "--root", str(root), "digest", ITEM)
    assert code == 0 and out == digest_of(root) + "\n"
    assert out.startswith("sha256:") and len(out.strip()) == len("sha256:") + 64
    assert run(capsys, "--root", str(root), "digest", ITEM)[1] == out
    rendered = root / ITEM / "rendered"
    (rendered / "meta-data").write_text((rendered / "meta-data").read_text() + "# changed\n")
    assert run(capsys, "--root", str(root), "digest", ITEM)[1] != out


def test_digest_includes_preserved_files_and_new_files(root):
    before = digest_of(root)
    (root / ITEM / "rendered" / "flake.lock").write_text("{}\n")
    assert digest_of(root) != before


def test_digest_ignores_file_modes(root):
    before = digest_of(root)
    os.chmod(root / ITEM / "rendered" / "user-data", 0o600)
    assert digest_of(root) == before


def test_digest_format_is_a_sha256sum_manifest(tmp_path):
    d = tmp_path / "rendered"
    (d / "sub").mkdir(parents=True)
    (d / "b").write_bytes(b"two\n")
    (d / "a").write_bytes(b"one\n")
    (d / "sub" / "c").write_bytes(b"")
    manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in [("a", b"one\n"), ("b", b"two\n"), ("sub/c", b"")])
    assert state.render_digest(d) == "sha256:" + hashlib.sha256(manifest.encode()).hexdigest()


def test_empty_rendered_digest_is_the_empty_manifest(tmp_path):
    (tmp_path / "rendered").mkdir()
    assert state.render_digest(tmp_path / "rendered") == "sha256:" + hashlib.sha256(b"").hexdigest()


@pytest.mark.skipif(not all(shutil.which(t) for t in ("find", "sort", "xargs", "sha256sum")), reason="coreutils/findutils not installed")
def test_digest_matches_the_documented_shell_command(root):
    """docs/state.md: an agent reproduces the digest with coreutils alone."""
    (root / ITEM / "rendered" / "flake.lock").write_text("{}\n")  # a nested-free extra file
    (root / ITEM / "rendered" / "dir").mkdir()
    (root / ITEM / "rendered" / "dir" / "Z-é.txt").write_text("x\n")  # non-ASCII sorts by bytes
    cmd = "find . -type f -printf '%P\\0' | LC_ALL=C sort -z | xargs -0r sha256sum -- | sha256sum"
    out = subprocess.run(["sh", "-c", cmd], cwd=root / ITEM / "rendered", capture_output=True, text=True, check=True).stdout
    assert "sha256:" + out.split()[0] == digest_of(root)


def test_digest_of_an_unrendered_item_is_exit_2(tmp_path, capsys):
    r = str(tmp_path)
    assert run(capsys, "--root", r, "init", ITEM, "--backend", "ubuntu-autoinstall")[0] == 0
    code, out, err = run(capsys, "--root", r, "digest", ITEM)
    assert code == 2 and out == "" and "not rendered yet; run prov render os-install/demo" in err


def test_digest_refuses_symlinks_and_backslashes(root, capsys):
    rendered = root / ITEM / "rendered"
    (rendered / "link").symlink_to("/etc/hostname")
    code, _, err = run(capsys, "--root", str(root), "digest", ITEM)
    assert code == 2 and "symlink, special file or unsafely named file link; run prov render" in err
    (rendered / "link").unlink()
    (rendered / "back\\slash").write_text("x")
    with pytest.raises(ConfigError, match="backslash"):
        state.render_digest(rendered)


def test_digest_json(root, capsys):
    code, out, _ = run(capsys, "--root", str(root), "digest", ITEM, "--json")
    assert code == 0 and json.loads(out) == {"format": "prov.cli/v1alpha1", "ref": ITEM, "digest": digest_of(root)}


def test_plan_prints_the_digest(root, capsys, tmp_path):
    code, out, _ = run(capsys, "--root", str(root), "plan", ITEM)
    assert code == 0 and f"# render digest: {digest_of(root)} - record it with what you apply (docs/state.md)" in out.splitlines()
    code, out, _ = run(capsys, "--root", str(root), "plan", ITEM, "--json")
    assert json.loads(out)["render_digest"] == digest_of(root)
    shutil.rmtree(root / ITEM / "rendered")
    code, out, _ = run(capsys, "--root", str(root), "plan", ITEM)
    assert code == 0 and "# render digest: none (not rendered yet) - record it with what you apply (docs/state.md)" in out
    assert json.loads(run(capsys, "--root", str(root), "plan", ITEM, "--json")[1])["render_digest"] is None


# ---------------------------------------------------------------- [state] envelope table


def test_state_table_is_optional_and_checked(root, capsys):
    spec = root / ITEM / "spec.toml"
    text = spec.read_text()
    spec.write_text(text + "\n[state]\ndecommissioned = true\n")
    assert run(capsys, "--root", str(root), "validate", ITEM)[0] == 0
    spec.write_text(text + "\n[state]\nretired = true\n")
    code, _, err = run(capsys, "--root", str(root), "validate", ITEM)
    assert code == 1 and "state" in err and "retired" in err
    spec.write_text(text + '\n[state]\ndecommissioned = "yes"\n')
    assert run(capsys, "--root", str(root), "validate", ITEM)[0] == 1


def test_state_table_does_not_change_the_render(root, capsys):
    before = digest_of(root)
    spec = root / ITEM / "spec.toml"
    spec.write_text(spec.read_text() + "\n[state]\ndecommissioned = true\n")
    code, out, _ = run(capsys, "--root", str(root), "diff", ITEM)
    assert code == 0 and "up to date" in out and digest_of(root) == before


# ---------------------------------------------------------------- records


def test_published_record_schema_matches_code():
    path = SCHEMA_DIR / state.STATE_SCHEMA_FILE
    text = state.record_schema_json()
    if UPDATE_GOLDENS:
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, "regenerate: PROV_UPDATE_GOLDENS=1 uv run pytest"
    jsonschema.Draft202012Validator.check_schema(json.loads(text))


def test_full_record_is_valid_for_both_validators(state_dir):
    path = write_record(
        state_dir,
        applied_rev=REV,
        render_digest="sha256:" + "0" * 64,
        generation="42",
        drift={"status": "drifted", "detail": "local.nix edited on the host"},
        facts={"hostname": "demo", "addresses": ["192.0.2.10/24"]},
        agent_version="0.1.0",
    )
    record, findings = state.load_record(state_dir, "os-install", "demo")
    assert findings == [] and record["generation"] == "42"
    jsonschema.Draft202012Validator(json.loads((SCHEMA_DIR / state.STATE_SCHEMA_FILE).read_text())).validate(json.loads(path.read_text()))


def test_missing_record_is_not_an_error(state_dir):
    assert state.load_record(state_dir, "os-install", "demo") == (None, [])


@pytest.mark.parametrize(
    "content,message",
    [
        ("not json", "invalid JSON"),
        ("[]", "expected one JSON object"),
        ('{"a": 1, "a": 2}', "duplicate key 'a'"),
        ('{"x": NaN}', "NaN is not valid JSON"),
        (b"\xff", "not valid UTF-8"),
    ],
)
def test_unreadable_records_are_errors(state_dir, content, message):
    path = state_dir / "os-install" / "demo.json"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    record, findings = state.load_record(state_dir, "os-install", "demo")
    assert record is None and len(findings) == 1 and message in findings[0].message and findings[0].where == "state"


@pytest.mark.parametrize(
    "fields,message",
    [
        ({"api_version": "prov.state/v9"}, "api_version"),
        ({"ref": "os-install/other"}, "does not match its path"),
        ({"last_seen": "yesterday"}, "last_seen"),
        ({"last_seen": "2026-13-01T00:00:00Z"}, "not a valid date and time"),
        ({"applied_rev": "main"}, "applied_rev"),
        ({"render_digest": "md5:abc"}, "render_digest"),
        ({"drift": {"status": "maybe"}}, "drift"),
        ({"drift": {}}, "drift"),
        ({"extra": 1}, "extra"),
        ({"drift": {"status": "drifted", "detail": "line one\nline two"}}, "control characters"),  # agents flatten to one line
    ],
)
def test_invalid_records_are_errors(state_dir, fields, message):
    write_record(state_dir, **fields)
    record, findings = state.load_record(state_dir, "os-install", "demo")
    assert record is None and findings and any(message in f.message for f in findings)


def test_symlinked_records_and_kind_dirs_are_refused(state_dir, tmp_path):
    real = write_record(tmp_path / "elsewhere")  # a valid record outside the state directory
    (state_dir / "os-install" / "demo.json").symlink_to(real)
    record, findings = state.load_record(state_dir, "os-install", "demo")
    assert record is None and "symlinks and special files are refused" in findings[0].message
    other = tmp_path / "other-state"
    other.mkdir()
    (other / "os-install").symlink_to(state_dir / "os-install")
    record, findings = state.load_record(other, "os-install", "demo")
    assert record is None and "is not a directory (symlinks are refused)" in findings[0].message


def test_oversized_record_is_refused(state_dir):
    (state_dir / "os-install" / "demo.json").write_text(" " * (state.MAX_RECORD_BYTES + 1))
    assert "larger than" in state.load_record(state_dir, "os-install", "demo")[1][0].message


# ---------------------------------------------------------------- time and duration


@pytest.mark.parametrize(
    "text,utc",
    [
        ("2026-01-10T12:00:00Z", "2026-01-10T12:00:00Z"),
        ("2026-01-10T14:00:00+02:00", "2026-01-10T12:00:00Z"),
        ("2026-01-10T11:00:00.123456789-01:00", "2026-01-10T12:00:00Z"),
    ],
)
def test_parse_time(text, utc):
    assert state.format_time(state.parse_time(text)) == utc


@pytest.mark.parametrize("text,seconds", [("90s", 90), ("15m", 900), ("72h", 259200), ("3d", 259200)])
def test_parse_duration(text, seconds):
    assert state.parse_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["0h", "3", "h", "1w", "-1d", "1.5h", ""])
def test_parse_duration_rejects(text):
    with pytest.raises(ConfigError, match="invalid duration"):
        state.parse_duration(text)


# ---------------------------------------------------------------- status


def test_status_without_state_dir_is_planned(root, capsys, monkeypatch):
    monkeypatch.delenv("PROV_STATE_DIR", raising=False)
    code, out, err = status(capsys, root, None)
    assert (code, out, err) == (0, f"{ITEM}\tplanned\t-\t-\t-\n", "")


def test_status_lifecycle_and_sync(root, state_dir, capsys):
    digest = digest_of(root)
    write_record(state_dir, applied_rev=REV, render_digest=digest)
    assert status(capsys, root, state_dir) == (0, f"{ITEM}\tactive\tin-sync\t0123456789ab\t2026-01-10T11:00:00Z\n", "")

    write_record(state_dir, applied_rev=REV, render_digest="sha256:" + "0" * 64)
    assert status(capsys, root, state_dir)[1].split("\t")[1:3] == ["active", "pending"]

    write_record(state_dir, render_digest=digest, drift={"status": "drifted", "detail": "x"})
    assert status(capsys, root, state_dir)[1].split("\t")[1:3] == ["active", "drifted"]

    write_record(state_dir, render_digest=digest, drift={"status": "unknown"})
    assert status(capsys, root, state_dir)[1].split("\t")[1:3] == ["active", "unknown"]

    write_record(state_dir)  # no render_digest reported
    assert status(capsys, root, state_dir)[1].split("\t")[1:3] == ["active", "unknown"]

    write_record(state_dir, render_digest=digest, last_seen="2026-01-07T11:59:59Z")  # older than 72h
    assert status(capsys, root, state_dir)[1].split("\t")[1:3] == ["stale", "in-sync"]
    assert status(capsys, root, state_dir, "--stale-after", "4d")[1].split("\t")[1] == "active"


def test_status_decommissioned(root, state_dir, capsys):
    spec = root / ITEM / "spec.toml"
    spec.write_text(spec.read_text() + "\n[state]\ndecommissioned = true\n")
    assert status(capsys, root, state_dir)[1].split("\t")[1] == "decommissioned"
    write_record(state_dir, render_digest=digest_of(root))
    code, out, err = status(capsys, root, state_dir)
    assert code == 0 and out.split("\t")[1] == "decommissioned" and "decommissioned, but its host still reports" in err
    write_record(state_dir, last_seen="2026-01-01T00:00:00Z")
    code, out, err = status(capsys, root, state_dir)
    assert code == 0 and out.split("\t")[1] == "decommissioned" and err == ""


def test_status_invalid_record_is_exit_1(root, state_dir, capsys):
    write_record(state_dir, ref="os-install/other")
    code, out, err = status(capsys, root, state_dir)
    assert code == 1 and out == f"{ITEM}\tinvalid\t-\t-\t-\n" and "does not match its path" in err


def test_status_invalid_spec_is_a_row_not_a_crash(root, state_dir, capsys):
    spec = root / ITEM / "spec.toml"
    spec.write_text(spec.read_text().replace('hostname = "demo"', "hostname = 7"))
    code, out, err = status(capsys, root, state_dir)
    assert code == 1 and out == f"{ITEM}\tinvalid\t-\t-\t-\n" and "hostname" in err
    spec.write_text(spec.read_text().replace('backend = "ubuntu-autoinstall"', 'backend = "nope"'))
    code, out, err = status(capsys, root, state_dir)
    assert code == 1 and out.split("\t")[1] == "invalid" and "nope" in err


def test_status_unrendered_item(root, state_dir, capsys):
    shutil.rmtree(root / ITEM / "rendered")
    write_record(state_dir, render_digest="sha256:" + "0" * 64)
    assert status(capsys, root, state_dir) == (0, f"{ITEM}\tactive\tunknown\t-\t2026-01-10T11:00:00Z\n", "")


def test_status_future_last_seen_warns(root, state_dir, capsys):
    write_record(state_dir, last_seen="2026-01-10T13:00:00Z")
    code, out, err = status(capsys, root, state_dir)
    assert code == 0 and out.split("\t")[1] == "active" and "is in the future" in err


def test_status_reports_orphan_records(root, state_dir, capsys):
    write_record(state_dir, path_ref="os-install/gone")
    (state_dir / "os-install" / "notes.txt").write_text("ignored")
    code, out, err = status(capsys, root, state_dir)
    assert code == 0 and out.startswith(f"{ITEM}\tplanned") and "os-install/gone" in err and "has no item" in err
    assert "notes" not in err
    # asking for named refs skips the orphan scan
    assert status(capsys, root, state_dir, ITEM)[2] == ""


def test_status_named_refs(root, state_dir, capsys):
    code, out, _ = status(capsys, root, state_dir, ITEM)
    assert code == 0 and out.startswith(ITEM)
    code, _, err = status(capsys, root, state_dir, "os-install/missing")
    assert code == 2 and "not found" in err
    code, _, err = status(capsys, root, state_dir, "../etc")
    assert code == 2 and "invalid ref" in err


def test_status_json(root, state_dir, capsys):
    digest = digest_of(root)
    write_record(state_dir, applied_rev=REV, render_digest=digest, facts={"cpu": "example"})
    code, doc = status_json(capsys, root, state_dir)
    assert code == 0
    assert doc["format"] == "prov.cli/v1alpha1" and doc["now"] == NOW and doc["stale_after_seconds"] == 259200
    assert doc["state_dir"] == str(state_dir.resolve()) and doc["findings"] == []
    (item,) = doc["items"]
    assert item == {
        "ref": ITEM,
        "backend": "ubuntu-autoinstall",
        "lifecycle": "active",
        "sync": "in-sync",
        "desired_digest": digest,
        "decommissioned": False,
        "record": json.loads((state_dir / f"{ITEM}.json").read_text()),
    }
    write_record(state_dir, ref="os-install/other")
    code, doc = status_json(capsys, root, state_dir)
    assert code == 1 and doc["items"][0]["lifecycle"] == "invalid" and doc["items"][0]["record"] is None
    assert doc["findings"][0]["level"] == "error" and doc["findings"][0]["where"] == f"{ITEM}: state"


def test_status_state_dir_from_environment(root, state_dir, capsys, monkeypatch):
    write_record(state_dir, render_digest=digest_of(root))
    monkeypatch.setenv("PROV_STATE_DIR", str(state_dir))
    assert status(capsys, root, None)[1].split("\t")[1:3] == ["active", "in-sync"]


@pytest.mark.parametrize(
    "argv,message",
    [
        (["--state-dir", "/nonexistent/state"], "does not exist or is not a directory"),
        (["--stale-after", "soon"], "invalid duration"),
        (["--now", "today"], "is not an RFC 3339 timestamp"),
    ],
)
def test_status_usage_errors(root, capsys, argv, message):
    code, out, err = run(capsys, "--root", str(root), "status", *argv)
    assert code == 2 and out == "" and message in err


def test_status_uses_the_clock_without_now(root, state_dir, capsys):
    write_record(state_dir, last_seen="2000-01-01T00:00:00Z")
    code, out, _ = run(capsys, "--root", str(root), "status", "--state-dir", str(state_dir))
    assert code == 0 and out.split("\t")[1] == "stale"


def test_status_runs_nothing_and_writes_nothing(root, state_dir, capsys, monkeypatch):
    write_record(state_dir, render_digest=digest_of(root))

    def refuse(*args, **kwargs):
        raise AssertionError(f"prov status tried to execute something: {args!r}")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, refuse)
    monkeypatch.setattr(os, "system", refuse)
    before = {p: p.stat().st_mtime_ns for p in list(root.rglob("*")) + list(state_dir.rglob("*"))}
    assert status(capsys, root, state_dir)[0] == 0
    assert status(capsys, root, state_dir, "--json")[0] == 0
    assert run(capsys, "--root", str(root), "digest", ITEM)[0] == 0
    after = {p: p.stat().st_mtime_ns for p in list(root.rglob("*")) + list(state_dir.rglob("*"))}
    assert before == after


# ---------------------------------------------------------------- review hardening


def test_parse_time_refuses_out_of_range_and_non_ascii():
    for text in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00", "２０２６-01-01T00:00:00Z", "2026-02-30T00:00:00Z"):
        with pytest.raises(ValueError):
            state.parse_time(text)
    assert state.format_time(state.parse_time("0999-12-31T23:59:59Z")) == "0999-12-31T23:59:59Z"


@pytest.mark.parametrize("last_seen", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_out_of_range_last_seen_is_an_invalid_record(root, state_dir, capsys, last_seen):
    write_record(state_dir, last_seen=last_seen)
    code, out, err = status(capsys, root, state_dir)
    assert code == 1 and out == f"{ITEM}\tinvalid\t-\t-\t-\n" and "not a valid date and time" in err and "Traceback" not in err


@pytest.mark.parametrize("now", ["0001-01-01T00:00:00+01:00", "２０２６-01-01T00:00:00Z"])
def test_out_of_range_or_non_ascii_now_is_a_usage_error(root, capsys, now):
    code, out, err = run(capsys, "--root", str(root), "status", "--now", now, "--json")
    assert code == 2 and out == "" and "is not an RFC 3339 timestamp" in err


needs_permissions = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads everything")


@needs_permissions
def test_unreadable_rendered_file_is_an_item_error(root, state_dir, capsys):
    target = root / ITEM / "rendered" / "user-data"
    os.chmod(target, 0)
    try:
        code, out, err = status(capsys, root, state_dir)
        assert code == 1 and out == f"{ITEM}\tplanned\t-\t-\t-\n" and "cannot read user-data" in err
        code, out, err = run(capsys, "--root", str(root), "plan", ITEM)
        assert code == 0 and "# render digest: none (" in out
        assert run(capsys, "--root", str(root), "digest", ITEM)[0] == 2
    finally:
        os.chmod(target, 0o644)


@needs_permissions
def test_unlistable_rendered_directory_has_no_digest(root, capsys):
    sub = root / ITEM / "rendered" / "sub"
    sub.mkdir()
    (sub / "f").write_text("x\n")
    os.chmod(sub, 0)
    try:
        code, out, err = run(capsys, "--root", str(root), "digest", ITEM)
        assert code == 2 and out == "" and "cannot read" in err
    finally:
        os.chmod(sub, 0o755)


def test_item_with_invalid_toml_gets_an_invalid_row(root, state_dir, capsys):
    bad = root / "os-install" / "broken"
    bad.mkdir()
    (bad / "spec.toml").write_text("api_version = \n")
    code, out, err = status(capsys, root, state_dir)
    assert code == 1 and "os-install/broken\tinvalid\t-\t-\t-\n" in out and f"{ITEM}\tplanned" in out
    assert err.count("os-install/broken") == 1  # reported once, by the scan
    code, doc = status_json(capsys, root, state_dir)
    assert [i["ref"] for i in doc["items"]] == ["os-install/broken", ITEM] and doc["items"][0]["lifecycle"] == "invalid"


def test_pending_wins_over_unknown_drift(root, state_dir, capsys):
    write_record(state_dir, render_digest="sha256:" + "0" * 64, drift={"status": "unknown"})
    assert status(capsys, root, state_dir)[1].split("\t")[2] == "pending"


def test_invalid_record_wins_over_decommissioned(root, state_dir, capsys):
    spec = root / ITEM / "spec.toml"
    spec.write_text(spec.read_text() + "\n[state]\ndecommissioned = true\n")
    write_record(state_dir, ref="os-install/other")
    code, out, _ = status(capsys, root, state_dir)
    assert code == 1 and out.split("\t")[1] == "invalid"


def test_empty_state_dir_argument_means_no_records(root, state_dir, capsys, monkeypatch):
    write_record(state_dir)
    monkeypatch.setenv("PROV_STATE_DIR", str(state_dir))
    code, out, _ = run(capsys, "--root", str(root), "status", "--state-dir", "", "--now", NOW)
    assert code == 0 and out.split("\t")[1] == "planned"
