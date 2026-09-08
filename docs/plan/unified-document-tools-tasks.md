# Unified Document Tools Task Tracker

Last updated: 2026-09-08

Branch: `dev-plugin`

Base checkpoint: `36cedb2d` (`fix(writer): complete document tool compatibility`)

This file tracks implementation progress for the decisions recorded in
`[unified-document-tools.md](./unified-document-tools.md)`. Update an item only
after its implementation and relevant verification are complete.

## Status legend

- DONE: implementation or decision is complete.
- PARTIAL: scaffolding or part of the behavior exists, but the acceptance
criteria are not complete.
- TODO: work has not started.
- EXTERNAL: owned by or blocked on another parallel PR/team.



## A. Design and scaffolding


| ID  | Task                                                                | Status |
| --- | ------------------------------------------------------------------- | ------ |
| A1  | Define Capability, Toolkit, and Action API layers.                  | DONE   |
| A2  | Fix the public scope at 45 Capability APIs and 36 Chat Agent tools. | DONE   |
| A3  | Preserve the pre-refactoring MD/LMD conversion behavior.            | DONE   |
| A4  | Define legacy `writer.py` import compatibility.                     | DONE   |
| A5  | Define the scope of the v1 backend-facing Actions.                  | DONE   |
| A6  | Define two-mode provider synchronization.                           | DONE   |
| A7  | Define the provider capability contract.                            | DONE   |
| A8  | Define test and backend-handoff acceptance criteria.                | DONE   |
| A9  | Record the approved architecture decisions.                         | DONE   |
| A10 | Create the eight-module `document_tools` package scaffold.          | DONE   |




## B. Physical capability split


| ID  | Task                                                                                             | Status  |
| --- | ------------------------------------------------------------------------------------------------ | ------- |
| B1  | Move schemas, JSON helpers, Artifact files, and path handling into `artifacts.py`.               | DONE |
| B2  | Move MD/LMD reading, writing, conversion, and Markdown rendering into `artifacts.py`.            | DONE |
| B3  | Move the existing cross-reference target aggregation and binding into `references.py`.           | DONE |
| B4  | Move revision-task construction, target location, and modification planning into `revision.py`.  | DONE |
| B5  | Move Patch generation, validation, and application into `revision.py`.                           | DONE |
| B6  | Move provider locator and target resolution into `resources.py`.                                 | DONE |
| B7  | Move load, create, replace, append, publish, and synchronization into `resources.py`.            | DONE |
| B8  | Move writing-task, resource profiling, and writing-context implementation into `writing.py`.     | DONE |
| B9  | Move outlining, subproblem execution, drafting, and final-document generation into `writing.py`. | DONE |
| B10 | Move media collection, visual planning, and streaming generation into `writing.py`.              | DONE |
| B11 | Move `DraftMarkdownStreamEventEmitter` into `writing.py`.                                        | DONE |
| B12 | Restrict each concrete Toolkit to its own capability set.                                        | DONE |
| B13 | Reduce `toolkits.py` to composition and Chat Agent exposure.                                     | DONE |
| B14 | Reduce `WriterToolkitBase` to a legacy compatibility aggregate.                                  | DONE |
| B15 | Keep the nine Workflow-only capabilities available without exposing them to Chat Agents.          | DONE |


Implementation order:

```text
artifacts/references
→ revision
→ resources
→ writing
→ toolkit composition
→ WriterToolkitBase compatibility aggregate
```



## C. Writer Workflow thinning


| ID  | Task                                                                         | Status |
| --- | ---------------------------------------------------------------------------- | ------ |
| C1  | Consolidate Writer Workflow Python entry points in `scripts/tools.py`.        | DONE   |
| C2  | Keep checkpoint, fingerprint, and recovery behavior Workflow-private.        | DONE   |
| C3  | Keep private step, slot, and workspace state handling in the Workflow.       | DONE   |
| C4  | Move reusable writing business logic into `document_tools`.                  | DONE   |
| C5  | Move reusable revision and provider logic into `document_tools`.             | DONE   |
| C6  | Preserve all `writer_*` entry-point names and signatures referenced by YAML. | DONE    |
| C7  | Reduce non-orchestration `writer_*` functions to adaptation and forwarding.  | DONE   |
| C8  | Preserve current progress and draft-stream event behavior.                   | DONE   |
| C9  | Remove direct LazyLLM Writer implementation imports from Workflow adapters.  | DONE   |


