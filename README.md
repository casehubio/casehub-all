# CaseHub CI Architecture

## Overview

CaseHub is a multi-repo ecosystem (~30 repos) under the `casehubio` GitHub org.
The repos have a dependency graph that determines build order. CI operates at
two levels:

1. **Per-repo CI** — each repo has its own `publish.yml` workflow that builds,
   tests, and publishes SNAPSHOT artifacts to GitHub Packages on push to main.
2. **Cross-repo CI** (`casehub-all`) — verifies the entire ecosystem compiles
   together. Uses git submodules to pull all repos into one workspace and builds
   them as a single Maven reactor via dependency-grouped aggregator POMs.

### Why casehub-all exists

Per-repo CI only proves that each repo compiles and passes tests against the
SNAPSHOT artifacts that happened to be in GitHub Packages at that moment. It
cannot detect cross-repo breakage:

- **API drift:** Repo A changes a method signature and publishes. Repo B's CI
  runs before B's code is updated — it pulls A's new SNAPSHOT and fails. But
  if B's CI ran a minute earlier, it would have pulled A's old SNAPSHOT and
  passed. Per-repo CI gives inconsistent results depending on timing.
- **Diamond dependencies:** Repo C depends on both A and B. A and B both
  depend on platform. If platform publishes a breaking change and A updates
  but B hasn't yet, C's CI might pull updated-A and stale-B — a combination
  that was never tested together.
- **No reproducibility:** Per-repo CI builds against whatever SNAPSHOTs
  are currently published. There's no record of which exact combination of
  repo versions was verified. A build that passed yesterday might fail today
  with identical code because a transitive dependency published a new SNAPSHOT.

casehub-all solves these problems:

- **Source-level, staged pipeline:** All repos build from source in a staged
  pipeline. Each phase runs `mvn install`, populating `.m2` for subsequent
  phases. This uses the same resolution mechanism as SNAPSHOTs, but is
  deterministic within a single run because no external publishes can
  intervene. API drift is caught immediately.
- **Reproducible SHA manifest:** Every build records the exact SHA of every
  repo. Given these SHAs, the build can be reproduced deterministically.
  `git submodule status` is the bill of materials.
- **Pointer advance = verified state:** The committed submodule pointers
  represent the last combination that compiled together. Anyone cloning
  casehub-all gets a known-good set of versions.
- **Single `.m2` cache:** Building in one workspace means each phase's
  `mvn install` populates `.m2` for subsequent phases. No external artifact
  resolution, no SNAPSHOT timing races, no stale-cache surprises (within a
  single run).

## SHA Management and Submodule Pointers

casehub-all tracks every repo as a git submodule. Each submodule pointer is a
SHA — the exact commit that was last verified to compile with the rest of the
ecosystem.

### The Build Cycle

```
1. Checkout casehub-all (with submodule pointers from last green build)
2. git submodule update --remote parent    ← pull parent to latest main first
3. reconcile-submodules.py                 ← add/remove submodules based on CSVs
4. git submodule update --remote           ← pull ALL submodules to latest main
5. Record SHA manifest                     ← log exact SHAs for reproducibility
6. Build all phases (platform → G1-G6)
7. If green: git add -u && git commit && git push  ← advance pointers
```

After step 4, every submodule points to the latest `main` commit of its repo.
If the build succeeds, step 7 commits those SHAs back to casehub-all. This
means the committed submodule pointers always represent the **last known-good
combination** of all repos.

**Pointer advance is all-or-nothing.** A failure in any phase prevents
advancing any pointers, even for phases that succeeded earlier. The `git push`
only runs `if: success()` — meaning every phase must pass. The committed SHAs
on main always represent a fully-verified combination, never a partial build.

### SHA Manifest

Step 5 logs every submodule SHA in the CI output under "SHA manifest". This is
the reproducibility record — given these exact SHAs, the build is guaranteed
to produce the same result. Useful for bisecting cross-repo regressions.

### Resuming from a Group

The `resume_from` input skips earlier phases. For example, `resume_from=g3`
skips platform, G1, and G2, jumping straight to G3 (middleware). This works
because:

1. Earlier phases installed their artifacts into `.m2/repository` during a
   prior run.
2. GitHub Actions caches `.m2/repository` between runs (keyed on
   `parent/pom.xml` hash).
3. G3+ phases resolve their dependencies from `.m2` — they don't care whether
   the artifacts were built in this run or restored from cache.

