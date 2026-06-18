# casehub-all

Read-only meta-repo. All CaseHub repos are git submodules — clone this once, point Claude.ai at it, and navigate the full ecosystem without switching repos.

## What this is for

- Asking Claude.ai cross-repo questions ("which module owns X?", "show me all implementations of SPI Y")
- Filing issues in any peer repo via `gh issue create --repo casehubio/<repo>`
- Reading code and docs across the whole platform in one context window

## What this is NOT for

Making commits. Edit code in the individual repos. This repo only tracks submodule pointers — commits here just update which ref each submodule points to.

## Sync to latest

```bash
git pull && git submodule update --remote --merge
```

Run this at the start of each Claude.ai session to get the latest main from every repo.

## Submodule map

| Directory | GitHub repo | Role |
|-----------|-------------|------|
| `platform/` | casehubio/platform | Zero-dep foundational SPIs — identity, preferences, memory, agents |
| `ledger/` | casehubio/ledger | Immutable tamper-evident audit ledger + trust scoring |
| `work/` | casehubio/work | Human task lifecycle — WorkItem inbox, SLA, delegation |
| `qhorus/` | casehubio/qhorus | Agent communication mesh — speech acts, commitments, channels |
| `connectors/` | casehubio/connectors | Outbound/inbound message connectors (Slack, Teams, email) |
| `iot/` | casehubio/iot | Typed IoT device abstraction — Matter-aligned hierarchy, HA/OpenHAB providers |
| `eidos/` | casehubio/eidos | Structured agent identity — descriptors, discovery, system prompt generation |
| `neural-text/` | casehubio/neural-text | ONNX inference + LangChain4j RAG integration |
| `desiredstate/` | casehubio/casehub-desiredstate | Generic desired-state runtime — reconciliation loop, fault policy |
| `ras/` | casehubio/casehub-ras | Reticular Activating System — situational awareness, reactive case creation |
| `ops/` | casehubio/casehub-ops | CaseHub deployment desired-state — deployment YAML compiler, infra, compliance |
| `workers/` | casehubio/workers | HTTP, Camel, GitHub Actions worker dispatch adapters |
| `engine/` | casehubio/engine | Hybrid choreography+blackboard orchestration engine |
| `claudony/` | casehubio/claudony | Remote Claude CLI sessions + unified ecosystem dashboard |
| `openclaw/` | casehubio/openclaw | CaseHub × OpenClaw integration |
| `devtown/` | casehubio/devtown | PR review coordination app (tutorial + showcase) |
| `aml/` | casehubio/aml | AML investigation app (tutorial + showcase) |
| `clinical/` | casehubio/clinical | Clinical trial coordination app (tutorial + showcase) |
| `drafthouse/` | casehubio/drafthouse | Document review app |
| `life/` | casehubio/life | Household automation app |
| `quarkmind/` | casehubio/quarkmind | StarCraft II game AI — live proving ground |
| `parent/` | casehubio/parent | Root POM + PLATFORM.md, ADRs, repo deep-dives |

## Filing issues

```bash
gh issue create --repo casehubio/engine --title "..." --body "..."
gh issue create --repo casehubio/platform --title "..." --body "..."
# etc.
```

## Making code changes

Switch Claude.ai to the individual repo for write sessions. Or edit directly inside the submodule directory — changes there are commits to the origin repo, not this one.
