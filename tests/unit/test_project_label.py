"""Tests for project-label derivation — the shared cwd-normalization rules."""

import pytest

from app.services.project_label import (
    MAX_WORKTREE_PATTERNS,
    WORKTREE_CONTAINERS,
    clean_worktree_patterns,
    derive_project,
    normalize_cwd,
    pick_canonical_cwd,
    validate_worktree_pattern,
)


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


class TestWindowsPaths:
    """A Windows client reports backslash paths and the server runs on Linux,
    so the derivation must not depend on the *server's* separator."""

    def test_backslash_path_labels_with_its_basename(self):
        assert derive_project(r"D:\Projects\runway-ai-usage-tracker") == "runway-ai-usage-tracker"

    def test_forward_slash_windows_path_labels_the_same(self):
        assert derive_project("D:/Projects/runway-ai-usage-tracker") == "runway-ai-usage-tracker"

    def test_one_drive_path(self):
        assert (
            derive_project(r"C:\Users\bjoer\OneDrive\Projects\ai-usage-tracker")
            == "ai-usage-tracker"
        )

    def test_worktree_collapses_to_the_root(self):
        assert derive_project(r"D:\repo\.claude\worktrees\agent-x") == "repo"

    def test_normalize_cwd_truncates_a_windows_worktree(self):
        assert normalize_cwd(r"D:\repo\.claude\worktrees\x") == r"D:\repo"

    def test_trailing_backslash_stripped(self):
        assert normalize_cwd("D:\\repo\\") == "D:\\repo"

    def test_drive_root_has_no_label(self):
        assert derive_project("C:\\") is None
        assert derive_project("C:/") is None

    def test_unc_path(self):
        assert derive_project(r"\\server\share\repo") == "repo"

    def test_mixed_separators(self):
        assert derive_project(r"D:\repo/apps\web") == "web"

    def test_label_does_not_depend_on_the_host_os(self):
        # The regression pin: `os.path.basename` on a POSIX host returns the
        # whole Windows path, so this assertion fails the moment it comes back.
        assert derive_project(r"D:\repo") == "repo"

    def test_path_ending_at_a_claude_segment_is_truncated(self):
        # The one intentional change for POSIX paths: the `.claude` segment
        # marks the root boundary even when it is the last one.
        assert normalize_cwd("/a/.claude") == "/a"

    def test_a_relative_path_starting_with_claude_keeps_its_label(self):
        # There is no root to cut back to, so the path is left alone.
        assert normalize_cwd(".claude/worktrees/x") == ".claude/worktrees/x"
        assert derive_project(".claude/worktrees/x") == "x"


class TestPickCanonicalCwdOnWindows:
    def test_shallowest_windows_path_wins(self):
        assert pick_canonical_cwd([r"D:\repo\apps\web", r"D:\repo"]) == r"D:\repo"

    def test_a_windows_path_is_not_counted_as_shallow(self):
        # A Windows path counts zero separators when the host is POSIX, so the
        # depth comparison used to be decided by string length alone.
        assert pick_canonical_cwd([r"D:\repo\apps\web"]) == r"D:\repo\apps\web"

    def test_the_drive_spec_is_not_a_directory_level(self):
        assert pick_canonical_cwd([r"C:\repo", r"D:\a\b\c"]) == r"C:\repo"

    def test_ties_are_broken_deterministically(self):
        assert pick_canonical_cwd([r"D:\b\repo", r"D:\a\repo"]) == r"D:\a\repo"


