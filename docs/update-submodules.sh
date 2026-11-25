#!/usr/bin/env bash
# update-submodules.sh — update submodules based on workflow input mode.
# Called by prepare and finalize jobs.
#
# Usage: bash docs/update-submodules.sh <mode> [comma-separated-list]
#   mode: pinned | all | list

set -euo pipefail

MODE="${1:-pinned}"
LIST="${2:-}"

case "$MODE" in
  all)
    echo "Updating ALL submodules to latest main"
    git submodule update --remote parent
    git submodule update --remote
    ;;
  list)
    echo "Updating parent + listed submodules: $LIST"
    git submodule update --remote parent
    IFS=',' read -ra REPOS <<< "$LIST"
    for repo in "${REPOS[@]}"; do
      repo=$(echo "$repo" | xargs)
      if [ -d "$repo" ]; then
        echo "  Updating $repo"
        git submodule update --remote "$repo"
      else
        echo "  WARNING: $repo not found, skipping"
      fi
    done
    ;;
  pinned)
    echo "Using pinned submodule SHAs (committed pointers)"
    ;;
esac
