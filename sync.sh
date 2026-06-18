#!/bin/bash
# Sync all submodules to the latest main from each origin repo.
# Run at the start of each Claude.ai session.
set -e
git pull
git submodule update --remote --merge
echo "All submodules up to date."
