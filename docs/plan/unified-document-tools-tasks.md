# Unified Document Tools Task Tracker

Last updated: 2026-09-04

Branch: `unify-doc-tools`

Checkpoint: `d3629e28` (`wip(writer): checkpoint unified document tools`)

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
| A2  | Fix the public scope at 43 Capability APIs and 36 Chat Agent tools. | DONE   |
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
| B3  | Move cross-reference discovery, binding, and refresh into `references.py`.                       | DONE |
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
| B15 | Keep the seven Workflow capabilities available without exposing them to Chat Agents.             | DONE |


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
| C1  | Create the Writer Workflow private `runtime.py`.                             | TODO   |
| C2  | Move checkpoint, fingerprint, and recovery behavior into `runtime.py`.       | TODO   |
| C3  | Move private step, slot, and workspace state handling into `runtime.py`.     | TODO   |
| C4  | Move reusable writing business logic into `document_tools`.                  | PARTIAL |
| C5  | Move reusable revision and provider logic into `document_tools`.             | DONE    |
| C6  | Preserve all `writer_*` entry-point names and signatures referenced by YAML. | DONE    |
| C7  | Reduce every `writer_*` function to Runtime/path adaptation and forwarding.  | PARTIAL |
| C8  | Preserve current progress and draft-stream event behavior.                   | DONE    |
| C9  | Remove direct LazyLLM Writer implementation imports from Workflow adapters.  | PARTIAL |


Target structure:

```text
workflows/writer-workflow/scripts/
├── tools.py       # thin YAML-callable adapters
└── runtime.py     # private Workflow state and recovery
```



## D. Shared Artifact Actions


| ID  | Task                                                                        | Status  |
| --- | --------------------------------------------------------------------------- | ------- |
| D1  | Define `DocumentActionSpec`.                                                | TODO    |
| D2  | Define strict argument models for the four v1 Actions.                      | TODO    |
| D3  | Define strict result models for the four v1 Actions.                        | TODO    |
| D4  | Implement `rewrite_selection.preview.v1`.                                   | TODO    |
| D5  | Implement deterministic `rewrite_selection.execute.v1`.                     | TODO    |
| D6  | Implement `render_document.v1`.                                             | TODO    |
| D7  | Implement `save_document.v1`.                                               | TODO    |
| D8  | Implement `sync_document.v1`.                                               | TODO    |
| D9  | Reject duplicate registration and runtime replacement.                      | DONE    |
| D10 | Resolve `builtin:document.<action>.v1` references.                          | TODO    |
| D11 | Preserve ordinary pinned Workflow package-tool resolution.                  | DONE    |
| D12 | Validate built-in references, versions, and phases at Workflow publication. | TODO    |
| D13 | Reject Actions that are not declared in `workflow.yaml`.                    | DONE    |
| D14 | Enforce side-effect-free preview handlers.                                  | TODO    |
| D15 | Map invalid input, conflict, provider, and internal errors.                 | TODO    |
| D16 | Update the Writer Workflow manifest to use explicit built-in references.    | TODO    |


The current Writer Workflow has only a `rewrite_selection.preview_tool`, while
the backend application path invokes the execute phase. D5/D16 must make the
execute phase deterministic and must not call the model again.

## E. Provider-neutral behavior


| ID  | Task                                                                           | Status  |
| --- | ------------------------------------------------------------------------------ | ------- |
| E1  | Finalize bound-source write-back mode.                                         | PARTIAL |
| E2  | Finalize unbound local-document publication mode.                              | PARTIAL |
| E3  | Return and persist provider bindings after first publication.                  | PARTIAL |
| E4  | Treat explicit cross-provider publication as unbind-and-copy.                  | PARTIAL |
| E5  | Remove the three default-Feishu arguments from Writer Workflow functions.      | DONE    |
| E6  | Generalize or clearly deprecate `/api/writer/documents:sync`.                  | PARTIAL |
| E7  | Audit `document_tools` for provider-specific branches.                         | DONE    |
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
| L4  | Declare GitHub and WeChat capabilities after their provider PRs stabilize.    | EXTERNAL |
| L5  | Remove the default Feishu adapter from `WriterResourceTools.create_document`. | TODO     |
| L6  | Require revision checks for safe Patch-by-replace implementations.            | TODO     |




## T. Compatibility and acceptance tests


| ID  | Task                                                                       | Status  |
| --- | -------------------------------------------------------------------------- | ------- |
| T1  | Verify legacy `writer.py` imports.                                         | DONE |
| T2  | Snapshot all 43 Capability APIs and their owners.                          | DONE |
| T3  | Snapshot the unchanged 36 Chat Agent tools.                                | DONE |
| T4  | Verify class names and registered tool names.                              | DONE |
| T5  | Add pre-refactoring MD/LMD golden fixtures.                                | PARTIAL |
| T6  | Test flat Markdown zero-to-one writing.                                    | TODO    |
| T7  | Test sectioned Markdown zero-to-one writing.                               | TODO    |
| T8  | Test outlining and subproblem execution.                                   | TODO    |
| T9  | Regress Markdown and IR draft streaming.                                   | PARTIAL |
| T10 | Regress checkpointing and failure recovery.                                | PARTIAL |
| T11 | Test all Action argument and result contracts.                             | TODO    |
| T12 | Test built-in resolution and publication validation.                       | TODO    |
| T13 | Verify rewrite preview/execute content identity.                           | TODO    |
| T14 | Verify preview handlers have no external side effects.                     | TODO    |
| T15 | Test Feishu fake-provider first publication and binding.                   | TODO    |
| T16 | Test Notion fake-provider first publication and binding.                   | TODO    |
| T17 | Test bound write-back and revision conflicts for both providers.           | TODO    |
| T18 | Test missing authorization, denied permission, and unsupported capability. | TODO    |
| T19 | Regress academic, bid, and product Writer bridges.                         | PARTIAL |
| T20 | Run existing backend Writer and Artifact Action Go tests.                  | TODO    |
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
| I4  | Update the LazyLLM submodule SHA after provider PRs stabilize.                        | EXTERNAL |
| I5  | Merge the latest `dev-plugin` into this branch if a provider integration lands first. | EXTERNAL |
| I6  | Require provider PRs to migrate to `document_tools` if this refactoring lands first.  | EXTERNAL |
| I7  | Run final Feishu/Notion/GitHub/WeChat joint regression.                               | TODO     |
| I8  | Integrate Obsidian through the shared interface when its work is available.           | EXTERNAL |