Target structure:

```text
workflows/writer-workflow/scripts/
└── tools.py       # YAML adapters plus private state/orchestration only
```



## D. Shared Artifact Actions


| ID  | Task                                                                        | Status  |
| --- | --------------------------------------------------------------------------- | ------- |
| D1  | Define `DocumentActionSpec`.                                                | DONE    |
| D2  | Define strict argument models for the v1 Actions.                           | DONE    |
| D3  | Define strict result models for the v1 Actions.                             | DONE    |
| D4  | Implement `rewrite_selection.preview.v1`.                                   | DONE    |
| D5  | Implement deterministic `rewrite_selection.execute.v1`.                     | DONE    |
| D6  | Implement `render_document.v1`.                                             | DONE    |
| D7  | Implement `save_document.v1`.                                               | DONE    |
| D8  | Implement `sync_document.v1`.                                               | DONE    |
| D9  | Reject duplicate registration and runtime replacement.                      | DONE    |
| D10 | Resolve `builtin:document.<action>.v1` references.                          | DONE    |
| D11 | Preserve ordinary pinned Workflow package-tool resolution.                  | DONE    |
| D12 | Validate built-in references, versions, and phases at Workflow publication. | DONE    |
| D13 | Reject Actions that are not declared in `workflow.yaml`.                    | DONE    |
| D14 | Enforce side-effect-free preview handlers.                                  | DONE    |
| D15 | Map invalid input, conflict, provider, and internal errors.                 | DONE    |
| D16 | Update the Writer Workflow manifest to use explicit built-in references.    | DONE    |


The Writer Workflow now declares both rewrite phases against the same versioned
built-in contract. Preview may generate and stage a candidate; execute verifies
and returns that exact candidate without another model call.

## E. Provider-neutral behavior


| ID  | Task                                                                           | Status  |
| --- | ------------------------------------------------------------------------------ | ------- |
| E1  | Finalize bound-source write-back mode.                                         | DONE    |
| E2  | Finalize unbound local-document publication mode.                              | DONE    |
| E3  | Return and persist provider bindings after first publication.                  | DONE    |
| E4  | Treat explicit cross-provider publication as unbind-and-copy.                  | DONE    |
| E5  | Remove the three default-Feishu arguments from Writer Workflow functions.      | DONE    |
| E6  | Generalize or clearly deprecate `/api/writer/documents:sync`.                  | DONE    |
| E7  | Audit and remove provider-specific branches from `document_tools`.             | DONE    |
| E8  | Add the structured `PROVIDER_CAPABILITY_UNSUPPORTED` error.                    | DONE    |
| E9  | Prevent provider switching or document creation after failed bound write-back. | DONE    |
| E10 | Prevent automatic retry after ambiguous external write outcomes.               | DONE    |
| E11 | Expose provider-format conversion as a standalone Capability and Action.        | DONE    |
| E12 | Make write-back explicitly consume the converted provider artifact once.       | DONE    |
| E13 | Support late-bound repeated provider export, copy, and write-back choices.      | DONE    |

Bound Writer IR now requires its synchronized source baseline and exact target
binding. Unbound publication requires an explicit adapter or target, while an
explicit different provider detaches document/block identities, provider
payloads, and remote revision state before copying. First publication returns
both the provider-confirmed document and normalized target; Backend Core stores
the target for Markdown even when no earlier target revision exists. Markdown
publication retains its established IR conversion and media-reference behavior.
Provider conversion, write-back, editor adaptation, capability checks, revision
conflicts, and ambiguous-write classification live in the LazyLLM provider
contract. LazyMind exposes pure conversion and external write-back as separate
Capabilities and Actions. Backend publication invokes them in order, while
bound incremental editing keeps the distinct Patch synchronization path. The
old coupled replace/append Capability and Workflow adapters were removed.




