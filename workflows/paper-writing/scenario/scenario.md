# Paper writing

Start when the user explicitly requests paper writing or academic manuscript review.
For uploaded manuscripts, bind the exact attachment filename to manuscript_file and run peer_review directly.
Bind pasted text to manuscript_text; paper_topic and review_request are optional plain text.
If no topic is supplied, code derives a default from the manuscript title or filename.
paper_input supports existing programmatic business inputs only. Users need not supply JSON or pool.json.
Never treat filenames, paths, or summaries as manuscript text. If multiple attachments make the target ambiguous,
ask which manuscript to review.
Use fictional examples only for demo=true or an explicit request for default demonstration materials.
Otherwise ask for missing real materials; never silently fall back to fabricated examples.
When paper_input is bound, use it; validation failure must not trigger a fallback to default examples.

Select the algorithm node matching the user's goal without requiring preceding steps.
Review requests enter peer_review directly. Code initializes materials before execution; there is no prepare step.
Wait for the user's selection at each step; finish is available at any point.
Literature and review feedback use forward nodes:
literature_review → refine_topic, then rewind literature_review when the user asks to continue;
peer_review → revise_document, then rewind peer_review when the user asks for another review.
Native rewind preserves confirmed feedback snapshots before invalidating downstream state, allowing multiple rounds.
Do not reinitialize materials. Starting over differs from continuing with feedback; use a new session to clear feedback.

Allowed routes: F1→F2; F2→F1/F3/F5/F7; F3→F4; F4→F5; F5→F6; F6→F5; finish after F7.
Do not add other execution edges. refine_topic and revise_document are feedback operations of F1/F5,
not additional algorithm capabilities.
This workflow produces manuscripts or reading reflections. It does not generate or hand off to external workflows,
ingest into the knowledge base, or execute experiments.
Surface failures and ask for materials or a retry; never fabricate success.

## Chat completion

After the steps requested in this turn succeed and their artifacts are published, acknowledge completion in one
short sentence in the user's language and direct them to the workflow.
Do not repeat the document, review, or JSON, or list issues, conclusions, or next-step options.
Do not call list_artifacts, read_artifact, read_file_resource, or similar tools merely to summarize, repeat,
or reconfirm successfully published artifacts.
Read results when explicitly asked to explain them; continue subsequent steps when explicitly requested,
then acknowledge briefly once the current request is fulfilled.
For a review-only request, stop after successful review without automatically revising or finishing the workflow.
On failure or missing required input, explain the actual cause and wait for user action; do not claim success.
Always relay necessary user_notice messages from tools.

Use the configured Chinese stage labels when addressing users; do not expose F numbers or internal identifiers.
Do not show material preparation as a node or checklist document. On parsing failure, ask for a readable manuscript.
finish is an internal control node. Users may request it in Chat; existing artifacts are preserved.
Subsequent actions must select nodes that actually exist in the execution graph.
