# CaseHub CI Architecture

## Overview

CaseHub is a multi-repo ecosystem (~30 repos) under the `casehubio` GitHub org.
The repos have a dependency graph that determines build order. CI operates at
two levels:

1. **Per-repo CI** — each repo has its own `publish.yml` workflow that builds,
   tests, and publishes SNAPSHOT artifacts to GitHub Packages on push to main.
2. **Cross-repo CI** (`casehub-all`) — verifies the entire ecosystem compiles
   together. Uses git submodules to pull all repos into one workspace and builds
   them via dependency-grouped aggregator POMs across multiple GitHub Actions jobs.

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

## SHA Management and Submodule Pointers

casehub-all tracks every repo as a git submodule. Each submodule pointer is a
SHA — the exact commit that was last verified to compile with the rest of the
ecosystem.

### The Build Cycle

```
1. Checkout casehub-all (with submodule pointers from last green build)
2. git submodule update --remote           ← pull ALL submodules to latest main
3. Record SHA manifest                     ← log exact SHAs for reproducibility
4. Build platform (base → slices → convergence)
5. Build ecosystem (G1 → G2 → G3 → G4 → G5 → G6)
6. If green: git add -u && git commit && git push  ← advance pointers
```

After step 2, every submodule points to the latest `main` commit of its repo.
If the build succeeds, step 6 commits those SHAs back to casehub-all. This
means the committed submodule pointers always represent the **last known-good
combination** of all repos.

**Pointer advance is all-or-nothing.** A failure in any phase prevents
advancing any pointers, even for phases that succeeded earlier. The `git push`
only runs `if: success()` — meaning every phase must pass. The committed SHAs
on main always represent a fully-verified combination, never a partial build.

### SHA Manifest

The SHA manifest is logged in the CI output for every run. This is the
reproducibility record — given these exact SHAs, the build can be reproduced
deterministically. Useful for bisecting cross-repo regressions.

### Resuming from a Group

The `resume_from` input (choice type: `g1`, `g3`, `g4`, `g5`) skips earlier
phases. For example, `resume_from=g3` skips platform, G1, and G2, jumping
straight to G3 (middleware). This works because:

1. Earlier phases installed their artifacts into `.m2/repository` during a
   prior run.
2. GitHub Actions caches `.m2/repository` between runs (keyed on
   `parent/pom.xml` + `platform/pom.xml` hash).
3. G3+ phases resolve their dependencies from `.m2` — they don't care whether
   the artifacts were built in this run or restored from cache.

When resuming, the prepare and platform-slices jobs are skipped entirely. The
build job runs standalone using cached `.m2`.

**When resumption is safe:** Immediately after a failure in a later group, when
the earlier groups haven't changed. You fix the failing repo, push, and
re-trigger with `resume_from=g3` to skip rebuilding platform and core libs.

**When resumption is unsafe:** If any earlier repo has published a new SNAPSHOT
since the cached run. The cache holds stale artifacts, so downstream modules
compile against the old API.

**Warning — resumed builds weaken the pointer guarantee:** The submodule
update still pulls ALL repos to latest main regardless of `resume_from`.
If the build passes, the pointer advance commits SHAs for *every* repo —
including repos whose build phases were skipped. The SHAs are accurate (latest
main), but the verification is partial: skipped phases were not rebuilt, so
API compatibility between skipped and built phases is assumed, not proven.

## Frontend Build Cache

Three modules have heavy frontend builds (yarn install + TypeScript compilation):
**pages**, **casehub-mosaic**, and **drafthouse**. These are cached between runs
using the submodule commit SHA as the cache key.

### How it works

After `git submodule update --remote`, the workflow computes each submodule's
HEAD SHA and uses it as the `actions/cache` key:

```
key: frontend-Linux-<pages-sha>-<mosaic-sha>-<drafthouse-sha>
```

The cache stores:
- `node_modules/` — skips `yarn install` / `npm install` on hit
- `dist/` directories — skips `tsc --build` on hit (TypeScript output)
- `.tsbuildinfo` files — enables TypeScript incremental compilation
- `target/quinoa/` — skips Quinoa build on hit (pages, drafthouse)

### When the cache resets

The cache key includes all three SHAs. A new commit to **any** of the three
repos changes the key, causing a full cache miss and rebuild of **all** three.
This is intentional — a single composite key is simpler than three separate
caches, and frontend modules rarely all change independently in the same build.

### When the cache hits

If none of pages, mosaic, or drafthouse have new commits since the last build:
- `yarn install` → no-op (node_modules already present)
- `tsc --build` → no-op (incremental build sees nothing changed)
- Quinoa → skips build (target/quinoa already has resources)

