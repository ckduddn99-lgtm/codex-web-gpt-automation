import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("check_docs", ROOT / "scripts" / "check_docs.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_public_docs_brand_assets_and_versions_are_consistent() -> None:
    assert MODULE.check_repository(ROOT) == []


def test_social_preview_has_github_recommended_dimensions() -> None:
    preview = ROOT / "docs" / "assets" / "brand" / "social-preview.png"
    assert MODULE._png_dimensions(preview) == (1280, 640)


def test_changelog_backfill_covers_every_audited_change_commit() -> None:
    # This historical audit is independent of Git availability in source archives.
    # All 22 non-merge commits integrated Sep 2-5 through aecfd5a must stay visible.
    text = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    journal = text.split("<!-- dated-work-log:start -->", 1)[1].split(
        "<!-- dated-work-log:end -->", 1
    )[0]
    commits = {
        "aecfd5a", "721fac9", "0e7a9d0", "6180ec0", "e9ab6f9",
        "fa94ddd", "ee2a797", "5c2db3c", "d8c55ab", "9d8f53f",
        "10e3402", "c33f042", "fcf03ac", "7241ef5", "59da964",
        "b232e2e", "8153fda", "921d9b6", "076585e", "cdc148b",
        "a72acf6", "363f610",
    }
    assert len(commits) == 22
    for commit in sorted(commits):
        assert f"`{commit}`" in journal, f"missing historical changelog entry: {commit}"
    for day in ("2026-09-05", "2026-09-04", "2026-09-03", "2026-09-02"):
        assert f"### {day}" in journal
    assert "KST" in journal


def test_changelog_backfill_preserves_historical_validation_limits() -> None:
    text = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    journal = text.split("<!-- dated-work-log:start -->", 1)[1].split(
        "<!-- dated-work-log:end -->", 1
    )[0]
    # These are dated historical observations, never fresh test/release claims.
    for observation in (
        "146 passed", "618 passed", "9 skipped", "1 deselected", "1 warning",
        "389.92", "22.61", "exit_code=3", "100", "210",
        "web_search_verified=false", "solution_verified=false", "Unreleased",
    ):
        assert observation in journal, f"lost historical limitation: {observation}"
    assert "<!-- pending-work:start -->" in text
    assert "<!-- pending-work:end -->" in text
