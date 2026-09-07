# Unified Document Tools Refactoring Plan

This document records the decisions for extracting the shared writing
capabilities into `algorithm/lazymind/document_tools/`. A topic is considered
closed only after it has been explicitly confirmed during the design review.

Implementation progress is tracked separately in
[`unified-document-tools-tasks.md`](./unified-document-tools-tasks.md).

## Review status

1. Public API boundaries: confirmed
2. Internal module ownership and implementation split: confirmed
3. Shared Workflow artifact actions: confirmed
4. Provider-neutral target selection: confirmed
5. Compatibility tests and backend handoff contract: confirmed

## 1. Public API boundaries

Status: confirmed.

### Three API layers

The document tools expose three separate API layers:

- Capability API: reusable, fine-grained Python capabilities for algorithm code
  and Workflow implementations.
- Toolkit API: a curated subset of safe tools exposed to Chat Agents.
- Action API: stable, coarse-grained business operations consumed by the backend
  and frontend.

The backend must depend only on the Action API and its data contracts. It must
not depend directly on individual writing methods or algorithm-local paths.

### Capability and Toolkit exposure

All 43 pre-integration public methods on `WriterToolkitBase`, plus the two
provider-integration capabilities `resolve_create_target` and
`prepare_markdown_for_editor`, belong to the 45-method algorithm Capability
API. The following nine capabilities are included in that API but
are not added to the Chat Agent Toolkit API:

- `collect_available_media`
- `resolve_visual_needs`
- `materialize_acquired_media`
- `stream_outline`
- `execute_writing_subtasks`
- `stream_draft_blocks_ir`
- `stream_draft_blocks_markdown`
- `resolve_create_target`
- `prepare_markdown_for_editor`

The Chat Agent exposure remains compatible with the pre-refactoring version:
the existing 36 entries in the toolkit `__public_apis__` lists are neither
removed nor expanded in this version.

### Markdown and LMD

- Markdown and LMD remain the two general internal writing representations.
- Their conversion behavior and supported range remain exactly the same as
  before this refactoring. This work introduces no new conversion semantics.
- Zero-to-one writing uses Markdown by default.
- LMD is used when a flow explicitly reads from or writes back to a bound
  knowledge source.
- Existing Workflow fallback, exception, streaming, file-path, and Artifact
  behavior must not change as a side effect of the refactoring.

### Compatibility policy

- Existing imports from `lazymind.chat.engine.tools.writer` remain supported.
- Existing class names, tool names, method parameters, and return values remain
  compatible in this version.
- New code imports shared capabilities from `lazymind.document_tools`.
- Workflow internals may continue passing local Artifact paths.
- Action API requests and responses must never require the backend to understand
  or access an algorithm-local path.

### Action API v1

The backend-facing Action API v1 contains only these operations:

| Action | Phase | Responsibility |
| --- | --- | --- |
| `rewrite_selection` | `preview` | Generate a selected-text rewrite preview. |
| `render_document` | `preview`, `execute` | Produce displayable document content. |
| `save_document` | `execute` | Save or download the selected Artifact. |
| `sync_document` | `execute` | Write back to a bound source or create the selected provider target. |

The lower-level resource operations `load_document`, `create_document`,
`replace_document`, `append_document`, and `publish_revision` remain Capability
APIs and are not exposed directly to the backend in v1.

Each Workflow explicitly declares whether an action is enabled and which slots
it may access. A Workflow may reference either its own package tool or an
explicit built-in handler; no undeclared action and no implicit handler fallback
is permitted. The existing `WorkflowActionInvokeRequest` transport contract
remains unchanged, while each action owns a fixed result contract rather than
sharing an overly generic result payload.

## 2. Internal module ownership and implementation split

Status: confirmed.

### Confirmed class boundary

The three concrete toolkits own distinct capability sets:

- `WriterCreateToolkit` owns writing capabilities only.
- `WriterRevisionToolkit` owns revision capabilities only.
- `WriterResourceToolkit` owns external document resource capabilities only.

They must no longer inherit every document capability from one monolithic base
class. Their public Chat Agent exposure remains governed by their existing
`__public_apis__` lists.