**When resumption is safe:** Immediately after a failure in a later group, when
the earlier groups haven't changed. You fix the failing repo, push, and
re-trigger with `resume_from=g3` to skip the 5+ minutes of rebuilding platform
and core libs.

**When resumption is unsafe:** If any earlier repo has published a new SNAPSHOT
since the cached run. The cache holds stale artifacts, so downstream modules
compile against the old API. This can cause false positives (build passes with
old API) or false negatives (build fails due to cache/live mismatch).

**Warning — resumed builds weaken the pointer guarantee:** The submodule
update in step 4 pulls ALL repos to latest main regardless of `resume_from`.
If the build passes, the pointer advance commits SHAs for *every* repo —
including repos whose build phases were skipped. The SHAs are accurate (latest
main), but the verification is partial: skipped phases were not rebuilt, so
API compatibility between skipped and built phases is assumed, not proven.

## Dependency Graph

Every cross-repo dependency is declared in `parent/build/modules-core.csv` and
`modules-applications.csv`. These determine build order.

### Core Modules

| Repo | Depends on |
|------|-----------|
| parent | — |
| platform | parent |
| pages | — |
| worker | parent, platform |
| ledger | parent, platform |
| connectors | parent, platform |
| neocortex | platform |
| eidos | platform, ledger |
| qhorus | platform, ledger, connectors, eidos |
| drafthouse | parent, qhorus |
| work | platform, ledger, engine, qhorus |
| engine | platform, ledger, connectors, eidos, neocortex, qhorus, work, worker |
| ras | platform, engine |
| agency | platform, ledger, neocortex, eidos, engine, qhorus, work, worker |
| casehub-langchain4j | platform, ledger, neocortex, engine |
| casehub-mosaic | pages, engine, work, qhorus, ledger, agency |
| workers | platform, engine, worker |
| claudony | platform, ledger, engine, qhorus, work, worker |
| desiredstate | platform, eidos, engine, neocortex, ras, work, worker |
| iot | platform, ledger, connectors, engine, life, neocortex, ras, work, worker |
| ops | platform, ledger, desiredstate, eidos, engine, iot, qhorus, ras, work, agency |
| chat-app | platform, qhorus, life |

### Application Modules

| Repo | Depends on |
|------|-----------|
| openclaw | platform, ledger, engine, pages, casehub-mosaic, qhorus |
| life | platform, ledger, connectors, engine, neocortex, openclaw, qhorus, work |
| devtown | platform, ledger, connectors, engine, agency, life, neocortex, qhorus, work, worker |
| aml | platform, ledger, engine, agency, eidos, life, neocortex, qhorus, work |
| clinical | platform, ledger, connectors, engine, agency, eidos, life, neocortex, qhorus, work, worker |
| soc | platform, ledger, desiredstate, eidos, engine, neocortex, qhorus, ras, work, worker |
| fsitrading | platform, ledger, desiredstate, engine, agency, eidos, neocortex, ops, qhorus, ras, work, worker |
| quarkmind | platform, ledger, connectors, engine, agency, eidos, life, neocortex, qhorus |
| scaffold | platform, engine, life, work |

## Build Grouping and Parallelism

The dependency graph determines the minimum number of sequential phases. Within
each phase, independent repos build in parallel.

### Phase Layout

```
Phase 0 ─── Platform base (3 modules)
              │
Phase 1 ─── Platform slices A/B/C/D (4 parallel Maven processes)
              │
Phase 2 ─── Platform convergence (Spring modules)
              │
Phase 3 ─── G1: worker, ledger, connectors, neocortex, pages
              │
Phase 4 ─── G2: eidos, qhorus
              │
Phase 5 ─── G3: work, engine, drafthouse
              │
Phase 6 ─── G4: ras, agency, langchain4j, mosaic, workers
              │
Phase 7 ─── G5: claudony, openclaw, soc, desiredstate, life, iot, chat-app
              │
Phase 8 ─── G6: devtown, aml, clinical, quarkmind, scaffold, ops, fsitrading
```

**Note:** `engine` and `work` have a circular dependency (engine depends on
work, work depends on engine). Both are in G3 — Maven resolves the cycle
within a single reactor phase.

### Grouping Table