class TestWorktreeContainers:
    @pytest.mark.parametrize("name", WORKTREE_CONTAINERS)
    def test_every_default_container_cuts_to_the_repository(self, name):
        assert normalize_cwd(f"/home/u/repo/{name}/213-cross-host/app") == "/home/u/repo"
        assert derive_project(f"/home/u/repo/{name}/213-cross-host/app") == "repo"

    @pytest.mark.parametrize("name", WORKTREE_CONTAINERS)
    def test_windows_separators_and_drive(self, name):
        assert derive_project(rf"C:\Users\u\repo\{name}\slug\app") == "repo"
        assert normalize_cwd(rf"C:\Users\u\repo\{name}\slug") == r"C:\Users\u\repo"

    @pytest.mark.parametrize("name", WORKTREE_CONTAINERS)
    def test_case_insensitive(self, name):
        assert derive_project(f"/home/u/repo/{name.upper()}/slug") == "repo"

    @pytest.mark.parametrize("name", WORKTREE_CONTAINERS)
    def test_trailing_separator_and_container_as_last_segment(self, name):
        assert normalize_cwd(f"/home/u/repo/{name}/") == "/home/u/repo"
        assert normalize_cwd(f"/home/u/repo/{name}") == "/home/u/repo"

    def test_a_relative_path_starting_with_a_container_is_unchanged(self):
        assert normalize_cwd(".wt/x") == ".wt/x"
        assert derive_project(".wt/x") == "x"

    def test_a_bare_worktrees_folder_is_not_a_container(self):
        assert normalize_cwd("/home/u/repo/worktrees/x") == "/home/u/repo/worktrees/x"
        assert derive_project("/home/u/repo/worktrees/x") == "x"

    def test_opencode_is_not_a_container(self):
        assert derive_project("/home/u/repo/.opencode/x") == "x"

    def test_a_longer_segment_name_is_not_cut(self):
        assert derive_project("/home/u/repo/.wtx/y") == "y"
        assert derive_project("/home/u/repo/my.wt/y") == "y"

    def test_the_first_container_wins_when_nested(self):
        assert derive_project("/home/u/repo/.wt/feat/.claude/worktrees/slug") == "repo"
        assert derive_project("/home/u/repo/.claude/worktrees/a/.wt/b") == "repo"

    def test_canonical_cwd_collapses_worktrees(self):
        assert (
            pick_canonical_cwd(["/home/u/repo/.wt/a/src", "/home/u/repo/.wt/b"]) == "/home/u/repo"
        )


class TestWorktreePatterns:
    """Operator-added patterns (`worktree_patterns`) on top of the built-ins."""

    def test_sibling_pattern_strips_the_suffix(self):
        path = "/home/u/repo-wt/213-cross-host/app"
        assert derive_project(path) == "app"
        assert derive_project("/home/u/repo-wt/213-cross-host") == "213-cross-host"
        assert normalize_cwd(path, ["*-wt"]) == "/home/u/repo"
        assert derive_project(path, ["*-wt"]) == "repo"

    def test_sibling_pattern_with_windows_separators_and_case(self):
        assert derive_project(r"C:\Users\u\Repo-WT\slug\app", ["*-wt"]) == "Repo"

    def test_sibling_segment_that_is_only_the_suffix_is_cut_like_a_container(self):
        assert normalize_cwd("/home/u/repo/-wt/slug", ["*-wt"]) == "/home/u/repo"

    def test_container_pattern_cuts_before_the_segment(self):
        assert normalize_cwd("/home/u/repo/worktrees-extra/s/app", ["worktrees-extra"]) == (
            "/home/u/repo"
        )
        assert normalize_cwd("/home/u/repo/wt-one/s", ["wt-*"]) == "/home/u/repo"

    def test_pattern_must_match_the_whole_segment(self):
        assert derive_project("/home/u/my-wt-notes/app", ["*-wt"]) == "app"
        assert derive_project("/home/u/repo/worktrees-extra2/s", ["worktrees-extra"]) == "s"

    def test_defaults_still_apply_and_the_first_match_wins(self):
        assert derive_project("/home/u/repo/.wt/slug", ["*-wt"]) == "repo"
        assert derive_project("/home/u/a-wt/b/.wt/c", ["*-wt"]) == "a"
        assert derive_project("/home/u/a/.wt/b-wt/c", ["*-wt"]) == "a"

    def test_a_relative_leading_segment_is_left_alone(self):
        assert normalize_cwd("repo-wt/slug", ["*-wt"]) == "repo-wt/slug"

    def test_no_patterns_is_unchanged(self):
        assert normalize_cwd("/home/u/repo-wt/slug", ()) == "/home/u/repo-wt/slug"

    def test_pick_canonical_cwd_uses_the_patterns(self):
        cwds = ["/home/u/repo-wt/s1/app", "/home/u/repo"]
        assert pick_canonical_cwd(cwds, ["*-wt"]) == "/home/u/repo"

    def test_a_corrupt_stored_pattern_is_ignored(self):
        assert derive_project("/home/u/repo/x/y", ["a/b", "", "*"]) == "y"


class TestWorktreePatternValidation:
    @pytest.mark.parametrize(
        "bad", ["", "  ", "a/b", "a\\b", "*", "**", "..", ".", "a?b", "[a]", "x" * 65, "a\x00b"]
    )
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            validate_worktree_pattern(bad)

    @pytest.mark.parametrize("good", ["*-wt", ".wt", "worktrees-extra", "wt-*", " *-wt "])
    def test_accepts(self, good):
        assert validate_worktree_pattern(good) == good.strip()

    def test_list_dedupes_case_insensitively_and_bounds_the_count(self):
        assert clean_worktree_patterns(["*-WT", "*-wt", "a"]) == ["*-WT", "a"]
        with pytest.raises(ValueError):
            clean_worktree_patterns([f"p{i}" for i in range(MAX_WORKTREE_PATTERNS + 1)])
