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
# Philosophy: only compilation errors and test failures are real.
# Everything else is infrastructure — diagnose it, fix it, retry.
#
# The build FIRST checks for code failures (compilation, tests).
# If found → fail immediately, human must fix.
# If NOT found → it's infrastructure. Diagnose the class, repair
# surgically (delete only what's broken), retry once.
#
# To add a new self-healing rule:
#   1. Add a grep pattern to the infrastructure section in run_with_healing()
#   2. Add the targeted repair (delete only what's broken)
#   3. Document it here with the same format
#   4. If it's a code failure that should NOT retry, add it to the
#      code-failure detection block instead
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
# ── Network / Runner Failures ─────────────────────────────────────
#   Cause:   GitHub Actions runner loses network, DNS fails, or the
#            runner is preempted mid-build.
#   Detect:  Maven output contains:
#              "Connection reset" / "Connection refused"
#              "UnknownHostException" / "SocketTimeoutException"
#              "NoRouteToHostException" / "EOFException"
#   Repair:  Clear .lastUpdated markers (same as CDN failures).
#   Retry:   Network is usually back within seconds on a new attempt.
#
# ── Out of Memory ─────────────────────────────────────────────────
#   Cause:   Large reactor builds exhaust JVM heap or system memory.
#   Detect:  Maven output contains:
#              "OutOfMemoryError" / "GC overhead limit"
#              "Java heap space" / "Cannot allocate memory"
#   Repair:  Delete target/ dirs to free disk (which relieves swap).
#   Retry:   Second attempt has more free memory from cleanup.
#
# ── Disk Space ────────────────────────────────────────────────────
#   Cause:   Artifact cache + build output fills the runner disk.
#   Detect:  Maven output contains:
#              "No space left on device" / "Disk quota exceeded"
#   Repair:  Delete target/ dirs and temp provisioning files.
#   Retry:   Freed space allows the build to continue.
#
# ── Lock Contention ───────────────────────────────────────────────
#   Cause:   Parallel builds or stale processes leave lock files.
#   Detect:  Maven output contains:
#              "OverlappingFileLockException"
#              "Could not acquire lock"
#              "locked by another process"
#   Repair:  Delete .lock files from ~/.m2/repository.
#   Retry:   Clean locks allow normal file access.
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
PHASE_START=$(date +%s)
bash docs/update-progress.sh "$MODE" "$PHASE" in_progress 2>&1 || echo "::warning::Dashboard update failed for $PHASE"

