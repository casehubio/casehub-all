# CaseHub CI Architecture

## Overview

CaseHub is a multi-repo ecosystem (~30 repos) under the `casehubio` GitHub org.
The repos have a dependency graph that determines build order. CI operates at
two levels:

1. **Per-repo CI** — each repo has its own `publish.yml` workflow that builds,
   tests, and publishes SNAPSHOT artifacts to GitHub Packages on push to main.
2. **Cross-repo CI** (`casehub-all`) — verifies the entire ecosystem compiles
   together. Uses git submodules to pull all repos into one workspace and builds
   them in a dependency-optimized parallel pipeline across multiple GitHub Actions
   jobs.

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
  pipeline. Each level runs `mvn install`, populating `.m2` for subsequent
  levels. This uses the same resolution mechanism as SNAPSHOTs, but is
  deterministic within a single run because no external publishes can
  intervene. API drift is caught immediately.
- **Reproducible SHA manifest:** Every build records the exact SHA of every
  repo. Given these SHAs, the build can be reproduced deterministically.
  `git submodule status` is the bill of materials.
- **Pointer advance = verified state:** The committed submodule pointers
  represent the last combination that compiled together. Anyone cloning
  casehub-all gets a known-good set of versions.

## Parent BOM and Version Management

All repos extend `casehub-parent` — a single Bill of Materials (BOM) POM that
centralises dependency versions, plugin configuration, and enforcer rules.

### What casehub-parent manages

- **Quarkus BOM** (3.39.x) — all Quarkus dependencies
- **Spring Boot BOM** (4.1.x) — all Spring dependencies
- **Serverless Workflow** (7.35.3.Final) — workflow type system
- **Quarkus MCP Server** (1.11.1) — MCP protocol support
- **LangChain4j** (1.19.x) — LLM integration
- **Surefire** (3.5.5) — test runner, with `forkedProcessTimeoutInSeconds=300`
- **Maven Enforcer** — `dependencyConvergence` rule (fail on version conflicts)

### Repos extending casehub-parent

Platform, engine, qhorus, ledger, connectors, neocortex, eidos, agency, work,
claudony, scaffold, clinical, pages, casehub-langchain4j (14 repos).
The remaining repos (iot, aml, fsitrading, etc.) inherit transitively or will
be migrated incrementally.

### Version override pattern

Repos that need a different version of a managed dependency declare it in
their own `<properties>` or `<dependencyManagement>`. The enforcer's
`dependencyConvergence` rule catches conflicting transitive versions.

For example, `work` pins `serverlessworkflow` to 7.13 (breaking API change
in 7.35) and `claudony` pins Quarkus to 3.32 (pending migration).

## SHA Management and Submodule Pointers

casehub-all tracks every repo as a git submodule. Each submodule pointer is a
SHA — the exact commit that was last verified to compile with the rest of the
ecosystem.

### The Build Cycle

```
1. Checkout casehub-all (with submodule pointers from last green build)
2. Update submodules (mode-dependent — see submodule_update input)
3. Check dependency graph for cycles (docs/check-cycles.py)
4. Record SHA manifest                     ← log exact SHAs for reproducibility
5. Build platform (base → 4 parallel slices → convergence)
6. Build ecosystem (L1–L9 in dependency order, with parallelism within levels)
7. If green: git add -u && git commit && git push  ← advance pointers
8. If failed: auto-retry with resume_from at the failed level (max 2 retries)
```

Step 2 depends on the `submodule_update` input:
- **`pinned`** (default): use committed SHAs — deterministic, no surprises
- **`all`**: pull latest main for every repo (legacy behavior)
- **`list`**: pull latest main only for named repos (comma-separated)

