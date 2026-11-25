"""profile.md and workspace.md: a UTF-8 byte-order mark at the top is stripped, and each item of an identity list is
its text exactly as written."""

from __future__ import annotations

from pathlib import Path

import pytest

from cerebro.compile import _workspace
from cerebro.project import WorkspaceError, _identity_list, read_ours

BOM = "﻿"
PROFILE = "---\nidentity:\n  email: [sam@quillmoor.example]\n  gong_id: [100000000000001]\n---\n\nBrief me.\n"
WORKSPACE = "---\nidentity:\n  domains: [quillmoor.example]\n---\n\nThe fiscal year starts February 1.\n"


@pytest.mark.parametrize("mark", ["", BOM])
def test_the_same_file_reads_the_same_with_and_without_a_byte_order_mark(tmp_path: Path, mark: str) -> None:
    (tmp_path / "profile.md").write_text(mark + PROFILE, encoding="utf-8")
    (tmp_path / "workspace.md").write_text(mark + WORKSPACE, encoding="utf-8")
    ours = read_ours(tmp_path)
    assert ours.addresses == ("sam@quillmoor.example",) and ours.domains == ("quillmoor.example",)
    assert _identity_list(tmp_path / "profile.md", "gong_id") == ("100000000000001",)
    assert _workspace(tmp_path) == "The fiscal year starts February 1."  # the body, its front matter left out


def test_each_item_of_an_identity_list_is_its_text_as_written(tmp_path: Path) -> None:
    """YAML 1.1 reads `007` as the number 7 (8 for `010`, an octal) and `1e5` as text; each is kept as written, so an
    id that looks like a number is never changed. A null item is dropped."""
    profile = tmp_path / "profile.md"
    profile.write_text("---\nidentity:\n  gong_id: [100000000000001, 007, 010, 1e5, 0x1F, true, yes, ~, 1.50, \"007\", "
                       "1_000, .5, -0]\n---\n")
    assert _identity_list(profile, "gong_id") == ("100000000000001", "007", "010", "1e5", "0x1f", "true", "yes", "1.50",
                                                  "007", "1_000", ".5", "-0")  # in lower case, as every item is
    profile.write_text("---\nidentity:\n  gong_id: 007\n---\n")
    assert _identity_list(profile, "gong_id") == ("007",)


def test_a_list_nested_in_an_identity_list_is_still_refused(tmp_path: Path) -> None:
    profile = tmp_path / "profile.md"
    profile.write_text("---\nidentity:\n  gong_id: [[1]]\n---\n")
    with pytest.raises(WorkspaceError, match="write a value, not a list or a map"):
        _identity_list(profile, "gong_id")


@pytest.mark.parametrize("written", ["email: [~]", "email: [null]", "email: [null, ~]", "email:\n    -"])
def test_an_identity_list_whose_every_item_is_null_reads_as_an_empty_list(
        tmp_path: Path, written: str) -> None:
    (tmp_path / "profile.md").write_text("---\nidentity:\n  " + written + "\n---\n\nBrief me.\n")
    assert _identity_list(tmp_path / "profile.md", "email") == ()
    assert read_ours(tmp_path).addresses == ()