`WriterToolkitBase` is retained in this version only as a compatibility aggregate
for old imports, schema constants, and any direct callers. New code must not
inherit from or depend on it. Once callers have migrated and the compatibility
window is explicitly closed, the aggregate class may be removed in a separate
change.

### Confirmed module ownership

- `artifacts.py` owns schemas, JSON and Artifact serialization, temporary
  Artifact paths, MD/LMD conversion, and Markdown rendering.
- `writing.py` owns writing-task and context preparation, resource profiling,
  multimodal preparation, outlining, subproblem execution, drafting, streaming,
  consistency checks, and final-document generation.
- `revision.py` owns revision-task construction, target location, modification
  planning, Patch generation, validation, and local application.
- `references.py` owns cross-reference discovery, binding, and refresh behavior
  shared by writing and revision flows.
- `resources.py` owns provider locator resolution and external document load,
  create, replace, append, publish, and synchronization behavior.
- `toolkits.py` composes the capability owners into the curated Chat Agent
  toolkits and maintains compatibility exports.
- `actions.py` composes coarse-grained Workflow Artifact actions without owning
  the underlying writing algorithms.

`render_markdown` is implemented by `artifacts.py` and remains reachable through
the existing `WriterCreateToolkit` API via a compatibility delegate.
`sync_writer_documents` is implemented by `resources.py`; its old import location
continues to re-export it for compatibility.

Cross-module flows must compose these owners rather than duplicate their logic.
In particular, actions may call writing, revision, and resource capabilities;
resource synchronization may call pure revision/Patch capabilities; and writing
and revision may share reference and Artifact helpers.

### Confirmed migration strategy

The implementation split is behavior-preserving and staged:

1. Move each existing capability to its owning module without changing its
   signature, return value, exception behavior, fallback behavior, streaming
   behavior, or Artifact-path contract.
2. Run the existing Workflow contract tests after each capability group moves.
3. Switch concrete toolkits to their distinct capability owners.
4. Reduce `WriterToolkitBase` to a compatibility aggregate only after callers
   use the new concrete classes and modules.

Algorithm behavior cleanup and feature changes must not be mixed into the
mechanical ownership migration.

### Confirmed Writer Workflow boundary

`workflows/writer-workflow/scripts/tools.py` becomes a thin Workflow adapter.
It retains only the callable names and signatures referenced by `workflow.yaml`,
Workflow Runtime context and Artifact-path adaptation, calls into
`document_tools`, result registration, and progress or stream-event forwarding.

Reusable writing policy and implementation move to their owning
`document_tools` modules. This includes request and length parsing, document
structure selection, outlining and drafting, multimodal acquisition and
placement, document assembly and numbering, selection revision, provider
operations, and the shared Action implementations.

Writer-Workflow-specific state orchestration must not be moved into the shared
document package. Checkpoints, fingerprints, idempotent recovery, step/slot
state, and Workflow Runtime context handling remain private in the Workflow's
single Python entry module:

```text
workflows/writer-workflow/scripts/
└── tools.py       # YAML adapters plus private state/recovery orchestration
```

Other Workflows may reuse `document_tools` without inheriting the Writer
Workflow's step, slot, checkpoint, or recovery policy.

## 3. Shared Workflow Artifact actions

Status: confirmed.

### Confirmed declaration and registry rules

Workflows continue to declare `artifact_actions`, allowed `slots`, and the
existing `preview_tool` or `execute_tool` fields. A shared implementation is
selected explicitly with a built-in reference such as:

```yaml
artifact_actions:
  rewrite_selection:
    slots: [outline_document, draft_document]
    preview_tool: builtin:document.rewrite_selection.v1
```

- A `builtin:` reference resolves through `document_tools/actions.py`.
- An ordinary tool name resolves from the pinned Workflow package as it does
  today.
- Missing tool declarations are errors; the Runtime never guesses a handler by
  action name.
- Actions absent from `workflow.yaml` are not enabled, even if a matching
  built-in handler exists.
- Built-in registrations are static and cannot be replaced at runtime.
- Duplicate built-in registrations fail immediately.
- Workflow-specific behavior uses a package tool instead of overriding a
  built-in registration.

