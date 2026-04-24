"""Tests for the filter chain and individual filter functions."""

from __future__ import annotations

import pytest

from gh_autofork.config import FilterConfig
from gh_autofork.filters import (
    FilterChain,
    created_after,
    created_before,
    description_pattern,
    exclude_archived,
    exclude_forks,
    exclude_private,
    has_license,
    language_allowlist,
    language_blocklist,
    max_days_since_push,
    max_forks,
    max_size_kb,
    max_stars,
    min_forks,
    min_size_kb,
    min_stars,
    name_pattern,
    not_own_repo,
    topics_any,
    topics_none,
)
from gh_autofork.github_client import RepoInfo
from tests.conftest import make_repo_raw


def repo(**kwargs) -> RepoInfo:
    return RepoInfo(make_repo_raw(**kwargs))


# ---------------------------------------------------------------------------
# Individual filters
# ---------------------------------------------------------------------------

class TestMinMaxStars:
    def test_min_stars_pass(self):
        assert min_stars(50)(repo(stars=100)) is True

    def test_min_stars_fail(self):
        assert min_stars(200)(repo(stars=100)) is False

    def test_max_stars_pass(self):
        assert max_stars(500)(repo(stars=100)) is True

    def test_max_stars_fail(self):
        assert max_stars(50)(repo(stars=100)) is False


class TestLanguageFilters:
    def test_allowlist_match(self):
        assert language_allowlist(["python", "go"])(repo(language="Python")) is True

    def test_allowlist_no_match(self):
        assert language_allowlist(["go", "rust"])(repo(language="Python")) is False

    def test_allowlist_case_insensitive(self):
        assert language_allowlist(["PYTHON"])(repo(language="python")) is True

    def test_blocklist_blocks(self):
        assert language_blocklist(["php"])(repo(language="PHP")) is False

    def test_blocklist_allows(self):
        assert language_blocklist(["php"])(repo(language="Python")) is True

    def test_allowlist_no_language(self):
        raw = make_repo_raw()
        raw["language"] = None
        r = RepoInfo(raw)
        assert language_allowlist(["python"])(r) is False


class TestTopicFilters:
    def test_topics_any_match(self):
        assert topics_any(["ml"])(repo(topics=["ml", "deep-learning"])) is True

    def test_topics_any_no_match(self):
        assert topics_any(["blockchain"])(repo(topics=["ml"])) is False

    def test_topics_none_blocks(self):
        assert topics_none(["deprecated"])(repo(topics=["deprecated", "ml"])) is False

    def test_topics_none_allows(self):
        assert topics_none(["deprecated"])(repo(topics=["ml"])) is True

    def test_topics_any_empty_repo_topics(self):
        assert topics_any(["ml"])(repo(topics=[])) is False


class TestSizeFilters:
    def test_min_size_pass(self):
        raw = make_repo_raw()
        raw["size"] = 5000
        assert min_size_kb(1000)(RepoInfo(raw)) is True

    def test_min_size_fail(self):
        raw = make_repo_raw()
        raw["size"] = 100
        assert min_size_kb(1000)(RepoInfo(raw)) is False

    def test_max_size_pass(self):
        raw = make_repo_raw()
        raw["size"] = 500
        assert max_size_kb(1000)(RepoInfo(raw)) is True


class TestBooleanFilters:
    def test_exclude_archived(self):
        assert exclude_archived()(repo(is_archived=True)) is False
        assert exclude_archived()(repo(is_archived=False)) is True

    def test_exclude_forks(self):
        assert exclude_forks()(repo(is_fork=True)) is False
        assert exclude_forks()(repo(is_fork=False)) is True

    def test_exclude_private(self):
        assert exclude_private()(repo(is_private=True)) is False
        assert exclude_private()(repo(is_private=False)) is True

    def test_not_own_repo(self):
        assert not_own_repo("alice")(repo(full_name="alice/project")) is False
        assert not_own_repo("alice")(repo(full_name="bob/project")) is True

    def test_has_license(self):
        assert has_license()(repo()) is True
        raw = make_repo_raw()
        raw["license"] = None
        assert has_license()(RepoInfo(raw)) is False


class TestDateFilters:
    def test_max_days_since_push_fresh(self):
        f = max_days_since_push(30)
        r = repo(pushed_at="2099-01-01T00:00:00Z")
        assert f(r) is True

    def test_max_days_since_push_old(self):
        f = max_days_since_push(1)
        r = repo(pushed_at="2000-01-01T00:00:00Z")
        assert f(r) is False

    def test_max_days_since_push_no_date(self):
        f = max_days_since_push(1)
        raw = make_repo_raw()
        raw["pushed_at"] = None
        assert f(RepoInfo(raw)) is True   # unknown → don't exclude

    def test_created_after(self):
        f = created_after("2020-01-01")
        assert f(repo()) is True    # created_at="2020-01-01T00:00:00Z"
        raw = make_repo_raw()
        raw["created_at"] = "2010-01-01T00:00:00Z"
        assert f(RepoInfo(raw)) is False

    def test_created_before(self):
        f = created_before("2030-01-01")
        assert f(repo()) is True
        raw = make_repo_raw()
        raw["created_at"] = "2040-01-01T00:00:00Z"
        assert f(RepoInfo(raw)) is False