Expected savings: **~15 min per unchanged module** (pages ~5 min, mosaic ~12 min,
drafthouse ~3 min).

### Observing cache behaviour

Every build summary reports cache status:

```
## Frontend Cache
✅ Cache HIT — yarn install + tsc build skipped for unchanged modules

| Module | SHA |
|--------|-----|
| pages | abc1234 |
| mosaic | def5678 |
| drafthouse | ghi9012 |
```

Compare total run duration between a MISS run (first build after a frontend
change) and a HIT run (no frontend changes) to measure the savings.

The `actions/cache` step also logs `Cache restored from key: ...` or
`Cache not found for input keys: ...` in the step output.

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

The workflow uses three GitHub Actions jobs. Each job runs on its own runner
(2 CPUs, 7 GB RAM). Platform slices use a matrix strategy to spawn 4 parallel
jobs, giving the slice phase 8 CPUs total.

### Job Architecture

```
┌──────────────────────────┐
│  Job 1: prepare          │  1 runner (2 CPUs)
│  checkout + submodules   │
│  platform base (3 mods)  │
│  upload .m2 + jandex     │
└──────────┬───────────────┘
           │ needs: prepare
           ▼
┌──────────────────────────┐
│  Job 2: platform-slices  │  4 runners (8 CPUs total)
│  matrix: [a, b, c, d]   │
│  download base artifacts │
│  build slice             │
│  upload .m2 + jandex     │
└──────────┬───────────────┘
           │ needs: platform-slices (waits for ALL 4)
           ▼
┌──────────────────────────┐
│  Job 3: build            │  1 runner (2 CPUs)
│  download all artifacts  │
│  convergence             │
│  G1 → G2 → G3 → G4      │
│  → G5 → G6              │
│  advance pointers        │
└──────────────────────────┘
```

Artifacts passed between jobs:
- **`.m2` artifacts** (`m2-platform-*`): installed JARs for Maven dependency
  resolution. Downloaded into `~/.m2/repository/io/casehub/`.
- **Jandex indexes** (`jandex-platform-*`): `target/classes/META-INF/jandex.idx`
  files. Downloaded into `platform/` in the workspace. Required because the
  Spring generators read Jandex from the filesystem, not `.m2`.

### Phase Layout

