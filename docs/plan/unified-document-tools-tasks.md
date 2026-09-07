# Unified Document Tools Task Tracker

Last updated: 2026-09-07

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
| A5  | Define the scope of the four v1 backend-facing Actions.             | DONE   |
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
| D2  | Define strict argument models for the four v1 Actions.                      | DONE    |
| D3  | Define strict result models for the four v1 Actions.                        | DONE    |
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
| E1  | Finalize bound-source write-back mode.                                         | PARTIAL |
| E2  | Finalize unbound local-document publication mode.                              | PARTIAL |
| E3  | Return and persist provider bindings after first publication.                  | PARTIAL |
| E4  | Treat explicit cross-provider publication as unbind-and-copy.                  | PARTIAL |
| E5  | Remove the three default-Feishu arguments from Writer Workflow functions.      | DONE    |
| E6  | Generalize or clearly deprecate `/api/writer/documents:sync`.                  | PARTIAL |
| E7  | Audit and remove provider-specific branches from `document_tools`.             | PARTIAL |
| E8  | Add the structured `PROVIDER_CAPABILITY_UNSUPPORTED` error.                    | TODO    |
| E9  | Prevent provider switching or document creation after failed bound write-back. | TODO    |
| E10 | Prevent automatic retry after ambiguous external write outcomes.               | TODO    |




## L. LazyLLM provider-contract follow-up

This is a small interface hardening task, not part of the LazyMind structural
refactoring. It can proceed in parallel and must be complete before final backend
handoff.


| ID  | Task                                                                          | Status   |
| --- | ----------------------------------------------------------------------------- | -------- |
| L1  | Add the `WriterProviderCapabilities` data model.                              | TODO     |
| L2  | Make optional provider capabilities unsupported by default.                   | TODO     |
| L3  | Declare the tested Feishu and Notion capabilities.                            | TODO     |
| L4  | Declare GitHub and WeChat capabilities after their provider PRs stabilize.    | TODO     |
| L5  | Remove the default Feishu adapter from `WriterResourceTools.create_document`. | TODO     |
| L6  | Require revision checks for safe Patch-by-replace implementations.            | TODO     |




## T. Compatibility and acceptance tests


| ID  | Task                                                                       | Status  |
| --- | -------------------------------------------------------------------------- | ------- |
| T1  | Verify legacy `writer.py` imports.                                         | DONE |
| T2  | Snapshot all 45 Capability APIs and their owners.                          | DONE |
| T3  | Snapshot the unchanged 36 Chat Agent tools.                                | DONE |
| T4  | Verify class names and registered tool names.                              | DONE |
| T5  | Add pre-refactoring MD/LMD golden fixtures.                                | PARTIAL |
| T6  | Test flat Markdown zero-to-one writing.                                    | TODO    |
| T7  | Test sectioned Markdown zero-to-one writing.                               | TODO    |
| T8  | Test outlining and subproblem execution.                                   | TODO    |
| T9  | Regress Markdown and IR draft streaming.                                   | PARTIAL |
| T10 | Regress checkpointing and failure recovery.                                | PARTIAL |
| T11 | Test all Action argument and result contracts.                             | DONE    |
| T12 | Test built-in resolution and publication validation.                       | DONE    |
| T13 | Verify rewrite preview/execute content identity.                           | DONE    |
| T14 | Verify preview handlers have no external side effects.                     | DONE    |
| T15 | Test Feishu fake-provider first publication and binding.                   | TODO    |
| T16 | Test Notion fake-provider first publication and binding.                   | TODO    |
| T17 | Test bound write-back and revision conflicts for both providers.           | TODO    |
| T18 | Test missing authorization, denied permission, and unsupported capability. | TODO    |
| T19 | Regress academic, bid, and product Writer bridges.                         | PARTIAL |
| T20 | Run existing backend Writer and Artifact Action Go tests.                  | DONE    |
| T21 | Run final Python compilation, formatting, and `git diff --check`.          | TODO    |
| T22 | Complete and record a real Feishu smoke test.                              | TODO    |
| T23 | Complete and record a real Notion smoke test.                              | TODO    |