## Current summary


| Area                               | Progress summary                                      | Current state |
| ---------------------------------- | ----------------------------------------------------- | ------------- |
| Approved design                    | 10/10 design and scaffold items done                  | Complete      |
| Physical capability split          | 15/15 items done                                      | Complete      |
| Writer Workflow thinning           | 3 done, 3 partial, 3 not started                      | In progress   |
| Shared Artifact Actions            | 3 infrastructure rules done, 13 items not started     | Early stage   |
| Provider-neutral behavior          | 2 done, 5 partial, 3 not started                      | In progress   |
| LazyLLM provider contract          | 5 not started, 1 waiting on provider PRs              | Not started   |
| Compatibility and acceptance tests | 4 done, 4 partial, 15 not started                     | In progress   |
| Backend handoff                    | 1 done, 2 partial, 7 not started                      | Early stage   |
| Parallel knowledge-source work     | 3 coordination rules done, 4 external, 1 final test  | Parallel      |


## Current implementation boundary

Completed in the working tree:

- The 57 existing document capability methods are physically owned by
  `artifacts.py`, `writing.py`, `revision.py`, and `resources.py` without adding
  or removing a capability method.
- `toolkits.py` now contains composition and compatibility only. The three
  concrete Toolkits retain the existing 36 Chat Agent tools, while the seven
  Workflow-only capabilities remain algorithm APIs.
- Legacy imports through `lazymind.chat.engine.tools.writer` remain available.
- Reusable short-writing, media, selection-revision, provider-locator, and
  synchronization logic has moved out of the Writer Workflow adapter.
- Writer Workflow entry-point names are unchanged. The only intentional
  signature-default changes remove the two remaining `adapter='feishu'`
  defaults from `writer_sync_document` and `writer_create_document`.
- MD/LMD conversion delegates to the existing LazyLLM Writer conversion rules.

Not yet complete:

- `workflows/writer-workflow/scripts/runtime.py` does not exist. Checkpoint,
  fingerprint, recovery, step, slot, and workspace state still live in
  `scripts/tools.py`.
- Some document assembly and numbering policy still lives in the Workflow
  adapter, so `tools.py` is not yet a uniformly thin forwarding layer.
- The four backend-facing v1 Actions do not yet have typed contracts or complete
  built-in implementations. `actions.py` is still a registry scaffold.
- Provider binding lifecycle, cross-provider copy semantics, structured
  capability errors, ambiguous-write handling, and conflict tests are not done.
- The LazyLLM provider-capability contract has not started.
- The backend handoff package and final acceptance suite are incomplete.




## Latest verification

- Python compilation and Pyflakes checks pass for `document_tools`, the Writer
  Workflow adapter, and the modified Workflow route.
- `git diff --check` passes.
- 12 focused module ownership, API snapshot, legacy import, conversion,
  provider-neutral sync, and action-registry tests pass.
- All 20 Writer Workflow drafting, revision, stream, local LMD, and media tests
  pass with a temporary `rapidfuzz` import shim; the shim is outside the repo.
- 2 hermetic stream-recovery tests pass.
- 22 academic and bid Writer bridge tests pass.
- Shared MD/LMD conversion, short-document planning/streaming, media search and
  filtering, cross-reference discovery/refresh, provider locator resolution,
  and provider-neutral synchronization now live under `document_tools`.
- 54 relevant tests pass in the final focused run.
- The focused MD/LMD test now asserts the exact established envelope data and
  rendered Markdown instead of checking only for substrings.
- The broader local suite remains unavailable until the real optional Runtime/RAG
  dependencies such as `rapidfuzz` and the LazyLLM RAG dependency group are
  installed. Product bridge tests also require their hermetic subagent-tool
  stub to be restored independently of this refactoring.

## Recommended execution order

1. Review and commit the completed B-stage physical split as a stable local
   checkpoint. Keep the task tracker in the same checkpoint or a separate docs
   commit, but do not leave the implementation only in the working tree.
2. Finish C1-C9: create the private Writer Workflow Runtime, move remaining
   state/recovery behavior into it, and reduce `scripts/tools.py` to path,
   Runtime, and event adapters.
3. Implement D1-D16: typed v1 Action contracts, deterministic rewrite execute,
   built-in resolution, publication validation, and Workflow declarations.
4. Complete E1-E10 and L1-L6: provider binding lifecycle, provider capabilities,
   structured errors, conflict rules, and ambiguous-write behavior.
5. Complete T5-T23, then prepare H2-H10 for backend handoff.
6. Merge whichever provider integrations pass their own acceptance gates, adapt
   the later side to the shared interface, and run the final joint regression.

The immediate next task is therefore the remaining Writer Workflow thinning,
not the backend handoff and not waiting for every knowledge-source PR.
