#!/bin/sh
# The git hook for this repository.
#
# The interpreter is resolved at run time, never baked in when the hook was
# installed. The hooks live in the repository's shared .git/hooks, so every
# worktree writes to the same two files; a hook that pointed at one
# worktree's .venv stopped working for every worktree the moment that
# worktree was removed.
#
# The hook type comes from this file's own name, so one script serves both
# pre-commit and pre-push.

type=$(basename "$0")

root=$(git rev-parse --show-toplevel) || exit 1

# The main checkout, so a worktree without its own .venv can still find a
# pre-commit. git returns this relative in the main checkout and absolute in
# a worktree; --path-format makes it absolute either way.
common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
if [ -z "$common" ]; then
    common=$(git rev-parse --git-common-dir)
    case "$common" in
        */*) ;;
        *) common="$root/$common" ;;
    esac
fi
main=$(dirname "$common")

# A candidate is only accepted if it can actually import pre_commit -- a
# .venv that exists but was never installed into is not a usable interpreter.
have_pre_commit() {
    "$1" -c "import pre_commit" >/dev/null 2>&1
}

python=""
for candidate in \
    "$root/.venv/Scripts/python.exe" \
    "$root/.venv/bin/python" \
    "$main/.venv/Scripts/python.exe" \
    "$main/.venv/bin/python"
do
    if [ -x "$candidate" ] && have_pre_commit "$candidate"; then
        python="$candidate"
        break
    fi
done

if [ -n "$python" ]; then
    exec "$python" -mpre_commit hook-impl \
        --config="$root/.pre-commit-config.yaml" \
        --hook-type="$type" \
        --hook-dir "$(dirname "$0")" \
        -- "$@"
fi

if command -v pre-commit >/dev/null 2>&1; then
    exec pre-commit hook-impl \
        --config="$root/.pre-commit-config.yaml" \
        --hook-type="$type" \
        --hook-dir "$(dirname "$0")" \
        -- "$@"
fi

echo "The git hook cannot find an interpreter with pre-commit." >&2
echo "  looked for: $root/.venv, $main/.venv, then python3/python on PATH" >&2
echo "  install one with: python scripts/dev_checks.py install" >&2
exit 1