## L. LazyLLM provider-contract follow-up

This is a small interface hardening task, not part of the LazyMind structural
refactoring. It can proceed in parallel and must be complete before final backend
handoff.


| ID  | Task                                                                          | Status   |
| --- | ----------------------------------------------------------------------------- | -------- |
| L1  | Add the `WriterProviderCapabilities` data model.                              | DONE     |
| L2  | Make optional provider capabilities unsupported by default.                   | DONE     |
| L3  | Declare the tested Feishu and Notion capabilities.                            | DONE     |
| L4  | Declare GitHub and WeChat capabilities after their provider PRs stabilize.    | DONE     |
| L5  | Remove the default Feishu adapter from `WriterResourceTools.create_document`. | DONE     |
| L6  | Require revision checks for safe Patch-by-replace implementations.            | DONE     |
| L7  | Define the shared `WriterProviderDocument` conversion result.                  | DONE     |
| L8  | Add mandatory `convert_document` and `write_document` provider interfaces.     | DONE     |
| L9  | Implement pure conversion and media materialization for all four providers.    | DONE     |
| L10 | Keep replace and append as compatibility compositions over the two stages.     | DONE     |




## T. Compatibility and acceptance tests


| ID  | Task                                                                       | Status  |
| --- | -------------------------------------------------------------------------- | ------- |
| T1  | Verify legacy `writer.py` imports.                                         | DONE |
| T2  | Snapshot all 45 Capability APIs and their owners.                          | DONE |
| T3  | Snapshot the unchanged 36 Chat Agent tools.                                | DONE |
| T4  | Verify class names and registered tool names.                              | DONE |
| T5  | Add pre-refactoring MD/LMD golden fixtures.                                | DONE    |
| T6  | Test flat Markdown zero-to-one writing.                                    | DONE    |
| T7  | Test sectioned Markdown zero-to-one writing.                               | DONE    |
| T8  | Test outlining and subproblem execution.                                   | DONE    |
| T9  | Regress Markdown and IR draft streaming.                                   | DONE    |
| T10 | Regress checkpointing and failure recovery.                                | DONE    |
| T11 | Test all Action argument and result contracts.                             | DONE    |
| T12 | Test built-in resolution and publication validation.                       | DONE    |
| T13 | Verify rewrite preview/execute content identity.                           | DONE    |
| T14 | Verify preview handlers have no external side effects.                     | DONE    |
| T15 | Test Feishu fake-provider first publication and binding.                   | DONE    |
| T16 | Test Notion fake-provider first publication and binding.                   | DONE    |
| T17 | Test bound write-back and revision conflicts for both providers.           | DONE    |
| T18 | Test missing authorization, denied permission, and unsupported capability. | DONE    |
| T19 | Regress academic, bid, and product Writer bridges.                         | DONE    |
| T20 | Run existing backend Writer and Artifact Action Go tests.                  | DONE    |
| T21 | Run final Python compilation, formatting, and `git diff --check`.          | DONE    |
| T22 | Complete and record a real Feishu smoke test.                              | EXTERNAL |
| T23 | Complete and record a real Notion smoke test.                              | EXTERNAL |


The complete local acceptance gate has run. The two real-account smoke tests
remain external because this checkout has no Feishu or Notion credentials and
no authorized target documents; no external document is created implicitly.

## H. Backend handoff package


| ID  | Task                                                     | Status  |
| --- | -------------------------------------------------------- | ------- |
| H1  | Architecture decision record.                            | DONE    |
| H2  | JSON Schemas for the four v1 Action contracts.           | TODO    |
| H3  | Representative success and failure payloads.             | TODO    |
| H4  | Versioned built-in Action registry listing.              | TODO    |
| H5  | Provider capability and binding lifecycle handoff guide. | PARTIAL |
| H6  | Error-code, HTTP-status, and retryability table.         | TODO    |
| H7  | Legacy/new compatibility matrix.                         | TODO    |
| H8  | Automated test report.                                   | TODO    |
| H9  | Feishu and Notion smoke-test records.                    | TODO    |
| H10 | Backend implementation checklist.                        | PARTIAL |