Step 3 validates the dependency CSVs are acyclic using topological sort
(Kahn's algorithm). This prevents circular dependency regressions that would
make the pipeline impossible to schedule.

If the build succeeds, step 7 commits the SHAs back to casehub-all. The
committed submodule pointers always represent the **last known-good
combination** of all repos.

If the build fails, step 8 detects which level failed and triggers a new
run with `resume_from` set to that level. This handles transient failures
(corrupted artifact downloads, CI runner timeouts) automatically. The
`retry_count` input caps retries at 2 to prevent infinite loops on
deterministic failures.

**Pointer advance is all-or-nothing.** A failure in any level prevents
advancing any pointers, even for levels that succeeded earlier. The `git push`
only runs `if: success()` — meaning every level must pass. The committed SHAs
on main always represent a fully-verified combination, never a partial build.

### SHA Manifest

The SHA manifest is logged in the CI output for every run. This is the
reproducibility record — given these exact SHAs, the build can be reproduced
deterministically. Useful for bisecting cross-repo regressions.

### Resuming from a Level

The `resume_from` input (choice type: `L1`, `L2`, `L3`, `L5`, `L8`) skips
earlier phases. For example, `resume_from=L3` skips platform, L1, and L2,
jumping straight to L3 (work). This works because:

1. Earlier levels installed their artifacts into `.m2/repository` during a
   prior run.
2. GitHub Actions caches `.m2/repository` between runs (keyed on
   `parent/pom.xml` + `platform/pom.xml` hash).
3. L3+ levels resolve their dependencies from `.m2` — they don't care whether
   the artifacts were built in this run or restored from cache.

When resuming, the prepare, platform-slices, and convergence jobs are skipped
entirely. Levels start from the `resume_from` point using cached `.m2`.

**When resumption is safe:** Immediately after a failure in a later level, when
the earlier levels haven't changed. You fix the failing repo, push, and
re-trigger with `resume_from=L3` to skip rebuilding platform and core libs.

**When resumption is unsafe:** If any earlier repo has published a new SNAPSHOT
since the cached run. The cache holds stale artifacts, so downstream modules
compile against the old API.

**Warning — resumed builds weaken the pointer guarantee:** The submodule
update still pulls ALL repos to latest main regardless of `resume_from`.
If the build passes, the pointer advance commits SHAs for *every* repo —
including repos whose build levels were skipped. The SHAs are accurate (latest
main), but the verification is partial: skipped levels were not rebuilt, so
API compatibility between skipped and built levels is assumed, not proven.

## Dependency Graph

Every cross-repo dependency is declared in `parent/build/modules-core.csv` and
`modules-applications.csv`. These are generated from actual POM files by
`parent/build/sync-deps.py` and determine build order.

### Core Modules

| Repo | Depends on |
|------|-----------|
| parent | — |
| platform | — |
| worker | platform |
| ledger | platform |
| connectors | platform |
| neocortex | connectors, eidos, platform |
| eidos | ledger, platform |
| desiredstate | eidos, engine, neocortex, platform, ras, work, worker |
| ras | engine, platform |
| qhorus | connectors, eidos, ledger, pages, platform |
| work | ledger, platform, qhorus |
| engine | connectors, eidos, ledger, neocortex, platform, qhorus, work, worker |
| pages | platform |
| agency | eidos, engine, ledger, neocortex, platform, qhorus, work, worker |
| casehub-langchain4j | engine, ledger, neocortex, platform |
| casehub-mosaic | agency, neocortex, pages, platform |
| workers | engine, platform, worker |
| claudony | eidos, engine, ledger, platform, qhorus, work, worker |
| ops | agency, desiredstate, eidos, engine, ledger, platform, qhorus, ras, work |
| iot | casehub-mosaic, connectors, desiredstate, engine, ledger, neocortex, pages, platform, ras, work, worker |
| chat-app | casehub-mosaic, pages, platform, qhorus |
| drafthouse | agency, eidos, pages, platform, qhorus, work |

### Application Modules

| Repo | Depends on |
|------|-----------|
| openclaw | casehub-mosaic, engine, ledger, pages, platform, qhorus |
| devtown | agency, casehub-mosaic, connectors, eidos, engine, ledger, neocortex, pages, platform, qhorus, work, worker |
| aml | agency, casehub-mosaic, eidos, engine, ledger, neocortex, pages, platform, qhorus, work |
| clinical | agency, casehub-mosaic, connectors, eidos, engine, ledger, neocortex, pages, platform, qhorus, work, worker |
| life | connectors, engine, ledger, neocortex, platform, qhorus, work |
| soc | desiredstate, eidos, engine, ledger, neocortex, pages, platform, qhorus, ras, work, worker |
| fsitrading | agency, casehub-mosaic, desiredstate, eidos, engine, ledger, neocortex, ops, pages, platform, qhorus, ras, work, worker |
| quarkmind | agency, casehub-mosaic, connectors, eidos, engine, ledger, neocortex, pages, platform, qhorus |
| scaffold | casehub-mosaic, engine, ops, pages, platform, work |

### Keeping CSVs in sync

Run `python3 parent/build/sync-deps.py` from the casehub-all root to
regenerate CSVs from actual POM dependencies. The script scans all submodule
POMs, extracts `io.casehub` dependencies (excluding test and provided scope),
maps artifact IDs to repo names, and writes the CSVs. Exit code 1 if changes
were made. The workflow runs `docs/check-cycles.py` at the start of every
build to catch cycle regressions.

## Build Architecture

### 10-Level Parallel Pipeline

The workflow uses **15+ GitHub Actions jobs** organized into 10 dependency
levels. Repos at the same level build in parallel on separate runners.
Artifacts are passed between levels via `actions/upload-artifact` /
`actions/download-artifact`.

```
L0:  prepare → platform-slices(×4) → convergence
       │
L1:  [ledger→eidos] | connectors | [worker+pages]       (3 parallel jobs)
       │
L2:  neocortex | qhorus                                   (2 parallel jobs)
       │
L3:  work                                                  (1 job)
       │
L4:  engine                                                (1 job)
       │
L5:  [ras+desiredstate] | [claudony+agency] | [langchain4j+workers]  (3 parallel)
       │
L6:  ops | casehub-mosaic                                  (2 parallel jobs)
       │
L7:  drafthouse                                            (1 job)
       │
L8:  life | iot | quarkmind | [devtown+aml+soc] | [chat-app+clinical+openclaw]  (5 parallel)
       │
L9:  [scaffold+fsitrading]                                 (1 job)
       │
fin: dashboard finish + pointer advance
```

Each level waits only for its immediate dependency level. Repos within a level
run in parallel via `strategy.matrix`. Bundled repos (e.g., `[ledger→eidos]`)
build sequentially within a single runner — the `→` indicates build order,
`+` indicates no order constraint within the bundle.

### Level Assignment Rationale

| Level | Repos | Why this level |
|-------|-------|---------------|
| L0 | platform (base, 4 slices, convergence) | Foundation for everything |
| L1 | ledger, eidos, connectors, worker, pages | Depend only on platform |
| L2 | neocortex, qhorus | Need eidos (L1), connectors (L1) |
| L3 | work | Needs qhorus (L2) |
| L4 | engine | Needs work (L3), neocortex (L2), qhorus (L2) |
| L5 | ras, desiredstate, claudony, agency, casehub-langchain4j, workers | Need engine (L4) |
| L6 | ops, casehub-mosaic | Foundational infra — ops needs desiredstate (L5), mosaic needs agency (L5) |
| L7 | drafthouse | Needs mosaic (L6) for UI |
| L8 | life, iot, quarkmind, devtown, aml, soc, chat-app, clinical, openclaw | Apps — need mosaic (L6) or drafthouse (L7) |
| L9 | scaffold, fsitrading | Need ops (L6) — scaffold and fsitrading depend on ops |

### Bundling Strategy

Repos are bundled into single jobs to avoid runner overhead (~1-2 min per job
for checkout + cache restore + artifact download). The sweet spot is ~9 minutes
per bundle — fast enough not to bottleneck, but long enough to amortize setup.

- **L1:** ledger+eidos bundled (eidos depends on ledger, ~15 min combined);
  connectors alone (~15 min); worker+pages bundled (~11 min combined)
- **L5:** ras+desiredstate (desiredstate depends on ras); claudony+agency
  (similar CDI infra); langchain4j+workers (both small)
- **L8:** Apps bundled by dependency similarity: devtown+aml+soc (all need
  mosaic+agency); chat-app+clinical+openclaw (all need mosaic)
- **L9:** scaffold+fsitrading bundled (both need ops, both small)

### Platform Build (L0)

Platform (~220 modules) is split into 3 phases across separate jobs:

1. **Prepare (1 runner):** Checkout, submodule update, cycle check, platform
   base (3 modules: platform-api, platform-core, playbook-catalog). Outputs
   `MVN_COMMON` and `MAVEN_ARGS` for all downstream jobs.

2. **Platform slices (4 parallel runners):**
   - Slice A — Agent/AI (44 modules): agents, simulation, mcp, llm-config
   - Slice B — Identity/Auth (57 modules): authn, acl, persistence, subscriptions
   - Slice C — Events/Comms (53 modules): streams, notifications, callback, graphql
   - Slice D — YAML/Tooling (15 modules): yaml, observability, pdf, signing

3. **Convergence (1 runner):** Purges all `io.casehub` from `.m2`, downloads
   slice artifacts, validates integrity, then does a full `mvn install
   -DskipTests -f platform/pom.xml` rebuild. This eliminates Jandex corruption
   from artifact downloads and ensures all ~220 modules are coherent.

### Artifact Integrity Validation

GitHub Actions artifact uploads/downloads occasionally produce corrupt files
(`invalid CEN header` for JARs, `Not a jandex index` for Jandex). The pipeline
validates artifacts at two points:

- **Producer-side (each slice runner):** After Maven build, validates all
  produced JARs with `jar tf` before uploading. Catches Maven/disk corruption
  at source.
- **Consumer-side (convergence runner):** After downloading artifacts, validates
  JARs (`jar tf`) and Jandex indexes (checks `0xCAFE` magic header). Corrupt
  files are **deleted** — the convergence step recompiles everything from source
  via `platform/pom.xml`, so deleted files are regenerated fresh.

### Artifact Passing Between Levels

Each level uploads its built artifacts to `m2-L{N}-{name}`. The next level
downloads all prior artifacts via `pattern: "m2-*"` with `merge-multiple: true`.
Before downloading, each job purges `~/.m2/repository/io/casehub` to prevent
stale cache artifacts from interfering.

Every job also installs the parent POMs (`mvn install -N -f parent/pom.xml`
and `mvn install -N -f platform/pom.xml`) since these are needed for dependency
resolution but aren't included in the uploaded artifacts.

Platform slices additionally upload Jandex indexes (`target/classes/META-INF/
jandex.idx` files) as separate artifacts. The Spring generators read Jandex from
the filesystem (not `.m2`), so these must be restored into the `platform/`
workspace directory.

### Per-Repo CI Status (as of 2026-10-30)

All per-repo CIs are green. Full `mvn verify` times from latest successful runs.

| Repo | CI Time | Level | Notes |
|------|---------|-------|-------|
| platform | ~36m | L0 (slices) | ~220 modules, 61 @QuarkusTest. Extends casehub-parent. |
| work | ~27m | L3 | MongoDB tests skip without Docker |
| engine | ~22m | L4 | ~1500 tests. Extends casehub-parent. |
| iot | ~18m | L8 | All tests enabled (dynamic ports) |
| neocortex | ~16m | L2 | ONNX latency test skips on CI. Dynamic WireMock ports. |
| quarkmind | ~16m | L8 | Dynamic test port for ReplayVisualizerIT |
| connectors | ~15m | L1 | All tests enabled (gateway leak + event-driven assertions fixed) |
| casehub-mosaic | ~12m | L6 | declaration emit disabled for agent-viewer |
| fsitrading | ~13m | L9 | ops-service excluded from classpath |
| qhorus | ~12m | L2 | Extends casehub-parent. |
| casehub-langchain4j | ~10m | L5 | Extends casehub-parent. |
| soc | ~8m | L8 | |
| agency | ~8m | L5 | Extends casehub-parent. |
| ledger | ~8m | L1 | Dynamic WireMock ports. Extends casehub-parent. |
| eidos | ~7m | L1 | Extends casehub-parent. |
| devtown | ~7m | L8 | |
| aml | ~7m | L8 | |
| scaffold | ~6m | L9 | Extends casehub-parent. |
| claudony | ~6m | L5 | Extends casehub-parent. |
| drafthouse | ~6m | L7 | E2E tests excluded (need Playwright) |
| ops | ~5m | L6 | |
| life | ~4m | L8 | |
| chat-app | ~3m | L8 | |
| workers | ~3m | L5 | All tests enabled (thread pool leak fixed) |
| clinical | ~3m | L8 | |
| openclaw | ~3m | L8 | |
| pages | ~8m | L1 | Extends casehub-parent. Maven publish + frontend-artifacts. |
| desiredstate | — | L5 | |
| ras | — | L5 | |

### Tests Skipped on CI

Some tests require infrastructure not available on GitHub Actions runners.
They run locally but are skipped on CI via `@DisabledIfEnvironmentVariable(CI=true)`
or surefire `<exclude>`:

| Test | Repo | Reason |
|------|------|--------|
| `StrategyClassifierOnnxTest.latencyUnderThreshold` | neocortex | Latency benchmark unreliable on shared runners |
| `BrainstormE2ETest` + all `e2e/*` | drafthouse | Playwright browser driver not installed |

Previously skipped tests that have been **re-enabled** (root causes fixed):

| Test | Repo | What was fixed |
|------|------|---------------|
| `DiscordGatewayTest` (10 tests) | connectors | Gateway thread leak — `disconnect()` now joins connect thread + AutoCloseable |
| `ScriptWorkerExecutionManagerTest` | workers | Thread pool leak — `@AfterEach` cleanup of unclosed CachedThreadPool |
| `HomeAssistantWebSocketClientTest` | iot | Was NOT actually disabled — hardcoded port 8081 → dynamic port |
| `DiscordChatPlatformTest` | connectors | Awaitility polling → CountDownLatch (event-driven) |
| `EmailInboundConnectorTest` | connectors | Awaitility removed — LinkedBlockingQueue.poll() is already event-driven |
| `PlaybookOrchestratorTest` | pages | Thread.sleep(2000) → CountDownLatch + 5s timeout |
| `RaceStepTests` / `SelectStepTests` | platform | Scope leak + submission/cancel race fixed with CountDownLatch gate |

### Build Times (estimated)

| Mode | Critical path | Total wall-clock |
|------|--------------|-----------------|
| `test_mode=skip` (compile only) | L0 ~5m → L1 ~3m → L2 ~2m → L3 ~2m → L4 ~2m → L5 ~2m → L6 ~2m → L7 ~1m → L8 ~2m → L9 ~1m | **~22 min** |
| `test_mode=run` (full tests) | L0 ~17m → L1 ~15m → L2 ~16m → L3 ~27m → L4 ~22m → L5 ~8m → L6 ~12m → L7 ~6m → L8 ~18m → L9 ~13m | **~150 min** |

Wall-clock time is determined by the critical path (longest chain of sequential
levels), not the sum. Within each level, only the slowest parallel job matters.

Step timeouts per level:

| Level | Timeout | Bottleneck |
|-------|---------|-----------|
| L0 prepare | 5m | 3 platform base modules |
| L0 slices | 45m | Slice C (53 modules, 25 @QuarkusTest) |
| L0 convergence | 20m | Full platform rebuild |
| L1 | 25m | connectors (~15m with tests) |
| L2 | 25m | neocortex (~16m) |
| L3 | 35m | work (~27m) |
| L4 | 45m | engine (~22m, ~1500 tests) |
| L5 | 25m | claudony+agency bundle |
| L6 | 25m | mosaic (~12m) |
| L7 | 25m | drafthouse |
| L8 | 30m | iot (~18m) or quarkmind (~16m) |
| L9 | 30m | fsitrading (~13m) |

Setup overhead per job (checkout, cache restore, artifact download) adds ~1-2
minutes per job transition.

## CI Dashboard

### Architecture

A live build progress dashboard at **https://casehub.org/casehub-all/**
(GitHub Pages from `docs/`). Build data is stored in a Cloudflare Worker + KV
store — no git history pollution, instant reads, server-side ownership
enforcement.

| Component | Location |
|-----------|----------|
| Dashboard HTML | `docs/index.html` (single-file HTML+CSS+JS, no frameworks) |
| Data store | Cloudflare Worker + KV at `casehub-ci-dashboard.casehub.workers.dev` |
| Worker source | `docs/worker/src/index.js` |
| Update script | `docs/update-progress.sh` (phase-file, phase, finish commands) |
| Maven wrapper | `docs/run-with-progress.sh` (start/end updates per phase) |
| Auth | `CI_DASHBOARD_TOKEN` org secret (write token for Worker) |

### How Updates Flow

1. **Init (before checkout):** The prepare job's first step creates the
   `current` object in KV with all phases set to `pending`. This uses
   `X-Init: true` to establish run_id ownership.

2. **Phase start (each job):** Parallel phases write to their own KV keys
   (`phase-{runId}-{name}`) to avoid contention. Sequential phases update
   `progress` directly.

3. **During build:** `run-with-progress.sh` wraps each Maven build. At
   completion, it calls `update-progress.sh` to write stats (modules, tests,
   warnings, errors) parsed from Maven output.

4. **Finish:** The finalize job merges all phase-file KV entries into the
   main `progress` KV key, archives the run to `history`, and cleans up
   the phase files.

### Server-Side Run ID Ownership

A key design constraint: when a new build starts, it cancels the previous
build, but the cancelled build's in-flight curl requests can arrive *after*
the new build's init — corrupting the new build's dashboard data.

The Cloudflare Worker enforces ownership:
- Init writes (`X-Init: true`) always succeed and establish a new owner
- All other writes include `X-Run-Id` and are rejected with HTTP 409 if
  the run_id doesn't match the current owner
- This prevents cancelled/stale builds from corrupting current build data

### Parallel-Safe Phase Updates

Parallel jobs within a level (e.g., the 3 L1 jobs) can't safely
read-modify-write the shared `progress` KV key. Instead:

- **Parallel phases** use `run-with-progress.sh --parallel` which calls
  `update-progress.sh phase-file` — writes to a separate KV key per phase
  (`phase-{runId}-{name}`)
- **Sequential phases** (work, engine) use `update-progress.sh phase` which
  updates `progress` directly
- **Finish command** merges all phase files into `progress` before archiving

### Stats Captured Per Phase

All stats are parsed from Maven's own output — reactor summary, surefire
aggregate lines, and error messages. Each parser section is wrapped in
`try/except` so a parse failure in one stat never blocks the others.

| Stat | Source | Example |
|------|--------|---------|
| `modules` | Reactor summary `SUCCESS/FAILURE/SKIPPED` lines | `87` |
| `tests_run` | Surefire `Tests run: N` | `1514` |
| `tests_failed` | Surefire `Failures: N, Errors: N` | `2` |
| `tests_skipped` | Surefire `Skipped: N` | `3` |
| `tests_flaky` | Surefire `Flakes: N` (failed then passed on retry) | `1` |
| `timeouts` | `TimeoutException` / `forkedProcessTimeout` grep | `0` |
| `compilation_warnings` | `[WARNING]` lines (noise filtered) | `45` |
| `slowest_modules` | Top 5 by time from reactor summary | `[{name, seconds}]` |
| `slowest_tests` | Top 5 by time from surefire output | `[{test, seconds}]` |
| `self_healing` | Corrupt JAR/Jandex deletions + retries | `{corrupt_jars: 3, retries: 1}` |
| `failed_modules` | Reactor `FAILURE` lines | `["casehub-engine"]` |
| `test_errors` | Top 5 test failures with error message (120 char) | `[{test, error}]` |
| `maven_time_s` | `Total time:` line | `2700` |

### Data Model

```json
{
  "current": {
    "run_id": 41018829651,
    "run_url": "https://github.com/casehubio/casehub-all/actions/runs/...",
    "commit": "abc1234",
    "started": "2026-10-30T14:00:00Z",
    "resume_from": null,
    "retry_count": 0,
    "phases": [
      {"name": "Prepare + Platform base", "status": "success", "duration_s": 120,
       "modules": 3, "tests_run": 16},
      {"name": "Platform A", "status": "in_progress", "started": "...",
       "modules": 22, "tests_run": 150},
      {"name": "work", "status": "pending"},
      ...
    ]
  },
  "history": [
    {"run_id": 41012076639, "conclusion": "success", "duration_s": 5400,
     "total_tests": 4500, "total_modules": 247, "phases": [...]},
    ...
  ]
}
```

History is capped at 200 entries. Entries older than 90 days are trimmed on
every write.

### Dashboard UI

**Pipeline visualization:**
- Parallel phases at the same level shown side-by-side (CSS columns)
- Sequential levels stacked vertically
- Each phase box: status colour, duration/elapsed, modules, tests, flaky
  count, timeouts, self-healing indicator
- Pulsing animation on `in_progress` phases, red styling on failures
- Event log with timestamped fetch events for debugging

**Phase detail (click to expand):**
- Top 5 slowest modules with times
- Top 5 slowest tests with times
- Compilation warning counts
- Self-healing breakdown (corrupt JARs, Jandex, retries)
- Failed module names and test error messages

**Failure emphasis:**
- Red card border and background tint on failed builds
- FAILED badge on failed phases
- "Last build FAILED" banner when no build is in progress and the most
  recent history entry is a failure

**History (expandable rows):**
- Summary line: status dot, run ID, commit, duration, tests, modules
- Click expands to full pipeline view + detail tables for that run

**Trend charts (inline SVG, no libraries):**
- Build duration over recent runs (green/red dots for pass/fail)
- Test count trends
- Self-healing frequency

### Setup

1. **GitHub Pages:** Settings → Pages → Source: "Deploy from a branch" →
   Branch: `main`, folder: `/docs`
2. **Cloudflare Worker:** Deploy `docs/worker/` via `npx wrangler deploy`
   from the worker directory. Set WRITE_TOKEN secret via `npx wrangler
   secret put WRITE_TOKEN`.
3. **CI_DASHBOARD_TOKEN:** Add the write token as an org secret
   `CI_DASHBOARD_TOKEN` in GitHub Settings → Secrets → Actions.

## Surefire Configuration

### Forked Process Timeout

`forkedProcessTimeoutInSeconds=300` (casehub-parent POM) kills any hung
Surefire forked JVM after 5 minutes. This prevents a single stuck test from
consuming the entire CI job budget (previously caused 45-minute job timeouts).

All repos that extend casehub-parent inherit this setting automatically.
Platform also declares it in its own `<pluginManagement>` as belt-and-suspenders
(platform extends casehub-parent since the BOM alignment work).

The timeout applies per fork, not per test. If a fork has multiple tests,
the 5-minute budget covers all of them. Individual tests should use JUnit
`@Timeout` for per-test safety nets.

### Surefire Version Alignment

All repos use Surefire **3.5.5** (set in `casehub-parent`, inherited by all
repos that extend it). Surefire 3.5.5 has stricter module-path validation
that detects split packages (classes in the same Java package across
different JARs).

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
`target/classes/META-INF/jandex.idx` files as artifacts. The convergence job
downloads them into the `platform/` workspace before running the full rebuild.
Both `.m2` artifacts (JARs) and Jandex indexes (filesystem) must be passed.

**Escape hatch:** `-Dcasehub.generators.skip=true`

## Runner Constraints (GitHub Actions Free Tier)

GitHub Actions free-tier runners have **2 CPUs** and 7 GB RAM. Each job in the
workflow gets its own runner.

| Constraint | Impact | Mitigation |
|-----------|--------|------------|
| 2 CPUs per runner | `-T 1C` means 2 threads per job. | 10-level pipeline with parallel jobs at each level gives up to 5 runners simultaneously (L8 has 5 parallel jobs). |
| 7 GB RAM per runner | Each job has its own 7 GB — no memory sharing between jobs. | `MAVEN_OPTS='-Xmx4g'` per job. |
| No shared filesystem | Each job starts with a clean workspace. | Artifacts pass `.m2` JARs and Jandex indexes between jobs via `actions/upload-artifact` / `actions/download-artifact`. |
| Cache is per-repo, not per-job | All jobs restore the same `.m2` cache (snapshot from prior run). | Each job purges `io.casehub` from `.m2` before downloading fresh artifacts. |

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
| `test_mode` | choice | `skip` | `skip` = compile tests but don't run (`-DskipTests`); `compile` = compile tests only (`-DskipTests`); `run` = compile + execute tests. Note: uses `-DskipTests` (not `-Dmaven.test.skip=true`) so test-jar artifacts are still produced for downstream deps. |
| `quarkus_build` | boolean | `false` | When true, runs Quarkus augmentation (produces runner JARs). Leave false — the deadlock makes this unsafe with `-T 1C`. |
| `enforcer` | boolean | `false` | When true, runs Maven enforcer plugin. |
| `maven_args` | string | (empty) | Escape hatch for additional Maven flags. Passed via `GITHUB_ENV` to prevent shell injection. |
| `resume_from` | choice | (empty) | Resume from: `L1`, `L2`, `L3`, `L5`, or `L8`. Skips prepare + platform + earlier levels; downstream levels use cached `.m2`. |
| `submodule_update` | choice | `pinned` | `pinned` = use committed SHAs (deterministic); `all` = pull latest main for all repos; `list` = pull latest only for repos named in `submodule_list`. |
| `submodule_list` | string | (empty) | Comma-separated repo names to update (only when `submodule_update=list`). E.g. `engine,aml,claudony`. |
| `retry_count` | number | `0` | Auto-retry counter. Set automatically by the retry step — do not set manually. Max 2 retries. |

```bash
# Default (compile-only, pinned SHAs, no tests)
gh workflow run mvn-all.yml --repo casehubio/casehub-all

# Full test run with pinned SHAs (recommended for verification)
gh workflow run mvn-all.yml --repo casehubio/casehub-all -f test_mode=run

# Pull latest main for all repos, then full test run
gh workflow run mvn-all.yml --repo casehubio/casehub-all -f test_mode=run -f submodule_update=all

# Pull latest for specific repos only (after fixing a repo)
gh workflow run mvn-all.yml --repo casehubio/casehub-all -f test_mode=run -f submodule_update=list -f submodule_list="engine,aml"

# Resume from L3 after fixing a work test (uses cached L0/L1/L2 artifacts)
gh workflow run mvn-all.yml --repo casehubio/casehub-all -f test_mode=run -f resume_from=L3
```

`MVN_COMMON` is assembled dynamically from inputs in the "Configure Maven
flags" step. `maven_args` is set as the `MAVEN_ARGS` environment variable
(not interpolated directly) and logged via `::notice::`.

## Known Limitations

### resume_from is fragile
See "Resuming from a Level" above. Only safe when earlier levels haven't
changed since the cached run. The Maven `.m2` cache is keyed on
`parent/pom.xml` + `platform/pom.xml` hash — it does NOT invalidate when
individual repo SNAPSHOTs are republished. Stale cache artifacts can mask
or fabricate failures. Always run a full build (no `resume_from`) for
definitive verification.

The auto-retry mechanism uses `resume_from` to skip already-green levels.
This is safe for transient failures (corrupted downloads, runner timeouts)
but will retry deterministic failures too — the 2-retry cap prevents
infinite loops.

### Stale cache between jobs
Each job restores a Maven cache from a previous run, which may contain stale
or corrupted casehub snapshot JARs. A purge step (`rm -rf
~/.m2/repository/io/casehub`) runs in every job before downloading fresh
artifacts. Levels that build from source (convergence) regenerate everything
fresh. This defense-in-depth approach handles both stale cache entries and
artifact download corruption.

### Pointer advance race
The finalize job advances submodule pointers and pushes on success. If two runs
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

### Reactor-only CDI issues
Some modules have CDI deployment problems that only manifest in the casehub-all
reactor, not in standalone CI. The reactor classpath can differ from standalone
because Maven reactor resolution resolves from `target/` instead of `.m2`,
and `-fae` continues past failures that change the effective classpath.

**Claudony (resolved):** Previously had 28 unsatisfied CDI dependencies.
Fixed by narrowing the `quarkus.arc.exclude-types` glob from
`io.casehub.qhorus.runtime.**` to per-sub-package globs. The `identity`
sub-package is intentionally kept alive so `TenancyContextFilter`
(`@Provider @PreMatching`) remains a CDI bean — RESTEasy Reactive discovers
`@Provider` classes from Jandex independently of CDI exclusion, and without
CDI management the filter's `@Inject` fields are null.

### Mosaic TypeScript in reactor context
Mosaic's TypeScript build resolves `@casehubio/*` packages via portal links
to `.casehub-packages/` (unpacked from Maven artifacts). The type resolution
can differ from standalone Yarn workspace resolution, causing TS errors that
don't reproduce locally. Current mitigations:
- `declaration: false` in agent-viewer's tsconfig.build.json (TS4094)
- `never` exhaustiveness checks replaced with fallback defaults

## Port Management

All test infrastructure uses **dynamic ports** (`0` or `dynamicPort()`).
No hardcoded ports remain in test code. This eliminates port-conflict
failures under `-T 1C` parallel builds.

Previously hardcoded ports that were cleaned up:
- neocortex: WireMock 39200/39201 → `QuarkusTestResourceLifecycleManager`
- ledger: WireMock 8098/8099 → `@InjectWireMock` with dynamic port
- connectors: IRC 16667 → `ServerSocket(0)` with `SO_REUSEADDR`
- iot: Quarkus 8082 → `quarkus.http.test-port=0` + `DynamicTestPortResource`
- quarkmind: Quarkus http.port → `-Dquarkus.http.port=0` + stdout parser

When adding new test infrastructure, always bind to port `0` and read
the assigned port from the server/resource.

## Operations

### Fixing a Red Repo

1. Check idle state: `git -C ~/claude/casehub/REPO rev-parse --abbrev-ref HEAD`
2. If not on main or dirty: note branch, stash, switch to main.
3. Reproduce locally: `mvn -f ~/claude/casehub/REPO/pom.xml verify`
4. Fix, commit, push to origin + fork.
5. Restore original branch.

### Adding a New Repo

1. Add to `parent/build/modules-core.csv` or `modules-applications.csv`
2. Run `python3 parent/build/sync-deps.py` to validate
3. Add as submodule in casehub-all
4. Add to the appropriate level in `mvn-all.yml` (determine level from dependencies)
5. Add the phase name to the `init` step's phases list and the `finish` command's
   phase cleanup list in `update-progress.sh`
6. Run `python3 scripts/reconcile-submodules.py` in casehub-all

## File Map

```
parent/
  pom.xml                          — casehub-parent BOM (single org parent for all repos)
  build/
    modules-core.csv              — core repo dependency declarations (generated by sync-deps.py)
    modules-applications.csv      — app repo dependency declarations (generated by sync-deps.py)
    sync-deps.py                  — regenerate CSVs from actual POM dependencies
  aggregator-g1-core-libs.xml     — legacy G1 aggregator (not used by current pipeline)
  aggregator-g2-domain.xml        — legacy G2 aggregator
  aggregator-g3-middleware.xml    — legacy G3 aggregator
  aggregator-g4-extensions.xml    — legacy G4 aggregator
  aggregator-g5-apps-independent.xml — legacy G5 aggregator
  aggregator-g6-apps-chain.xml    — legacy G6 aggregator
  aggregator-foundation.xml       — foundation repos aggregator
  aggregator-apps.xml             — app repos aggregator

platform/
  aggregator-base.xml             — base slice (platform-api, platform-core, playbook-catalog)
  aggregator-slice-a.xml          — Agent/AI (44 modules)
  aggregator-slice-b.xml          — Identity/Auth (57 modules)
  aggregator-slice-c.xml          — Events/Comms (53 modules)
  aggregator-slice-d.xml          — YAML/Tooling (15 modules)
  aggregator-convergence.xml      — historical — convergence now uses platform/pom.xml directly

casehub-all/
  .github/workflows/mvn-all.yml   — 10-level parallel pipeline (15+ jobs, parameterised)
  docs/
    index.html                    — CI dashboard (single-file HTML+CSS+JS, no frameworks)
    update-progress.sh            — Cloudflare Worker updater (phase-file, phase, finish commands)
    run-with-progress.sh          — Maven wrapper with start/end dashboard updates
    check-cycles.py               — dependency graph cycle detection (Kahn's algorithm)
    worker/
      src/index.js                — Cloudflare Worker (KV store with run_id ownership)
      wrangler.toml               — Worker configuration (KV namespace binding)
  scripts/
    reconcile-submodules.py       — syncs submodules from CSVs
```
