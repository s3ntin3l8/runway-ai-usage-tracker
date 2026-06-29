"""Tests for project-label derivation — the shared cwd-normalization rules."""

from app.services.project_label import derive_project, normalize_cwd, pick_canonical_cwd


class TestNormalizeCwd:
    def test_plain_path(self):
        assert normalize_cwd("/home/u/repo") == "/home/u/repo"

    def test_trailing_slash_stripped(self):
        assert normalize_cwd("/home/u/repo/") == "/home/u/repo"

    def test_worktree_truncated_at_claude(self):
        assert normalize_cwd("/home/u/repo/.claude/worktrees/agent-abc/apps/web") == "/home/u/repo"

    def test_tmp_truncated_at_claude(self):
        assert normalize_cwd("/home/u/repo/.claude/tmp/dedup") == "/home/u/repo"

    def test_first_claude_segment_wins(self):
        assert normalize_cwd("/a/.claude/x/.claude/y") == "/a"

    def test_none_and_empty(self):
        assert normalize_cwd(None) is None
        assert normalize_cwd("") is None

    def test_only_slashes_collapse_to_none(self):
        assert normalize_cwd("/") is None


class TestDeriveProject:
    def test_basename(self):
        assert derive_project("/home/u/ai-usage-tracker") == "ai-usage-tracker"

    def test_subfolder_uses_basename_of_self(self):
        # derive_project alone does NOT collapse subfolders — that's rule 2.
        assert derive_project("/home/u/repo/services/api") == "api"

    def test_worktree_collapses_to_root(self):
        assert (
            derive_project("/home/u/portfolio-tracker/.claude/worktrees/agent-x")
            == "portfolio-tracker"
        )

    def test_none(self):
        assert derive_project(None) is None
        assert derive_project("") is None


class TestPickCanonicalCwd:
    def test_shallowest_wins(self):
        cwds = [
            "/home/u/repo/services/api",
            "/home/u/repo",
            "/home/u/repo/apps/web",
        ]
        assert pick_canonical_cwd(cwds) == "/home/u/repo"

    def test_rule1_applied_before_selection(self):
        # The worktree path normalizes to the root, which is the shallowest.
        cwds = [
            "/home/u/repo/.claude/worktrees/agent-x",
            "/home/u/repo/packages/db",
        ]
        assert pick_canonical_cwd(cwds) == "/home/u/repo"

    def test_depth_tie_broken_by_shortest(self):
        # Equal separator count → shorter string wins.
        assert pick_canonical_cwd(["/aaa/bbb", "/a/b"]) == "/a/b"

    def test_nulls_skipped(self):
        assert pick_canonical_cwd([None, "/home/u/repo", None]) == "/home/u/repo"

    def test_all_null_returns_none(self):
        assert pick_canonical_cwd([None, None]) is None
        assert pick_canonical_cwd([]) is None