class TestRegexFilters:
    def test_description_pattern_match(self):
        raw = make_repo_raw()
        raw["description"] = "A fast ML library for Python"
        assert description_pattern(r"machine|ML")(RepoInfo(raw)) is True

    def test_description_pattern_no_match(self):
        raw = make_repo_raw()
        raw["description"] = "A database driver"
        assert description_pattern(r"machine|ML")(RepoInfo(raw)) is False

    def test_description_pattern_empty_desc(self):
        raw = make_repo_raw()
        raw["description"] = None
        assert description_pattern(r"ML")(RepoInfo(raw)) is False

    def test_name_pattern_match(self):
        assert name_pattern(r"^awesome-")(repo(full_name="alice/awesome-stuff")) is True

    def test_name_pattern_no_match(self):
        assert name_pattern(r"^awesome-")(repo(full_name="alice/boring-stuff")) is False


# ---------------------------------------------------------------------------
# FilterChain
# ---------------------------------------------------------------------------

class TestFilterChain:
    def test_empty_chain_passes_all(self):
        chain = FilterChain()
        assert chain(repo()) is True

    def test_all_pass(self):
        chain = FilterChain([min_stars(10), exclude_archived()])
        assert chain(repo(stars=100, is_archived=False)) is True

    def test_one_fails(self):
        chain = FilterChain([min_stars(10), exclude_archived()])
        assert chain(repo(stars=100, is_archived=True)) is False

    def test_explain_empty(self):
        chain = FilterChain([min_stars(1000)])
        rejections = chain.explain(repo(stars=5))
        assert len(rejections) == 1
        assert "min_stars" in rejections[0]

    def test_add_returns_self(self):
        chain = FilterChain()
        result = chain.add(min_stars(1))
        assert result is chain

    def test_add_if_conditional(self):
        chain = FilterChain()
        chain.add_if(True,  min_stars(1))
        chain.add_if(False, min_stars(999999))
        assert chain(repo(stars=100)) is True

    def test_len(self):
        chain = FilterChain([min_stars(1), max_stars(1000)])
        assert len(chain) == 2

    def test_from_config_honours_flags(self):
        cfg = FilterConfig(
            exclude_archived=True,
            exclude_forks=True,
            min_stars=50,
        )
        chain = FilterChain.from_config(cfg)
        assert chain(repo(is_archived=True)) is False
        assert chain(repo(is_fork=True)) is False
        assert chain(repo(stars=10)) is False
        assert chain(repo(stars=100, is_archived=False, is_fork=False)) is True

    def test_from_config_language_filter(self):
        cfg = FilterConfig(languages=["Python", "Go"])
        chain = FilterChain.from_config(cfg)
        assert chain(repo(language="Python")) is True
        assert chain(repo(language="Java")) is False

    def test_from_config_auth_login_excludes_own(self):
        cfg = FilterConfig()
        chain = FilterChain.from_config(cfg, auth_login="myuser")
        assert chain(repo(full_name="myuser/project")) is False
        assert chain(repo(full_name="otheruser/project")) is True


# ---------------------------------------------------------------------------
# FilterChain.from_expression DSL
# ---------------------------------------------------------------------------

class TestDSLParser:
    def test_stars_ge(self):
        chain = FilterChain.from_expression("stars >= 100")
        assert chain(repo(stars=200)) is True
        assert chain(repo(stars=50)) is False

    def test_language(self):
        chain = FilterChain.from_expression("language python")
        assert chain(repo(language="Python")) is True
        assert chain(repo(language="Go")) is False

    def test_not_language(self):
        chain = FilterChain.from_expression("not language php")
        assert chain(repo(language="PHP")) is False
        assert chain(repo(language="Python")) is True

    def test_no_archived(self):
        chain = FilterChain.from_expression("no archived archived")
        assert chain(repo(is_archived=True)) is False

    def test_topics(self):
        chain = FilterChain.from_expression("topics machine-learning")
        assert chain(repo(topics=["machine-learning"])) is True
        assert chain(repo(topics=["blockchain"])) is False

    def test_min_size(self):
        chain = FilterChain.from_expression("min_size 500")
        raw = make_repo_raw()
        raw["size"] = 1000
        assert chain(RepoInfo(raw)) is True
        raw["size"] = 100
        assert chain(RepoInfo(raw)) is False

    def test_pushed_within(self):
        chain = FilterChain.from_expression("pushed_within 30")
        assert chain(repo(pushed_at="2099-01-01T00:00:00Z")) is True
        assert chain(repo(pushed_at="2000-01-01T00:00:00Z")) is False

    def test_comment_lines_ignored(self):
        chain = FilterChain.from_expression("# this is a comment\nstars >= 1")
        assert len(chain) == 1

    def test_blank_lines_ignored(self):
        chain = FilterChain.from_expression("\n\nstars >= 1\n\n")
        assert len(chain) == 1

    def test_multiple_rules(self):
        chain = FilterChain.from_expression("""
            stars >= 100
            language python
            no archived archived
        """)
        assert len(chain) == 3
