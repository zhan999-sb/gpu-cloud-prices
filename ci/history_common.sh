#!/usr/bin/env bash
# Bash is required for pipefail; the commands otherwise use POSIX shell syntax.
set -euo pipefail

history_ref=refs/heads/data

fetch_history() {
    # Exit 2 means no matching ref; authentication/network failures are errors.
    if git ls-remote --exit-code --heads origin "$history_ref" >"$scratch/remote-head"; then
        git fetch --quiet --no-tags --depth=1 origin "$history_ref"
        history_commit=$(git rev-parse FETCH_HEAD)
        expected=$(printf '100644 blob README.md\n100644 blob gpu_prices.sqlite')
        actual=$(git ls-tree -r "$history_commit" | awk '{print $1 " " $2 " " $4}')
        if [ "$actual" != "$expected" ]; then
            echo 'The data branch must contain only regular README.md and gpu_prices.sqlite files.' >&2
            return 1
        fi
    else
        code=$?
        if [ "$code" -ne 2 ]; then
            echo 'Cannot read the data branch; refusing to treat a remote error as empty history.' >&2
            return "$code"
        fi
        history_commit=missing
    fi
}

history_blob() {
    if [ "$history_commit" = missing ]; then
        printf '%s\n' missing
    else
        git rev-parse "$history_commit:gpu_prices.sqlite"
    fi
}
