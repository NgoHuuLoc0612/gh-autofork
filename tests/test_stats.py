"""Tests for statistics collection and formatting."""

from __future__ import annotations

import json

import pytest

from gh_autofork.stats import (
    ForkQueueStats,
    collect_stats,
    format_stats_text,
    stats_to_dict,
    stats_to_json,
)


class TestForkQueueStats:
    def test_success_rate_zero_completed(self):
        stats = ForkQueueStats(total=5, pending=5, running=0,
                               success=0, failed=0, skipped=0, cancelled=0)
        assert stats.success_rate_pct == 0.0

    def test_success_rate_all_success(self):
        stats = ForkQueueStats(total=10, pending=0, running=0,
                               success=10, failed=0, skipped=0, cancelled=0)
        assert stats.success_rate_pct == 100.0

    def test_success_rate_mixed(self):
        stats = ForkQueueStats(total=10, pending=0, running=0,
                               success=8, failed=2, skipped=0, cancelled=0)
        assert abs(stats.success_rate_pct - 80.0) < 0.01


class TestCollectStats:
    def test_returns_full_stats(self, db):
        stats = collect_stats(db, client=None)
        assert stats.generated_at != ""
        assert isinstance(stats.queue, ForkQueueStats)
        assert isinstance(stats.top_languages, list)
        assert isinstance(stats.job_history, list)

    def test_queue_counts_correct(self, db):
        fid1 = db.enqueue_fork("a/r1")
        fid2 = db.enqueue_fork("a/r2")
        db.mark_fork_success(fid1, "me/r1")

        stats = collect_stats(db, client=None)
        assert stats.queue.success == 1
        assert stats.queue.pending == 1
        assert stats.queue.total   == 2

    def test_watchlist_included(self, db):
        db.upsert_watch("user", "alice")
        db.upsert_watch("org",  "myorg")
        stats = collect_stats(db, client=None)
        targets = {w["target"] for w in stats.watchlist}
        assert "alice" in targets
        assert "myorg" in targets

    def test_no_api_call_when_client_none(self, db):
        stats = collect_stats(db, client=None)
        assert stats.rate_limit is None

    def test_rate_limit_fetched_from_client(self, db, mock_client):
        stats = collect_stats(db, client=mock_client)
        assert stats.rate_limit is not None
        assert "remaining" in stats.rate_limit


class TestFormatStatsText:
    def test_contains_key_sections(self, db):
        stats = collect_stats(db, client=None)
        text = format_stats_text(stats)
        assert "FORK QUEUE" in text
        assert "generated_at" not in text  # internal field not shown

    def test_shows_counts(self, db):
        fid = db.enqueue_fork("a/r1")
        db.mark_fork_success(fid, "me/r1")
        stats = collect_stats(db, client=None)
        text = format_stats_text(stats)
        assert "Success:" in text or "1" in text


class TestStatsToDict:
    def test_is_json_serialisable(self, db):
        stats = collect_stats(db, client=None)
        d = stats_to_dict(stats)
        # Should not raise
        s = json.dumps(d, default=str)
        assert len(s) > 0

    def test_queue_key_present(self, db):
        stats = collect_stats(db, client=None)
        d = stats_to_dict(stats)
        assert "queue" in d
        assert "total" in d["queue"]


class TestStatsToJson:
    def test_valid_json(self, db):
        stats = collect_stats(db, client=None)
        j = stats_to_json(stats)
        parsed = json.loads(j)
        assert "generated_at" in parsed

    def test_indented(self, db):
        stats = collect_stats(db, client=None)
        j = stats_to_json(stats, indent=4)
        assert "    " in j  # 4-space indent
