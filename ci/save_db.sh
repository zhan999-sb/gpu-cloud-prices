#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/history_common.sh"
repo=$(git rev-parse --show-toplevel)
cd "$repo"
db=${1:-data/gpu_prices.sqlite}
state=${2:-ci/runtime/history-base}
if [ ! -f "$state" ]; then
    echo 'No successful restore baseline; refusing to replace remote history.' >&2
    exit 1
fi
baseline=$(cat "$state")
case "$baseline" in
    missing) ;;
    *[!0-9a-f]*|'') echo 'Invalid history baseline.' >&2; exit 1 ;;
esac
mkdir -p ci/runtime
snapshot=ci/runtime/history-snapshot.sqlite
# SQLite backup includes committed WAL pages and never copies a live DB blindly.
# Keep this snapshot for the recovery artifact if pushing is rejected.
python "$script_dir/sqlite_snapshot.py" "$db" "$snapshot"
python - "$snapshot" <<'PY'
from pathlib import Path
import sys
if Path(sys.argv[1]).stat().st_size >= 95 * 1024 * 1024:
    sys.exit('History is approaching the Git blob limit (95 MiB guard); recovery snapshot retained. Plan a storage migration.')
PY
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' 0
export GIT_INDEX_FILE="$scratch/index"
export GIT_AUTHOR_NAME='github-actions[bot]'
export GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com'
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
db_blob=$(git hash-object -w -- "$snapshot")

attempt=1
while [ "$attempt" -le 3 ]; do
    fetch_history
    remote_blob=$(history_blob)
    # A binary DB cannot be rebased safely. Retry metadata-only branch changes,
    # but never drop observations written by a different database writer.
    if [ "$remote_blob" != "$baseline" ] && [ "$remote_blob" != "$db_blob" ]; then
        echo 'Remote history changed since restore; refusing to overwrite it. Recovery snapshot retained.' >&2
        exit 1
    fi
    if [ "$history_commit" = missing ]; then
        cat >"$scratch/README.md" <<'EOF'
# GPU price history

This orphan data branch contains only the persistent SQLite price-history
database and this README. GitHub Actions restores it before collection and
saves a consistent SQLite snapshot afterward. Do not force-push or delete it.
Application source and CI instructions live on main in docs/ci-setup.md.
EOF
    else
        git show "$history_commit:README.md" >"$scratch/README.md"
    fi
    readme_blob=$(git hash-object -w -- "$scratch/README.md")
    git read-tree --empty
    git update-index --add --cacheinfo "100644,$readme_blob,README.md"
    git update-index --add --cacheinfo "100644,$db_blob,gpu_prices.sqlite"
    tree=$(git write-tree)
    if [ "$history_commit" != missing ] && [ "$tree" = "$(git rev-parse "$history_commit^{tree}")" ]; then
        echo 'History snapshot is unchanged.'
        exit 0
    fi
    if [ "$history_commit" = missing ]; then
        commit=$(printf '%s\n' 'Initialize GPU price history' | git commit-tree "$tree")
    else
        commit=$(printf '%s\n' 'Update GPU price history' | git commit-tree "$tree" -p "$history_commit")
    fi
    # Plain fast-forward push: no force, no credential in command/remote URL.
    # checkout's persisted built-in GITHUB_TOKEN authenticates this on Actions.
    if git push --quiet origin "$commit:$history_ref"; then
        printf '%s\n' "$db_blob" >"$state"
        echo 'Saved price history to the data branch.'
        exit 0
    fi
    echo "History push attempt $attempt failed; re-reading the branch before retry." >&2
    attempt=$((attempt + 1))
    if [ "$attempt" -le 3 ]; then sleep "$attempt"; fi
done
echo 'History push failed after 3 attempts; recovery snapshot retained.' >&2
exit 1
