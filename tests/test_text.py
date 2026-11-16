"""prov.text: the one predicate and escaper for everything prov prints."""

from __future__ import annotations

from prov.text import esc, has_unsafe, short_repr, show, unsafe


def test_unsafe_predicate():
    for ch in ("\t", "\n", "\r", "\x00", "\x1b", "\x7f", "\x85", "\x9b", " ", "​", "‮", " ", " ", "\udcff", "\U000e0001", "\U0010ffff"):
        assert unsafe(ch), repr(ch)
    for ch in (" ", "a", "é", "~", "\\", "中", "\U0001f600"):
        assert not unsafe(ch), repr(ch)
    assert has_unsafe("a‮b") and not has_unsafe("plain name.txt")


def test_esc_replaces_each_unsafe_char_by_its_escape():
    cases = {
        "a\nb": "a\\x0ab",
        "a\x1b[2Kb": "a\\x1b[2Kb",
        "a b": "a\\u2028b",
        "a\x9bb": "a\\x9bb",
        "a‮b": "a\\u202eb",
        "a​b": "a\\u200bb",
        "s\udcff": "s\\udcff",
        "a\U000e0001b": "a\\U000e0001b",
        "a b": "a\\xa0b",
    }
    for raw, expected in cases.items():
        assert esc(raw) == expected and show(raw) == expected and expected.isprintable()
    assert show("user-data") == "user-data" and show("a b") == "a b" and show("ünïcødé") == "ünïcødé"
    assert esc("one\ntwo\tthree\x1b", keep="\n") == "one\ntwo\\x09three\\x1b"
    # the result always encodes, even with a strict codec (lone surrogates are escaped)
    show("s\udcff\U0001f600").encode("utf-8", "strict")


def test_short_repr_truncates_before_repr_and_caps_the_output():
    assert short_repr("ab") == "'ab'"
    assert short_repr("v" * 81, limit=80) == "'" + "v" * 80 + "'…"
    out = short_repr("\x1b" * 100)  # every char repr's to 4: cut before repr, never mid-escape
    assert out == repr("\x1b" * 20) + "…" and len(out) <= 83
    for value in ("\x1b" * 1000, "\U0001f600‮" * 500, ["x" * 10] * 50, {"k": "\n" * 300}, "\udcff" * 99):
        out = short_repr(value)
        assert len(out) <= 83 and out.endswith("…") and out.isprintable(), out
