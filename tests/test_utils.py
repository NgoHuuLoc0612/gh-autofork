"""Tests for utility functions."""

from __future__ import annotations

import pytest

from gh_autofork.utils import (
    chunked,
    format_duration,
    human_size,
    parse_repo_file,
    parse_repo_name,
    pluralise,
    truncate,
)


# ---------------------------------------------------------------------------
# parse_repo_name
# ---------------------------------------------------------------------------

class TestParseRepoName:
    @pytest.mark.parametrize("raw, expected", [
        ("torvalds/linux",                       "torvalds/linux"),
        ("https://github.com/torvalds/linux",    "torvalds/linux"),
        ("https://github.com/torvalds/linux.git","torvalds/linux"),
        ("git@github.com:torvalds/linux.git",    "torvalds/linux"),
        ("  torvalds/linux  ",                   "torvalds/linux"),
        ("microsoft/vscode",                      "microsoft/vscode"),
        ("user-name/repo.name",                  "user-name/repo.name"),
    ])
    def test_valid_formats(self, raw, expected):
        assert parse_repo_name(raw) == expected

    @pytest.mark.parametrize("raw", [
        "",
        "# comment",
        "not-a-repo",
        "https://example.com/not-github",
        "just-one-word",
    ])
    def test_invalid_returns_none(self, raw):
        assert parse_repo_name(raw) is None


# ---------------------------------------------------------------------------
# parse_repo_file
# ---------------------------------------------------------------------------

class TestParseRepoFile:
    def test_parses_simple_file(self, tmp_path):
        f = tmp_path / "repos.txt"
        f.write_text("alice/repo1\nbob/repo2\n")
        result = parse_repo_file(str(f))
        assert result == ["alice/repo1", "bob/repo2"]

    def test_skips_comments_and_blanks(self, tmp_path):
        f = tmp_path / "repos.txt"
        f.write_text("# comment\nalice/repo1\n\nbob/repo2\n")
        result = parse_repo_file(str(f))
        assert result == ["alice/repo1", "bob/repo2"]

    def test_handles_github_urls(self, tmp_path):
        f = tmp_path / "repos.txt"
        f.write_text("https://github.com/alice/repo1\n")
        result = parse_repo_file(str(f))
        assert result == ["alice/repo1"]

    def test_raises_on_unparseable(self, tmp_path):
        f = tmp_path / "repos.txt"
        f.write_text("alice/repo1\njust-garbage\nbob/repo2\n")
        with pytest.raises(ValueError, match="line 2"):
            parse_repo_file(str(f))


# ---------------------------------------------------------------------------
# human_size
# ---------------------------------------------------------------------------

class TestHumanSize:
    def test_bytes_under_1kb(self):
        assert "B" in human_size(0, is_kb=False)

    def test_kb(self):
        result = human_size(512, is_kb=True)
        assert "KB" in result or "MB" in result

    def test_large_size(self):
        result = human_size(1024 * 1024, is_kb=True)   # 1 GB in KB
        assert "GB" in result


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_string_unchanged(self):
        assert truncate("hello", 10) == "hello"

    def test_long_string_truncated(self):
        s = "a" * 100
        result = truncate(s, 20)
        assert len(result) <= 20
        assert result.endswith("…")

    def test_exact_length(self):
        s = "hello"
        assert truncate(s, 5) == "hello"


# ---------------------------------------------------------------------------
# pluralise
# ---------------------------------------------------------------------------

class TestPluralise:
    def test_singular(self):
        assert pluralise(1, "repo") == "1 repo"

    def test_plural_default(self):
        assert pluralise(5, "repo") == "5 repos"

    def test_plural_explicit(self):
        assert pluralise(2, "repository", "repositories") == "2 repositories"

    def test_zero(self):
        assert pluralise(0, "repo") == "0 repos"


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------

class TestFormatDuration:
    def test_seconds(self):
        assert "s" in format_duration(45.3)

    def test_minutes(self):
        result = format_duration(90)
        assert "m" in result

    def test_hours(self):
        result = format_duration(3700)
        assert "h" in result


# ---------------------------------------------------------------------------
# chunked
# ---------------------------------------------------------------------------

class TestChunked:
    def test_even_split(self):
        result = list(chunked(range(6), 2))
        assert result == [[0, 1], [2, 3], [4, 5]]

    def test_uneven_split(self):
        result = list(chunked(range(7), 3))
        assert result == [[0, 1, 2], [3, 4, 5], [6]]

    def test_empty(self):
        assert list(chunked([], 3)) == []

    def test_larger_than_iterable(self):
        result = list(chunked([1, 2], 10))
        assert result == [[1, 2]]