### Confirmed handler versioning

Built-in Action handler identifiers carry an internal contract version, for
example `builtin:document.rewrite_selection.v1`. This version is independent of
both the immutable Workflow revision and the mutable Artifact revision:

- the Workflow revision selects a handler contract version;
- the handler operates on a selected Artifact revision;
- the backend may persist its result as a new Artifact revision.

Backward-compatible bug fixes and performance improvements may remain within
`v1`. Changes to required inputs, result fields, supported representations, or
core semantics require a new handler contract version such as `v2`.

### Confirmed typed contracts

Each of the four v1 Actions has its own strict arguments model and result model.
The registry associates those models with the handler and validates both sides
of execution. Unknown Action arguments are rejected.

The current backend-facing JSON field names, types, and meanings remain
compatible. The shared `WorkflowActionInvokeRequest` transport remains the outer
request envelope; Runtime-owned fields such as `artifact`, `slot`, and
`artifact_store` are injected separately and cannot be supplied through user
arguments. Invalid caller input remains a 422-class error, while a handler result
that violates its registered contract is an algorithm/upstream failure.

### Confirmed execution and failure boundaries

- Built-in references, versions, and supported phases are validated when a
  Workflow is published, rather than failing for the first time during a run.
- Preview handlers have no durable or external side effects.
- `rewrite_selection.preview` may call a model to produce a candidate;
  `rewrite_selection.execute` applies that exact candidate and must not call a
  model to regenerate it.
- `render_document` is a pure representation/rendering operation.
- `save_document` normalizes and returns an Artifact; the backend owns Artifact
  revision persistence.
- `sync_document` is the only v1 shared Action allowed to mutate an external
  provider document.
- Ambiguous external write outcomes are not retried automatically, preventing
  duplicate document creation or duplicate append operations.
- The backend retains ownership of authorization, optimistic concurrency,
  Artifact revisions, database persistence, and update notifications.
- Invalid arguments map to a 422-class response; stale selections and Artifact
  conflicts map to 409; provider authorization and permission failures retain
  structured provider errors; handler failures or invalid handler results map
  to an upstream/502-class response.

The Writer Workflow must declare a deterministic execute handler for
`rewrite_selection`; applying an accepted preview cannot regenerate content or
depend on a missing execute-phase tool.

## 4. Provider-neutral target selection

Status: confirmed.

### Confirmed two-mode synchronization

Provider synchronization has two explicit modes:

1. Bound-source write-back receives a source document and its revised document.
   The source `provider_binding` is authoritative and the operation writes back
   to that exact provider document.
2. Local-document publication receives unbound Markdown or LMD. It either writes
   to an explicit `target_document` or creates a document through an explicitly
   selected `adapter`.

The algorithm does not select a product-default provider. Missing target and
adapter information for an unbound document is an error. Provider selection
must not silently fall back to another provider, and a failed bound write-back
must not silently create a new document.

After the first successful publication of a zero-to-one document, the provider
returns a normalized `TargetDocument` and the confirmed persisted
`WriterDocument`. The backend stores that returned document as a new Artifact
revision, including its document- and block-level provider bindings. Later edits
therefore use bound-source write-back mode.

Publishing an already-bound document to a different explicitly selected
provider is treated as a new publication/copy: provider-owned document and block
identity are removed before mode 2 executes. It is not treated as a write-back
to the original source.

### Current implementation audit

The local Feishu and Notion providers already share the required lifecycle:
both create and return a `TargetDocument`, write Markdown or Writer IR through
that target, read back the confirmed representation, and attach provider binding
state to the persisted document.

The refactoring must remove remaining Feishu defaults from the algorithm and
Workflow layers, including the defaults in the Writer Workflow forwarding
functions and LazyLLM `WriterResourceTools.create_document`. The legacy
`/api/writer/documents:sync` algorithm endpoint also has a Feishu-only tool-config
check and must either become provider-neutral or remain only as a clearly
deprecated compatibility route. The current backend defaults an unbound document
to Feishu; that is a product-layer choice and must be changed by the backend
handoff if implicit Feishu selection is no longer desired. Until that backend
change lands, it may preserve current behavior only by explicitly sending
`adapter="feishu"`; the algorithm itself has no Feishu default.