Backend-owned follow-up items include removing the unbound-document Feishu
default, requiring an explicit first-publication provider, persisting returned
bindings, forwarding structured Action/provider errors, retaining ownership of
authorization/concurrency/revision persistence, and avoiding dependencies on
algorithm-local paths or provider-specific writing branches.

## I. Parallel knowledge-source integration


| ID  | Task                                                                                  | Status   |
| --- | ------------------------------------------------------------------------------------- | -------- |
| I1  | Do not chase every intermediate GitHub/WeChat PR update from this branch.             | DONE     |
| I2  | Merge whichever implementation first passes its acceptance gate.                      | DONE     |
| I3  | Make the later-merging side adapt to the latest shared interface.                     | DONE     |
| I4  | Update the LazyLLM submodule SHA after provider PRs stabilize.                        | DONE     |
| I5  | Merge the latest `dev-plugin` provider integrations into the refactoring.             | DONE     |
| I6  | Adapt merged provider integrations to `document_tools`.                              | DONE     |
| I7  | Run final Feishu/Notion/GitHub/WeChat joint regression.                               | DONE     |
| I8  | Integrate Obsidian through the shared interface when its work is available.           | EXTERNAL |




## Current summary


| Area                               | Progress summary                                      | Current state |
| ---------------------------------- | ----------------------------------------------------- | ------------- |
| Approved design                    | 10/10 design and scaffold items done                  | Complete      |
| Physical capability split          | 15/15 items done                                      | Complete      |
| Writer Workflow thinning           | 9/9 items done                                        | Complete      |
| Shared Artifact Actions            | 16/16 Action implementation items done                | Complete      |
| Provider-neutral behavior          | 13/13 items done                                       | Complete      |
| LazyLLM provider contract          | 10/10 items done                                       | Complete      |
| Compatibility and acceptance tests | 21 local items done, 2 real-account items external    | Local complete |
| Backend handoff                    | 1 done, 2 partial, 7 not started                      | Early stage   |
| Parallel knowledge-source work     | 7 integration items done, 1 external                  | Local complete |


## Current implementation boundary

Completed in the working tree:

- All 43 pre-integration document capability methods remain physically owned by
  `artifacts.py`, `writing.py`, `revision.py`, and `resources.py`. GitHub and
  WeChat integration add two Workflow-only resource capabilities, for 45 total.
- `toolkits.py` now contains composition and compatibility only. The three
  concrete Toolkits retain the existing 36 Chat Agent tools, while the nine
  Workflow-only capabilities remain algorithm APIs.
- Legacy imports through `lazymind.chat.engine.tools.writer` remain available.
- Reusable short-writing, media, selection-revision, provider-locator, and
  synchronization logic has moved out of the Writer Workflow adapter.
- Writer Workflow entry-point names are unchanged. The only intentional
  signature-default changes remove the two remaining `adapter='feishu'`
  defaults from `writer_sync_document` and `writer_create_document`.
- MD/LMD conversion delegates to the existing LazyLLM Writer conversion rules.
- Writer Workflow request fingerprints, authoritative step inputs, checkpoint
  recovery, atomic persistence, and its four private step state machines remain
  together in `scripts/tools.py`; no Workflow-internal Python forwarding layer
  is retained.
- Stateless Workflow-facing document execution and Artifact I/O adaptation are
  owned by shared `document_tools/execution.py`, rather than being hidden in a
  Workflow-local helper module.
- Workflow adapters no longer import `lazyllm.tools.writer.*` implementation
  modules. Request parsing, structure policy, document normalization, local-copy
  binding cleanup, media acquisition/resolution, document assembly, numbering,
  and revision media finalization are delegated to shared `document_tools`
  capabilities.

Not yet complete:

- Real-account Feishu and Notion smoke tests require authorized accounts and
  explicit target documents.
- The backend handoff package remains incomplete.




## Latest verification

- LazyLLM providers directly implement the same `convert_document` and
  `write_document` methods. Conversion produces Feishu/Notion block JSON,
  WeChat HTML, or GitHub Markdown without external IO; write-back consumes the
  converted result and materializes provider media.
