#!/bin/bash
# Run a Maven command with dashboard updates and self-healing.
#
# Usage:
#   run-with-progress.sh <phase-name> <maven-command...>             Sequential
#   run-with-progress.sh --parallel <phase-name> <maven-command...>  Parallel-safe
#
# ══════════════════════════════════════════════════════════════════════
# SELF-HEALING REGISTRY
# ══════════════════════════════════════════════════════════════════════
#
# When a build fails, the script scans the Maven output for known
# failure signatures. If matched, it repairs the cause and retries
# once. Each entry documents: what breaks, how we detect it, what
# we fix, and why it works.
#
# To add a new self-healing rule:
#   1. Add a grep pattern to the diagnose block in run_with_healing()
#   2. Add the targeted repair (delete only what's broken)
#   3. Document it here with the same format
#
# ── Corrupt JARs ──────────────────────────────────────────────────
#   Cause:   Artifact transfer between GitHub Actions jobs truncates
#            the JAR (upload-artifact/download-artifact race, or
#            network interruption during Maven download).
#   Detect:  Maven output contains:
#              "Unexpected end of ZLIB input stream"
#              "invalid LOC header"
#              "zip END header not found"
#              "error reading /path/to/file.jar"
#   Repair:  Delete the specific corrupt JAR file from ~/.m2/repository.
#   Retry:   Maven re-downloads the clean copy from the warm cache
#            or remote repository.
#
# ── CDN Resolution Failures ───────────────────────────────────────
#   Cause:   Maven Central CDN returns 404/503 for artifacts that
#            exist. Maven caches the failure in .lastUpdated marker
#            files and won't retry until the update interval elapses.
#   Detect:  Maven output contains:
#              "Could not find artifact"
#              "could not be resolved"
#              "Plugin not found in any plugin repository"
#   Repair:  Delete all .lastUpdated files from ~/.m2/repository.
#            This clears the "don't retry" markers.
#   Retry:   Maven re-checks all repositories. With -U in MVN_COMMON,
#            SNAPSHOT metadata is also refreshed.
#
# ── Corrupt Jandex Indexes ────────────────────────────────────────
#   Cause:   Parallel Maven builds (-T 1C) can write partial Jandex
#            index files. Quarkus reads the truncated index and fails.
#   Detect:  Maven output contains:
#              "Corrupt Jandex"
#              "IllegalArgumentException" near "jandex"
#   Repair:  Delete all jandex.idx files from ~/.m2/repository/io/casehub.
#   Retry:   Maven regenerates the index during the next build.
#
# ══════════════════════════════════════════════════════════════════════

set -euo pipefail

MODE="phase"
if [ "${1:-}" = "--parallel" ]; then
  MODE="phase-file"
  shift
fi

PHASE="$1"
shift
LOGFILE="/tmp/phase-$(echo "$PHASE" | tr ' :+/()' '------').log"

# Ensure final update fires even if the script crashes
cleanup() {
  local code=$?
  if [ "$code" -ne 0 ] && [ -z "${FINAL_SENT:-}" ]; then
    bash docs/update-progress.sh "$MODE" "$PHASE" failure "$LOGFILE" 2>&1 || echo "::warning::Dashboard update failed for $PHASE"
  fi
}
trap cleanup EXIT

# Signal start (won't overwrite existing started timestamp)
bash docs/update-progress.sh "$MODE" "$PHASE" in_progress 2>&1 || echo "::warning::Dashboard update failed for $PHASE"

# Run command with self-healing — diagnose, repair, retry known transient failures
run_with_healing() {
  "$@" 2>&1 | tee "$LOGFILE"
  EXIT_CODE=${PIPESTATUS[0]}
  [ "$EXIT_CODE" -eq 0 ] && return 0

  # Diagnose failure class
  local HEALED=0

  # Corrupt JARs (truncated artifact transfer)
  if grep -qE "Unexpected end of ZLIB|error reading.*\.jar|invalid LOC header|zip END header not found" "$LOGFILE" 2>/dev/null; then
    echo "::warning::[$PHASE] Corrupt JAR detected — purging damaged artifacts"
    grep -oE '/[^ ]*\.jar' "$LOGFILE" | sort -u | while read -r jar; do
      if [ -f "$jar" ]; then
        echo "  Deleting corrupt: $jar"
        rm -f "$jar"
      fi
    done
    HEALED=1
  fi

  # Cached resolution failures (CDN flakiness)
  if grep -qE "Could not find artifact|could not be resolved|Plugin not found" "$LOGFILE" 2>/dev/null; then
    echo "::warning::[$PHASE] Resolution failure — clearing .lastUpdated markers"
    find ~/.m2/repository -name "*.lastUpdated" -delete 2>/dev/null || true
    HEALED=1
  fi

  # Corrupt Jandex indexes
  if grep -qE "Corrupt Jandex\|IllegalArgumentException.*jandex" "$LOGFILE" 2>/dev/null; then
    echo "::warning::[$PHASE] Corrupt Jandex index — purging"
    find ~/.m2/repository/io/casehub -name "jandex.idx" -delete 2>/dev/null || true
    HEALED=1
  fi

  if [ "$HEALED" -eq 1 ]; then
    echo "::notice::[$PHASE] Self-healed — retrying build"
    "$@" 2>&1 | tee "$LOGFILE"
    EXIT_CODE=${PIPESTATUS[0]}
  fi

  return $EXIT_CODE
}

set +e
run_with_healing "$@"
set -e

# Count lines of code per file type (repo dir = phase name)
# Must not fail the build — wrapped in set +e
set +e
if [ -d "$PHASE" ]; then
  _loc_count() { find "$@" 2>/dev/null | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1+0}'; }
  JAVA_SRC=$(_loc_count "$PHASE" -name "*.java" -not -path "*/test/*" -not -path "*/target/*")
  JAVA_TEST=$(_loc_count "$PHASE" -name "*.java" -path "*/test/*" -not -path "*/target/*")
  YAML_LOC=$(_loc_count "$PHASE" \( -name "*.yaml" -o -name "*.yml" \) -not -path "*/target/*")
  JSON_LOC=$(_loc_count "$PHASE" -name "*.json" -not -path "*/target/*" -not -path "*/node_modules/*" -not -path "*/.yarn/*")
  TS_LOC=$(_loc_count "$PHASE" \( -name "*.ts" -o -name "*.tsx" \) -not -path "*/target/*" -not -path "*/node_modules/*" -not -path "*/.yarn/*" -not -path "*/dist/*")
  XML_LOC=$(_loc_count "$PHASE" -name "pom.xml")
  echo "[LOC] java_src=${JAVA_SRC:-0} java_test=${JAVA_TEST:-0} yaml=${YAML_LOC:-0} json=${JSON_LOC:-0} ts=${TS_LOC:-0} xml=${XML_LOC:-0}" >> "$LOGFILE"
fi
set -e

# Final update with stats
FINAL_SENT=1
if [ "$EXIT_CODE" -eq 0 ]; then
  bash docs/update-progress.sh "$MODE" "$PHASE" success "$LOGFILE" 2>&1 || echo "::warning::Dashboard update failed for $PHASE"
else
  bash docs/update-progress.sh "$MODE" "$PHASE" failure "$LOGFILE" 2>&1 || echo "::warning::Dashboard update failed for $PHASE"
fi

exit $EXIT_CODE