| Phase | Group | Repos | Parallelism | Rationale |
|-------|-------|-------|-------------|-----------|
| 0 | Platform base | platform-api, platform-core, playbook-catalog | Sequential | Required by all slices |
| 1 | Platform slices | A (44 modules), B (57), C (53), D (15) | 4 parallel processes | Zero cross-deps between slices; separate JVMs avoid Quarkus deadlock |
| 2 | Platform convergence | Spring starters + generators (40 modules) | Sequential | Depends on all 4 slices |
| 3 | G1 Core libs | worker, ledger, connectors, neocortex, pages | Sequential | All depend only on platform |
| 4 | G2 Domain | eidos, qhorus | Sequential | eidos depends on ledger (G1), qhorus depends on ledger + connectors + eidos |
| 5 | G3 Middleware | work, engine, drafthouse | Sequential | Depend on G1+G2 outputs; engine↔work cycle resolved within reactor |
| 6 | G4 Extensions | ras, agency, langchain4j, mosaic, workers | Sequential | Depend on G3 outputs |
| 7 | G5 Apps (independent) | claudony, openclaw, soc, desiredstate, life, iot, chat-app | Sequential | Depend on G4 outputs |
| 8 | G6 Apps (chain) | devtown, aml, clinical, quarkmind, scaffold, ops, fsitrading | Sequential | Some depend on G5 outputs (life, iot) |

Within each group, Maven's `-T 1C` parallelises compilation across modules.

### Per-Repo CI Times (standalone build + test)

Canonical times from the latest successful main-branch CI run per repo as of
2026-10-26. These are full `mvn verify` runs including all tests.

| Repo | CI Time | Modules | @QuarkusTest | Group | Status |
|------|---------|---------|-------------|-------|--------|
| platform | 35m 54s | 212 | 61 | Slices | green |
| work | 26m 43s | 28 | 150 | G3 | red (test) |
| engine | 21m 48s | 69 | 141 | G3 | green |
| iot | 17m 52s | 14 | — | G5 | green |
| neocortex | 15m 50s | 96 | — | G1 | green |
| quarkmind | 15m 40s | — | — | G6 | green |
| connectors | 14m 44s | 43 | — | G1 | green |
| casehub-mosaic | 13m 58s | — | — | G4 | red (test) |
| fsitrading | 12m 44s | — | — | G6 | red (test) |
| qhorus | 12m 14s | 43 | — | G2 | green |
| casehub-langchain4j | 10m 22s | — | — | G4 | green |
| soc | 8m 05s | — | — | G5 | green |
| agency | 7m 31s | — | — | G4 | green |
| ledger | 7m 30s | — | — | G1 | green |
| eidos | 7m 16s | — | — | G2 | green |
| devtown | 6m 44s | — | — | G6 | green |
| aml | 6m 36s | — | — | G6 | green |
| scaffold | 6m 24s | — | — | G6 | green |
| claudony | 6m 12s | — | — | G5 | green |
| drafthouse | 5m 37s | — | — | G3 | green |
| life | 4m 18s | — | — | G6 | green |
| chat-app | 3m 04s | — | — | G5 | green |
| workers | 2m 59s | — | — | G4 | green |
| clinical | 2m 54s | — | — | G6 | green |
| openclaw | 2m 47s | — | — | G5 | green |
| worker | — | — | — | G1 | no CI |
| desiredstate | — | — | — | G5 | no CI |
| ras | — | — | — | G4 | no CI |
| pages | — | — | — | G1 | no CI |
| ops | — | — | — | G6 | no CI |

**Repos without CI:** These repos are verified only via casehub-all (compile)
and as transitive dependencies of downstream repos that do have CI.
- **worker** — small library, API stable, consumed by engine/agency/iot
- **pages** — Yarn/TypeScript monorepo, tested via casehub-mosaic CI
- **ras** — tested transitively via soc, fsitrading, ops
- **desiredstate** — tested transitively via ops, soc
- **ops** — tested transitively via fsitrading

**Critical path** (longest sequential chain): platform → G1 → G2 → G3 → ops → fsitrading.
If tests ran sequentially across repos: ~36 + 15 + 12 + 27 + ? + 13 ≈ 103+ min.

### casehub-all Build Times (compile-only, `test_mode=skip`)

| Phase | Time |
|-------|------|
| Platform: base + 4 slices + convergence | 84s |
| G1: Core libs | ~2m 30s |
| G2: Domain | ~1m 30s |
| G3: Middleware | 1m 30s |
| G4: Extensions | 4m 19s |
| G5+G6: Apps | 1m 20s |
| **Total (incl setup)** | **~14 min** |

