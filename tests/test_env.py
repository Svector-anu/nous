"""Loading credentials from .env.

The failure this guards against is quiet: a key that loads but arrives mangled fails auth
in a way that is indistinguishable from a wrong key, which is an expensive thing to debug
against a remote gateway.
"""

from __future__ import annotations

import os

from src.env import load_env


def test_missing_file_is_not_an_error(tmp_path):
    """The sim runs without any credentials at all; a missing .env is the normal case."""
    assert load_env(tmp_path / "nope.env") == {}


def test_values_reach_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("DGRID_API_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text("DGRID_API_KEY=sk-abc123\n")

    applied = load_env(path)

    assert applied == {"DGRID_API_KEY": "sk-abc123"}
    assert os.environ["DGRID_API_KEY"] == "sk-abc123"


def test_an_inline_variable_beats_the_file(tmp_path, monkeypatch):
    """`DGRID_API_KEY=... python -m src.main` has to win, or a stale file silently
    overrides the key you just passed on purpose."""
    monkeypatch.setenv("DGRID_API_KEY", "sk-from-the-shell")
    path = tmp_path / ".env"
    path.write_text("DGRID_API_KEY=sk-from-the-file\n")

    load_env(path)

    assert os.environ["DGRID_API_KEY"] == "sk-from-the-shell"


def test_override_is_available_when_asked_for(tmp_path, monkeypatch):
    monkeypatch.setenv("DGRID_API_KEY", "sk-from-the-shell")
    path = tmp_path / ".env"
    path.write_text("DGRID_API_KEY=sk-from-the-file\n")

    load_env(path, override=True)

    assert os.environ["DGRID_API_KEY"] == "sk-from-the-file"


def test_quotes_and_whitespace_are_stripped(tmp_path, monkeypatch):
    """A quoted value that keeps its quotes authenticates as a different string and fails
    exactly like a wrong key."""
    for name in ("A_KEY", "B_KEY", "C_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / ".env"
    path.write_text('A_KEY="sk-double"\nB_KEY=\'sk-single\'\n  C_KEY =  sk-spaced  \n')

    load_env(path)

    assert os.environ["A_KEY"] == "sk-double"
    assert os.environ["B_KEY"] == "sk-single"
    assert os.environ["C_KEY"] == "sk-spaced"


def test_comments_and_blank_lines_are_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("REAL_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text("# a comment\n\n   \nREAL_KEY=value\n")

    applied = load_env(path)

    assert applied == {"REAL_KEY": "value"}


def test_a_value_containing_equals_survives(tmp_path, monkeypatch):
    """Base64-ish secrets carry '='; splitting on every one truncates the key."""
    monkeypatch.delenv("PADDED", raising=False)
    path = tmp_path / ".env"
    path.write_text("PADDED=abc==\n")

    load_env(path)

    assert os.environ["PADDED"] == "abc=="


def test_the_example_template_is_tracked_and_the_real_file_is_not():
    """The one that matters: .env.example ships, .env must never be committed."""
    from pathlib import Path
    import subprocess

    root = Path(__file__).resolve().parents[1]
    assert (root / ".env.example").exists(), "the template has to exist to be copied"

    ignored = subprocess.run(
        ["git", "check-ignore", ".env"], cwd=root, capture_output=True, text=True
    )
    assert ignored.returncode == 0, ".env is not gitignored; a real key could be committed"