# Run command with self-healing.
#
# Logic: if the build fails, classify the failure.
#   CODE FAILURE (compilation/test) → fail immediately, human must fix
#   INFRASTRUCTURE FAILURE (everything else) → diagnose, repair, retry
#
run_with_healing() {
  "$@" 2>&1 | tee "$LOGFILE"
  EXIT_CODE=${PIPESTATUS[0]}
  [ "$EXIT_CODE" -eq 0 ] && return 0

  # ── Code failures — these are real, don't retry ──────────────────
  if grep -qE "COMPILATION ERROR" "$LOGFILE" 2>/dev/null &&
     grep -qE "cannot find symbol|does not exist|is not abstract|cannot access|incompatible types" "$LOGFILE" 2>/dev/null; then
    echo "::error::[$PHASE] Compilation failure — code fix required"
    return $EXIT_CODE
  fi
  if grep -qE "Tests run:.*Failures: [1-9]|Tests run:.*Errors: [1-9]" "$LOGFILE" 2>/dev/null; then
    echo "::error::[$PHASE] Test failure — code fix required"
    return $EXIT_CODE
  fi

  # ── Infrastructure failures — diagnose, repair, retry ────────────
  echo "::warning::[$PHASE] Infrastructure failure detected — diagnosing"
  HEAL_CLASSES=""

  # Corrupt JARs (truncated artifact transfer)
  if grep -qE "Unexpected end of ZLIB|error reading.*\.jar|invalid LOC header|zip END header not found" "$LOGFILE" 2>/dev/null; then
    HEAL_CLASSES="${HEAL_CLASSES}corrupt_jar,"
    echo "  → Corrupt JAR — purging damaged artifacts"
    grep -oE '/[^ ]*\.jar' "$LOGFILE" | sort -u | while read -r jar; do
      [ -f "$jar" ] && echo "    rm $jar" && rm -f "$jar"
    done
  fi

  # Cached resolution failures (CDN flakiness)
  if grep -qE "Could not find artifact|could not be resolved|Plugin not found|Non-resolvable" "$LOGFILE" 2>/dev/null; then
    HEAL_CLASSES="${HEAL_CLASSES}cdn_resolution,"
    echo "  → Resolution failure — clearing .lastUpdated markers"
    find ~/.m2/repository -name "*.lastUpdated" -delete 2>/dev/null || true
  fi

  # Corrupt Jandex indexes
  if grep -qE "Corrupt Jandex|IllegalArgumentException.*jandex" "$LOGFILE" 2>/dev/null; then
    HEAL_CLASSES="${HEAL_CLASSES}corrupt_jandex,"
    echo "  → Corrupt Jandex index — purging"
    find ~/.m2/repository/io/casehub -name "jandex.idx" -delete 2>/dev/null || true
  fi

  # Network / runner failures
  if grep -qE "Connection reset|Connection refused|UnknownHostException|SocketTimeoutException|NoRouteToHostException|EOFException.*HTTP" "$LOGFILE" 2>/dev/null; then
    HEAL_CLASSES="${HEAL_CLASSES}network,"
    echo "  → Network failure — clearing cached connection errors"
    find ~/.m2/repository -name "*.lastUpdated" -delete 2>/dev/null || true
  fi

  # Out of memory
  if grep -qE "OutOfMemoryError|GC overhead limit|Java heap space|Cannot allocate memory" "$LOGFILE" 2>/dev/null; then
    HEAL_CLASSES="${HEAL_CLASSES}oom,"
    echo "  → OOM — clearing target dirs to free disk, retrying with more memory"
    find . -maxdepth 3 -name target -type d -exec rm -rf {} + 2>/dev/null || true
  fi

  # Disk space
  if grep -qE "No space left on device|Disk quota exceeded" "$LOGFILE" 2>/dev/null; then
    HEAL_CLASSES="${HEAL_CLASSES}disk_full,"
    echo "  → Disk full — purging target dirs and temp files"
    find . -maxdepth 3 -name target -type d -exec rm -rf {} + 2>/dev/null || true
    rm -rf /tmp/.provisioning-* 2>/dev/null || true
  fi

  # Stale lock files
  if grep -qE "OverlappingFileLockException|Could not acquire lock|locked by another process" "$LOGFILE" 2>/dev/null; then
    HEAL_CLASSES="${HEAL_CLASSES}lock_contention,"
    echo "  → Lock contention — clearing lock files"
    find ~/.m2/repository -name "*.lock" -delete 2>/dev/null || true
  fi

  HEAL_CLASSES="${HEAL_CLASSES%,}"
  echo "[HEAL] phase=$PHASE classes=$HEAL_CLASSES" >> "$LOGFILE"
  echo "::notice::[$PHASE] Repair complete (healed: $HEAL_CLASSES) — retrying build"
  "$@" 2>&1 | tee "$LOGFILE"
  EXIT_CODE=${PIPESTATUS[0]}
  return $EXIT_CODE
}

# Dry run — skip Maven entirely, just validate pipeline structure
if [ "${DRY_RUN:-}" = "true" ]; then
  echo "dry run: $PHASE — skipped" | tee "$LOGFILE"
  FINAL_SENT=1
  bash docs/update-progress.sh "$MODE" "$PHASE" success "$LOGFILE" 2>&1 || true
  exit 0
fi

# Pre-flight cache validation — catch corrupt JARs before Maven hits them
if [ -f docs/validate-cache.sh ]; then
  bash docs/validate-cache.sh --heal 2>&1 | tail -3
fi

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
PHASE_ELAPSED=$(( $(date +%s) - PHASE_START ))
echo "[TIMING] phase=$PHASE elapsed=${PHASE_ELAPSED}s" >> "$LOGFILE"
echo "::notice::[$PHASE] completed in ${PHASE_ELAPSED}s (exit=$EXIT_CODE)"
FINAL_SENT=1
if [ "$EXIT_CODE" -eq 0 ]; then
  bash docs/update-progress.sh "$MODE" "$PHASE" success "$LOGFILE" 2>&1 || echo "::warning::Dashboard update failed for $PHASE"
else
  bash docs/update-progress.sh "$MODE" "$PHASE" failure "$LOGFILE" 2>&1 || echo "::warning::Dashboard update failed for $PHASE"
fi

exit $EXIT_CODE