- Unsupported operations produce structured
  `PROVIDER_CAPABILITY_UNSUPPORTED` errors. Timeouts, connection loss, malformed
  write responses, and provider 5xx failures produce the non-retryable
  `PROVIDER_WRITE_OUTCOME_AMBIGUOUS` contract; deterministic validation failures
  pass through unchanged.
- 362 focused LazyLLM Writer/provider tests plus 12 subtests pass, covering all
  four providers, conversion/write separation, bound revision behavior,
  Markdown/LMD, streaming, revision, media, and short/sectioned writing.
- 160 focused LazyMind Action, document, Workflow, recovery, and academic, bid,
  and product bridge tests pass. Feishu and Notion fake-provider first
  publication now explicitly verifies create, single converted write, read-back,
  and returned document/block bindings.
- Backend Core `algo`, `chat`, and `workflow` package tests pass.
- LazyLLM providers now expose an immutable, explicit capability matrix.
  Feishu, Notion, GitHub, and WeChat declare only their implemented operations;
  unsupported operations fail before provider authorization or IO begins.
- `WriterResourceTools.create_document` requires an explicit adapter, and the
  WeChat Patch-by-replace path revalidates the remote draft revision before
  writing.
- All six v1 Actions now have immutable versioned specs, strict phase-specific
  argument/result contracts, built-in resolution, and validated invocation.
- Writer `rewrite_selection.execute.v1` reads the exact staged preview candidate,
  verifies both source and candidate hashes, and performs no second model call.
- The Writer manifest uses explicit built-in references for rewrite, render,
  save, and sync. Backend Core publish diagnostics reject unknown versions,
  mismatched Action names, and unsupported phases while leaving ordinary pinned
  package-tool names unchanged.
- Pre-refactoring Markdown input, normalized LMD envelope, and rendered Markdown
  are stored as golden fixtures and compared exactly after normalizing only the
  generated envelope metadata.
- The 45-method Capability API snapshot, with the resource publication pair
  changed from replace/append to convert/write, and unchanged 36-tool Toolkit exposure,
  legacy imports, conversions, provider synchronization, and Action registry
  tests pass.
- 27 focused `document_tools` and WeChat/GitHub integration tests pass.
- 42 focused document-tools, Workflow smoke, and Writer Workflow tests pass without a dependency
  shim, including the new adapter-boundary test.
- `make lint-python`, Go formatting, Workflow-scoped flake8, focused Python
  compilation, and `git diff --check` for both repositories pass. The migrated
  `document_tools` package and Workflow adapter use the repository's required
  single-quote style without a lint exception.
- Shared MD/LMD conversion, short-document planning/streaming, media search and
  filtering, cross-reference target binding, provider locator resolution,
  and provider-neutral synchronization now live under `document_tools`.
- The adapter-boundary test verifies that the 37 stateless `writer_*` execution
  entry points call shared `document_tools.execution`, while only the five
  Workflow-private control/state-machine entries retain orchestration and the
  structure classifier directly calls shared writing policy. `tools.py` imports
  no LazyLLM Writer implementation modules. Dedicated state tests cover
  authoritative bindings, checkpoint round trips, stale fingerprints, corrupt
  state, and atomic temporary-file cleanup.
- The Workflow publication entry points now expose `writer_convert_document`
  and `writer_write_document`; the old coupled replace/append adapters are gone.
- The focused MD/LMD test now asserts the exact established envelope data and
  rendered Markdown instead of checking only for substrings.
- Product bridge tests use a hermetic subagent-tool stub and validate the current
  chapter publication result without importing unrelated runtime dependencies.

## Recommended execution order

1. Prepare H2-H8 and H10 for backend handoff from the completed local acceptance
   evidence.
2. Run and record T22/T23 and H9 when explicit Feishu and Notion test accounts
   and targets are available.
3. Integrate Obsidian through the shared provider interface when its work is
   available.

The E-stage integration and local T-stage acceptance gate are complete; the next
local work is the backend handoff package.
