#!/usr/bin/env bash
# Run from any directory within the checkout; Git Bash is supported on Windows.
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$script_dir/history_common.sh"
repo=$(git rev-parse --show-toplevel)
cd "$repo"
db=${1:-data/gpu_prices.sqlite}
state=${2:-ci/runtime/history-base}
mkdir -p "$(dirname -- "$db")" "$(dirname -- "$state")"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' 0
# A failed restore must not leave a usable baseline from a previous invocation.
rm -f "$state"

fetch_history
if [ "$history_commit" = missing ]; then
    # A human can seed an existing local history before the very first run.
    # On a fresh Actions checkout the collector will create the new database.
    if [ -e "$db" ]; then
        python "$script_dir/sqlite_snapshot.py" "$db" "$scratch/seed.sqlite"
        echo 'No data branch yet; keeping the existing local database as the initial history.'
    else
        echo 'No data branch yet; the first collection will start the history.'
    fi
else
    if [ -e "$db" ] || [ -e "$db-wal" ] || [ -e "$db-shm" ]; then
        echo 'Restore destination already exists; use a fresh checkout or a different DB path.' >&2
        exit 1
    fi
    git show "$history_commit:gpu_prices.sqlite" >"$scratch/restored.sqlite"
    python "$script_dir/sqlite_snapshot.py" "$scratch/restored.sqlite" "$db"
    echo 'Restored price history from the data branch.'
fi
history_blob >"$state"