### Confirmed provider capability contract

Each LazyLLM Writer provider explicitly declares the document capabilities it
implements, including `load`, `create`, `replace`, `append`, `patch`, revision
checking, and media support where applicable. Optional capabilities default to
unsupported and are enabled only when backed by implementation and tests.

Capability support is distinct from account authorization and target-document
permission. Execution checks implementation support first, then authorization,
then target access.

A provider without a native Patch API may advertise Patch support only when its
adapter implements a safe equivalent, including remote revision validation
before local Patch application and whole-document replacement. Without a safe
equivalent, the operation returns a structured
`PROVIDER_CAPABILITY_UNSUPPORTED` error. It never switches provider, silently
overwrites concurrent edits, or drops provider-specific unknown blocks.

Provider differences remain inside LazyLLM adapters. Shared LazyMind document
capabilities and Actions contain no Feishu-, Notion-, or future-provider
branches.

## 5. Compatibility tests and backend handoff contract

Status: confirmed.

### Confirmed handoff test gate

The algorithm implementation is eligible for backend handoff only after the
relevant automated suites are green:

- Capability ownership and the complete 45-method API snapshot.
- The unchanged 36-tool Chat Agent exposure and legacy import, class-name, and
  tool-name compatibility.
- Golden regression fixtures for the pre-refactoring MD/LMD conversions.
- Flat and sectioned zero-to-one writing, outlining, subproblem execution,
  streaming drafts, selection rewrite, checkpointing, and recovery.
- Built-in Action resolution/versioning, publish-time validation, strict input
  and output contracts, phase side-effect rules, and error mappings.
- Feishu and Notion fake-provider flows covering bound write-back, first publish
  and binding, explicit cross-provider copy, revision conflict, authorization,
  permission, unsupported capability, and ambiguous external outcomes.
- Existing academic, bid, and product Writer bridge regressions.
- Existing backend Writer and Artifact Action Go tests that assert the unchanged
  JSON boundary.

Python compilation and formatting checks and `git diff --check` must pass. Core
cases cannot be hidden with skips or expected failures. Writer Workflow adapters
must use `document_tools` rather than importing LazyLLM Writer implementation
directly; shared LazyMind modules must contain no provider-specific branches;
and algorithm/Workflow APIs must contain no default Feishu adapter.

Unrelated failures in a full-repository test run do not block handoff when they
are recorded and shown to predate or fall outside this change. Real-account
external tests are kept out of ordinary CI, but one Feishu and one Notion manual
end-to-end smoke test must be completed and recorded before handoff.

### Confirmed backend handoff package

The repository handoff includes:

- this decision record;
- JSON Schemas for each v1 Action's arguments and result;
- representative success and failure payloads;
- the versioned built-in Action registry;
- provider capability and binding lifecycle documentation;
- the error-code, HTTP-status, and retryability mapping;
- a legacy/new compatibility matrix; and
- automated test commands/results plus the two manual provider smoke-test
  records.

The backend handoff explicitly requires removal of the unbound-document Feishu
default, caller-owned selection of the first publication provider, persistence
of the returned provider binding, structured Action/provider error handling,
and continued backend ownership of authorization, concurrency, Artifact
revisioning, persistence, and notifications. Backend code must not depend on
algorithm-local paths or add provider-specific writing branches.

### Confirmed compatible rollout

The rollout order is:

1. Land the LazyLLM provider capability contract and remove its default Feishu
   adapter.
2. Pin that LazyLLM revision in LazyMind and land `document_tools`, built-in
   Action resolution, and all compatibility exports.
3. Publish a new Writer Workflow revision that explicitly references the
   versioned built-in handlers.
4. Land backend provider-selection and unified-error changes.
5. Land frontend provider-selection and error-presentation changes.

These streams may overlap after their contracts are available. The algorithm
continues supporting existing package tools while adding explicit built-in
references, so old pinned Workflow revisions and Sessions remain operational
during the transition. LazyLLM and LazyMind changes are delivered as separate,
reviewable commits, and the LazyMind commit pins the exact LazyLLM submodule SHA
used by the implementation.