Previously executed narrow compatibility checks do not satisfy this final test
gate. No final handoff test suite has run yet.

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
| I7  | Run final Feishu/Notion/GitHub/WeChat joint regression.                               | TODO     |
| I8  | Integrate Obsidian through the shared interface when its work is available.           | EXTERNAL |




## Current summary


| Area                               | Progress summary                                      | Current state |
| ---------------------------------- | ----------------------------------------------------- | ------------- |
| Approved design                    | 10/10 design and scaffold items done                  | Complete      |
| Physical capability split          | 15/15 items done                                      | Complete      |
| Writer Workflow thinning           | 9/9 items done                                        | Complete      |
| Shared Artifact Actions            | 16/16 Action implementation items done                | Complete      |
| Provider-neutral behavior          | 1 done, 6 partial, 3 not started                      | In progress   |
| LazyLLM provider contract          | 6 not started                                         | Not started   |
| Compatibility and acceptance tests | 9 done, 4 partial, 10 not started                     | In progress   |
| Backend handoff                    | 1 done, 2 partial, 7 not started                      | Early stage   |
| Parallel knowledge-source work     | 6 integration items done, 1 final test, 1 external   | Parallel      |


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

- Provider binding lifecycle, cross-provider copy semantics, structured
  capability errors, ambiguous-write handling, and conflict tests are not done.
- GitHub Markdown handling and WeChat cover preparation still appear as
  provider-specific branches in `document_tools`; E7 remains partial until
  these differences move behind the LazyLLM provider capability contract.
- The LazyLLM provider-capability contract has not started.
- The backend handoff package and final acceptance suite are incomplete.




## Latest verification

- All four v1 Actions now have immutable versioned specs, strict phase-specific
  argument/result contracts, built-in resolution, and validated invocation.
- Writer `rewrite_selection.execute.v1` reads the exact staged preview candidate,
  verifies both source and candidate hashes, and performs no second model call.
- The Writer manifest uses explicit built-in references for rewrite, render,
  save, and sync. Backend Core publish diagnostics reject unknown versions,
  mismatched Action names, and unsupported phases while leaving ordinary pinned
  package-tool names unchanged.
- 53 focused Python Action, document-tools, Workflow-adapter, and Writer stream
  tests pass. Existing Backend Core Writer and Artifact Action tests pass across
  the root, `algo`, `chat`, and `workflow` packages, including the new
  publication-diagnostic cases.
- An additional full Backend Core Workflow package run compiled and began
  testing but could not complete in the restricted sandbox because an unrelated
  `httptest` case cannot bind a local listener. The relevant Writer and Artifact
  Action cases were rerun directly (with local-listener permission where needed)
  and pass.
- The 45-method Capability API snapshot, unchanged 36-tool Toolkit exposure,
  legacy imports, conversions, provider synchronization, and Action registry
  tests pass.
- 27 focused `document_tools` and WeChat/GitHub integration tests pass.
- 42 focused document-tools, Workflow smoke, and Writer Workflow tests pass without a dependency
  shim, including the new adapter-boundary test.
- `git diff --check` and focused Python compilation pass.
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
- All 43 existing `writer_*` Python entry-point names and signatures are
  unchanged relative to the C-stage base checkpoint.
- The focused MD/LMD test now asserts the exact established envelope data and
  rendered Markdown instead of checking only for substrings.
- The broader local suite remains unavailable until the real optional Runtime/RAG
  dependencies such as `rapidfuzz` and the LazyLLM RAG dependency group are
  installed. Product bridge tests also require their hermetic subagent-tool
  stub to be restored independently of this refactoring.

## Recommended execution order

1. Complete E1-E10 and L1-L6: provider binding lifecycle, provider capabilities,
   structured errors, conflict rules, and ambiguous-write behavior.
2. Complete T5-T23, then prepare H2-H10 for backend handoff.
3. Merge whichever provider integrations pass their own acceptance gates, adapt
   the later side to the shared interface, and run the final joint regression.

The immediate next task is the provider-neutral behavior and LazyLLM provider
contract work in stages E and L, not further Writer Workflow migration.