```
Job 1 (prepare):
  Phase 0 ─── Platform base (3 modules)

Job 2 (platform-slices, 4 parallel runners):
  Phase 1 ─── Slice A (44 modules) — Agent/AI: agents, simulation, mcp, llm-config
              Slice B (57 modules) — Identity/Auth: authn, acl, persistence, subscriptions
              Slice C (53 modules) — Events/Comms: streams, notifications, callback, graphql
              Slice D (15 modules) — YAML/Tooling: yaml, observability, pdf, signing

Job 3 (build):
  Phase 2 ─── Platform convergence (40 modules) — Spring starters + generators
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

| Phase | Group | Repos | Runner | Rationale |
|-------|-------|-------|--------|-----------|
| 0 | Platform base | platform-api, platform-core, playbook-catalog | prepare | Required by all slices |
| 1 | Platform slices | A (44), B (57), C (53), D (15 modules) | 4 separate runners | Zero cross-deps; each gets its own 2 CPUs |
| 2 | Convergence | Spring starters + generators (40 modules) | build | Depends on all 4 slices + Jandex indexes |
| 3 | G1 Core libs | worker, ledger, connectors, neocortex, pages | build | All depend only on platform |
| 4 | G2 Domain | eidos, qhorus | build | eidos depends on ledger (G1), qhorus on ledger + connectors + eidos |
| 5 | G3 Middleware | work, engine, drafthouse | build | Depend on G1+G2; engine↔work cycle resolved within reactor |
| 6 | G4 Extensions | ras, agency, langchain4j, mosaic, workers | build | Depend on G3 outputs |
| 7 | G5 Apps (independent) | claudony, openclaw, soc, desiredstate, life, iot, chat-app | build | Depend on G4 outputs |
| 8 | G6 Apps (chain) | devtown, aml, clinical, quarkmind, scaffold, ops, fsitrading | build | Some depend on G5 outputs (life, iot) |

Within each group, Maven's `-T 1C` parallelises compilation across modules
(2 threads per runner).

### Per-Repo CI Status (as of 2026-10-27)

All per-repo CIs are green. Full `mvn verify` times from latest successful runs.

| Repo | CI Time | Group | Notes |
|------|---------|-------|-------|
| platform | ~36m | Slices | 212 modules, 61 @QuarkusTest |
| work | ~27m | G3 | MongoDB tests skip without Docker |
| engine | ~22m | G3 | ~1500 tests |
| iot | ~18m | G5 | HomeAssistant WS tests skip on CI |
| neocortex | ~16m | G1 | ONNX latency test skips on CI |
| quarkmind | ~16m | G6 | |
| connectors | ~15m | G1 | Discord WS + Email tests hardened for CI |
| casehub-mosaic | ~12m | G4 | declaration emit disabled for agent-viewer |
| fsitrading | ~13m | G6 | ops-service excluded from classpath |
| qhorus | ~12m | G2 | |
| casehub-langchain4j | ~10m | G4 | |
| soc | ~8m | G5 | |
| agency | ~8m | G4 | |
| ledger | ~8m | G1 | |
| eidos | ~7m | G2 | |
| devtown | ~7m | G6 | |
| aml | ~7m | G6 | |
| scaffold | ~6m | G6 | |
| claudony | ~6m | G5 | |
| drafthouse | ~6m | G3 | E2E tests excluded (need Playwright) |
| ops | ~5m | G5 | |
| life | ~4m | G6 | |
| chat-app | ~3m | G5 | |
| workers | ~3m | G4 | Script tests skip on CI |
| clinical | ~3m | G6 | |
| openclaw | ~3m | G5 | |
| pages | ~8m | G1 | Maven publish + frontend-artifacts |

### Tests Skipped on CI

Some tests require infrastructure not available on GitHub Actions runners.
They run locally but are skipped on CI via `@DisabledIfEnvironmentVariable(CI=true)`
or surefire `<exclude>`:

| Test | Repo | Reason |
|------|------|--------|
| `DiscordGatewayTest` | connectors | WebSocket needs quiet env, flaky under `-T 1C` |
| `StrategyClassifierOnnxTest.latencyUnderThreshold` | neocortex | Latency benchmark unreliable on shared runners |
| `HomeAssistantWebSocketClientTest` | iot | Connects to hardcoded port 8081 |
| `ScriptWorkerExecutionManagerTest` | workers | Process execution flaky under `-T 1C` |
| `BrainstormE2ETest` + all `e2e/*` | drafthouse | Playwright browser driver not installed |

### casehub-all Build Times

| Mode | Phases | Total |
|------|--------|-------|
| `test_mode=skip` (compile only) | prepare ~2m, slices ~2m, G1-G6 ~12m | **~16 min** |
| `test_mode=run` (full tests) | prepare ~2m, slices ~15m, G1-G6 ~75m | **~90 min** |

Step timeouts (per-step, not per-job):

| Step | Timeout | Typical |
|------|---------|---------|
| Platform convergence | 10m | ~3m |
| G1: Core libs | 25m | ~15m |
| G2: Domain | 20m | ~10m |
| G3: Middleware | 60m | ~45m (engine tests) |
| G4: Extensions | 90m | ~15m |
| G5+G6: Apps | 90m | ~30m |

Setup overhead per job (checkout, cache restore, artifact download) adds ~1-2
minutes per job transition.

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

**Workaround (single-job builds):** Each `-spring` module declares a
`provided`-scope dependency on its paired Quarkus module. This establishes
reactor ordering without polluting the transitive dependency graph. Applied to
engine (x5), platform (x1), work (x1).

**Workaround (multi-job builds):** Slice jobs upload their
`target/classes/META-INF/jandex.idx` files as artifacts. The build job
downloads them into the `platform/` workspace before running convergence.
Both `.m2` artifacts (JARs) and Jandex indexes (filesystem) must be passed.

**Escape hatch:** `-Dcasehub.generators.skip=true`

## Runner Constraints (GitHub Actions Free Tier)

GitHub Actions free-tier runners have **2 CPUs** and 7 GB RAM. Each job in the
workflow gets its own runner.

| Constraint | Impact | Mitigation |
|-----------|--------|------------|
| 2 CPUs per runner | `-T 1C` means 2 threads per job. | Platform slices run as 4 separate jobs = 8 CPUs total for the slice phase. |
| 7 GB RAM per runner | Each job has its own 7 GB — no memory sharing between jobs. | `MAVEN_OPTS='-Xmx4g'` per job. |
| No shared filesystem | Each job starts with a clean workspace. | Artifacts pass `.m2` JARs and Jandex indexes between jobs via `actions/upload-artifact` / `actions/download-artifact`. |
| Cache is per-repo, not per-job | All jobs restore the same `.m2` cache (snapshot from prior run). | Each job restores independently. Fresh build outputs pass via artifacts, not cache. |

**Why 4 platform slices:** The slices have zero cross-dependencies (verified).
Each runs on its own 2-CPU runner. The wall-clock time for the slice phase is
determined by the slowest slice, not the sum. For compile-only, each slice
takes ~30s-1m. For test-enabled builds, slice C (53 modules, 25 @QuarkusTest)
is the bottleneck — having its own 2 CPUs avoids contention with the other
slices.

## Workflow Inputs

The `mvn-all.yml` workflow is parameterised via `workflow_dispatch` inputs:

| Input | Type | Default | Effect |
|-------|------|---------|--------|
| `test_mode` | choice | `skip` | `skip` = no compile/run (`maven.test.skip=true`); `compile` = compile tests only (`-DskipTests`); `run` = compile + execute tests |
| `quarkus_build` | boolean | `false` | When true, runs Quarkus augmentation (produces runner JARs). Leave false — the deadlock makes this unsafe with `-T 1C`. |
| `enforcer` | boolean | `false` | When true, runs Maven enforcer plugin. |
| `maven_args` | string | (empty) | Escape hatch for additional Maven flags. Passed via `GITHUB_ENV` to prevent shell injection. |
| `resume_from` | choice | (empty) | Resume from: `g1`, `g3`, `g4`, or `g5`. Skips prepare + platform-slices jobs entirely; build job uses cached `.m2`. |

```bash
# Default (compile-only, no tests)
gh workflow run mvn-all.yml --repo casehubio/casehub-all

# Compile tests but don't run them (surfaces cross-repo API drift)
gh workflow run mvn-all.yml --repo casehubio/casehub-all -f test_mode=compile

# Full test run
gh workflow run mvn-all.yml --repo casehubio/casehub-all -f test_mode=run
```

`MVN_COMMON` is assembled dynamically from inputs in the "Configure Maven
flags" step. `maven_args` is set as the `MAVEN_ARGS` environment variable
(not interpolated directly) and logged via `::notice::`.

## Known Limitations

### resume_from is fragile
See "Resuming from a Group" above. Only safe when earlier groups haven't
changed since the cached run. The Maven `.m2` cache is keyed on
`parent/pom.xml` + `platform/pom.xml` hash — it does NOT invalidate when
individual repo SNAPSHOTs are republished. Stale cache artifacts can mask
or fabricate failures. Always run a full build (no `resume_from`) for
definitive verification.

### Pointer advance race
The workflow advances submodule pointers and pushes on success. If two runs
complete near-simultaneously, the second push may fail. Mitigated with
`git pull --rebase` before push. If rebase fails, the push is skipped —
the pointers from the other run are correct.

### Stale submodule pointers
The committed submodule SHAs only advance when a full green build completes.
If the reactor is red, pointers stay at the last green build — which may be
days old. This is by design (pointers = verified state), but means
`git submodule update` without `--remote` gives stale code.

### SNAPSHOT timing between repos
Each repo publishes SNAPSHOTs independently. If repo A publishes a breaking
change and repo B's CI runs before A's CI finishes publishing, B pulls the old
SNAPSHOT. Fix: wait for upstream CI to publish, then re-trigger downstream.

Maven also caches "not found" resolution failures. If a dependency wasn't
published when Maven first checked, it won't retry until the update interval
expires. Fix: add `-U` to force SNAPSHOT update checks.

### Aggregator POM drift
The `<modules>` lists in `aggregator-g*.xml` and `aggregator-slice-*.xml` are
manually maintained. When a new module is added to a repo, the aggregator
must be updated manually. A future improvement: generate aggregator POMs from
CSVs, or add a CI validation step.

### Reactor-only CDI issues
Some modules have CDI deployment problems that only manifest in the casehub-all
reactor, not in standalone CI. The reactor classpath can differ from standalone
because Maven reactor resolution resolves from `target/` instead of `.m2`,
and `-fae` continues past failures that change the effective classpath.

Known: claudony has 28 unsatisfied CDI dependencies in G5+G6 `test_mode=run`
(Ledger SPIs, ProvisionerConfigRegistry). Standalone claudony CI is green.

### Mosaic TypeScript in reactor context
Mosaic's TypeScript build resolves `@casehubio/*` packages via portal links
to `.casehub-packages/` (unpacked from Maven artifacts). The type resolution
can differ from standalone Yarn workspace resolution, causing TS errors that
don't reproduce locally. Current mitigations:
- `declaration: false` in agent-viewer's tsconfig.build.json (TS4094)
- `never` exhaustiveness checks replaced with fallback defaults

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
  .github/workflows/mvn-all.yml   — 3-job, 9-phase build (parameterised)
  scripts/reconcile-submodules.py — syncs submodules from CSVs
```