## Quarkus Bugs and Limitations

### FacadeClassLoader Deadlock (quarkusio/quarkus#57751)

**Status:** Open. PR #57768 not merged.

**What happens:** When Maven runs with `-T 1C` and the `quarkus:build` goal
executes for multiple modules in the same Maven process, `FacadeClassLoader.loadClass()`
deadlocks. The class loading strategy uses a parent-first delegation model that
acquires locks in conflicting order when multiple threads augment simultaneously.

**Impact:** Cannot use parallel Maven for any reactor that includes Quarkus
application modules with augmentation enabled.

**Workaround:** `-Dquarkus.build.skip=true` disables the `quarkus:build` goal
entirely. This makes `-T 1C` safe because the deadlock is in the augmentation
phase, not compilation or testing. casehub-all only needs compile verification —
not runner JARs — so this is lossless for CI purposes.

**Why tests still work with `quarkus.build.skip=true`:** `@QuarkusTest` triggers
augmentation inside Surefire's forked JVM, not in the Maven process. Each test
fork gets its own classloader hierarchy. The deadlock is specific to multiple
`quarkus:build` goals running in the same JVM via `-T 1C`.

### Quarkus Test Boot Cost

Each distinct `@QuarkusTest` configuration profile requires a full Quarkus
augmentation + boot cycle (~2-5 seconds each). Repos with many profiles pay
a multiplicative boot tax:

| Repo | @QuarkusTest | Boot Points | Time per Boot |
|------|-------------|-------------|---------------|
| engine | 141 | 35 | ~2-5s |
| work | 150 | 14 | ~2-5s |
| platform | 61 | ~30 | ~2-5s |

Boot points = distinct Quarkus configurations that trigger separate starts.
Tests sharing the same profile share the same boot — so consolidating duplicate
profiles reduces boot points. However, gains are modest (~20-40s per
consolidation) because most remaining profiles genuinely differ in configuration.

### CDI / Jandex Discovery

Quarkus uses Jandex (not classpath scanning) to discover CDI beans. A class on
the classpath is invisible to CDI unless it's in a Jandex index. Dependencies
must either embed `jandex.idx` in their JAR, be listed in
`quarkus.index-dependency`, or include `META-INF/beans.xml`.

Common failure: `UnsatisfiedResolutionException` when a dependency is on the
classpath but not indexed.

### Spring-Generator Jandex Race

The platform's Spring/REST/GraphQL/MCP code generators read Jandex indexes from
sibling module `target/` directories via filesystem path, not Maven dependency.
In a multi-module reactor, Maven cannot guarantee the sibling compiles before
the generator runs.

**Workaround:** Each `-spring` module declares a `provided`-scope dependency on
its paired Quarkus module. This establishes reactor ordering without polluting
the transitive dependency graph. Applied to engine (x5), platform (x1), work (x1).

**Escape hatch:** `-Dcasehub.generators.skip=true`

## Runner Constraints (GitHub Actions Free Tier)

GitHub Actions free-tier runners have **2 CPUs** and 7 GB RAM. This fundamentally
shapes the CI strategy:

| Constraint | Impact | Mitigation |
|-----------|--------|------------|
| 2 CPUs | `-T 1C` means 2 threads max. Parallelism tops out at 2x. | Platform uses 4 separate Maven processes (background `&`), not `-T 1C` within one process. Each gets its own JVM. |
| 2 CPUs | 4 parallel processes time-share, not truly parallel. | Still faster than serial: Maven's I/O-wait phases (dependency resolution, JAR writes) overlap with another process's CPU phases (compilation). |
| 7 GB RAM | Multiple Maven processes compete for heap. | `MAVEN_OPTS='-Xmx4g'` for the main process. Slice processes inherit this. |
| No persistent cache across runs | `.m2` cache is restored from GitHub Actions cache, adding ~30s startup. | Cache keyed on `parent/pom.xml` + `platform/pom.xml` hash. Restore-keys fall back to any prior cache. |
| No cross-job artifact sharing | Each job starts clean. | All phases run in a single job to share `.m2` artifacts installed by earlier phases. |

