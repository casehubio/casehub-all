#!/usr/bin/env bash
# compute-mvn-flags.sh — compute MVN_COMMON and MAVEN_ARGS from workflow inputs.
# Called by gate, config, prepare, and convergence jobs.
#
# Usage: bash docs/compute-mvn-flags.sh <test_mode> <quarkus_build> <enforcer> [maven_args]
#   --env    also write to GITHUB_ENV (for jobs that build in later steps)
#
# Outputs (to GITHUB_OUTPUT):
#   mvn-common=<flags>
#   maven-args=<extra>

set -euo pipefail

WRITE_ENV=false
if [ "${1:-}" = "--env" ]; then
  WRITE_ENV=true
  shift
fi

TEST_MODE="${1:-skip}"
QUARKUS_BUILD="${2:-false}"
ENFORCER="${3:-false}"
MAVEN_ARGS="${4:-}"

FLAGS="--batch-mode -ntp -Dquarkus.http.test-port=0"

if [ "$TEST_MODE" = "skip" ] || [ "$TEST_MODE" = "compile" ]; then
  FLAGS="$FLAGS -DskipTests"
fi

# quarkus:build deadlocks under -T 1C (multi-thread Maven) — Surefire
# forks a separate JVM so @QuarkusTest still works without augmentation.
if [ "$QUARKUS_BUILD" != "true" ]; then
  FLAGS="$FLAGS -Dquarkus.build.skip=true"
fi

if [ "$ENFORCER" != "true" ]; then
  FLAGS="$FLAGS -Denforcer.skip=true"
fi

if [ "$TEST_MODE" = "compile" ]; then
  FLAGS="$FLAGS -Dnative.provision.skip=true"
fi

echo "mvn-common=$FLAGS" >> "$GITHUB_OUTPUT"
echo "maven-args=$MAVEN_ARGS" >> "$GITHUB_OUTPUT"

if [ "$WRITE_ENV" = "true" ]; then
  echo "MVN_COMMON=$FLAGS" >> "$GITHUB_ENV"
  echo "MAVEN_ARGS=$MAVEN_ARGS" >> "$GITHUB_ENV"
fi

echo "::notice::MVN_COMMON=$FLAGS MAVEN_ARGS=$MAVEN_ARGS"