**Why 4 platform slices, not 2:** On a 2-CPU runner, 4 concurrent Maven processes
outperform 2 because Maven's build phases alternate between CPU-bound and I/O-bound
work. While slice A waits on a dependency download, slice D can use the CPU for
compilation. Slice D (15 modules, 0 `@QuarkusTest`) finishes in ~26 seconds and
frees its process for the heavier slices. Merging it into a larger slice would
add 15 modules to an already-heavy build without reducing wall-clock time.

## Workflow Inputs

The `mvn-all.yml` workflow is parameterised via `workflow_dispatch` inputs:

| Input | Type | Default | Effect |
|-------|------|---------|--------|
| `test_mode` | choice | `skip` | `skip` = no compile/run (`maven.test.skip=true`); `compile` = compile tests only (`-DskipTests`); `run` = compile + execute tests |
| `quarkus_build` | boolean | `false` | When true, runs Quarkus augmentation (produces runner JARs). Leave false — the deadlock makes this unsafe with `-T 1C`. |
| `enforcer` | boolean | `false` | When true, runs Maven enforcer plugin. |
| `maven_args` | string | (empty) | Escape hatch for any additional Maven flags. |
| `resume_from` | choice | (empty) | Resume from a group: `g1`, `g3`, `g4`, or `g5`. Skips earlier groups — relies on `.m2` cache from a prior run. |

```bash
# Default (compile-only, no tests)
gh workflow run mvn-all.yml --repo casehubio/casehub-all

# Compile tests but don't run them (surfaces cross-repo API drift)
gh workflow run mvn-all.yml --repo casehubio/casehub-all -f test_mode=compile

# Full test run
gh workflow run mvn-all.yml --repo casehubio/casehub-all -f test_mode=run
```

`MVN_COMMON` is assembled dynamically from these inputs in the "Configure Maven
flags" step and logged via `::notice::`.

## Known Limitations

### resume_from is fragile
See "Resuming from a Group" above. Only safe when earlier groups haven't
changed since the cached run. Stale cache artifacts can mask or fabricate
failures.

### Pointer advance race
The workflow advances submodule pointers and pushes on success. If two runs
complete near-simultaneously, the second push is rejected (`fetch first`).
Harmless — the pointers from the first run are correct.

### SNAPSHOT timing between repos
Each repo publishes SNAPSHOTs independently. If repo A publishes a breaking
change and repo B's CI runs before A's CI finishes publishing, B pulls the old
SNAPSHOT. Fix: wait for upstream CI to publish, then re-trigger downstream.

## Operations

### Fixing a Red Repo

1. Check idle state: `git -C ~/claude/casehub/REPO rev-parse --abbrev-ref HEAD`
2. If not on main or dirty: note branch, stash, switch to main.
3. Reproduce locally: `mvn -f ~/claude/casehub/REPO/pom.xml verify`
4. Fix, commit, push to origin + fork.
5. Restore original branch.

### Adding a New Repo

1. Add to `parent/build/modules-core.csv` or `modules-applications.csv`
2. Add to the appropriate `aggregator-g*.xml`
3. Add as submodule in casehub-all
4. Run `python3 scripts/reconcile-submodules.py` in casehub-all

## File Map

```
parent/
  build/
    modules-core.csv              — core repo dependency declarations
    modules-applications.csv      — app repo dependency declarations
  aggregator-foundation.xml       — legacy: all core repos (still works)
  aggregator-apps.xml             — legacy: all app repos (still works)
  aggregator-g1-core-libs.xml     — G1: worker, ledger, connectors, neocortex, pages
  aggregator-g2-domain.xml        — G2: eidos, qhorus
  aggregator-g3-middleware.xml    — G3: work, engine, drafthouse
  aggregator-g4-extensions.xml    — G4: ras, agency, langchain4j, mosaic, workers
  aggregator-g5-apps-independent.xml — G5: independent apps
  aggregator-g6-apps-chain.xml    — G6: chained apps (ops→fsitrading)

platform/
  aggregator-base.xml             — base slice (platform-api, platform-core)
  aggregator-slice-a.xml          — Agent/AI (44 modules)
  aggregator-slice-b.xml          — Identity/Auth (57 modules)
  aggregator-slice-c.xml          — Events/Comms (53 modules)
  aggregator-slice-d.xml          — YAML/Tooling (15 modules)
  aggregator-convergence.xml      — Spring convergence (40 modules)

casehub-all/
  .github/workflows/mvn-all.yml   — 9-phase grouped build (parameterised)
  scripts/reconcile-submodules.py — syncs submodules from CSVs
```
