---
name: smartdata-analytics
description: Answer business data questions through the SmartData MCP using read-only queries. Use for counts, totals, averages, extrema, rankings, date-filtered metrics, and supported trends from connected data; do not use for database administration, data mutation, or general-knowledge questions.
---

# SmartData 数据问答

Use SmartData only when the answer must come from a connected business data source.

SmartData owns the whole question path — intent, retrieval, grounding, planning, SQL generation, validation and execution. Your job is only to pick the right data source, call the right MCP tool, and report what SmartData returned. Never reproduce any of that pipeline yourself: do not plan a query, do not write SQL to work around a failed question, and do not combine results from several sources into one answer.

Preserve the user's own business wording when you call `ask_data`. Do not rewrite "今年华东销售额是多少？" into a self-invented definition or a self-authored query.

## Workflow

1. **Find the source.** Call `list_datasources`. If exactly one is `ready`, use it — do not ask the user for table or field names first.
2. **Choose among several.** If several `ready` sources could be relevant, choose by datasource name and the user's stated context. Only if it is still genuinely unclear, call `get_schema_context` or `search_dataset` to narrow it. Ask the user a minimal question only when narrowing still does not settle it.
3. **Make it ready.** If the chosen source is `created` (or otherwise not `ready`), call `scan_datasource` first. Do not scan a source that is already `ready`.
4. **Ask.** Call `ask_data` with the original question, the workspace, the chosen source, and a sensible row limit.
5. **Report.** Answer from the returned `answer`, `result` and `evidence`. If the result is truncated or does not cover the requested scope, say so.

Steps 2's schema tools are diagnostic and ambiguity-resolution tools, not a fixed preamble. Never dump the entire schema before every question.

## Connecting a data source

`register_sqlite` — only when the user gives a local SQLite file path and asks to connect it. It is a convenience wrapper over the same secure contract; it does not need a legacy connection JSON. Follow it with `scan_datasource`.

Any other driver — the order is fixed:

```text
test_secure_datasource
→ create_secure_datasource
→ scan_datasource
```

Do not skip the test. `create_secure_datasource` stops at `status = created`, which is a normal intermediate state and **not** yet answerable; `scan_datasource` is what makes it `ready`.

To change an existing secure datasource, the order is `test_secure_datasource(candidate)` → `update_secure_datasource` → `scan_datasource`. An update invalidates the previous scan, so the scan is required again.

Call `delete_datasource` only when the user explicitly asks to delete a datasource. Never delete one because a connection, scan or question failed.

## Credentials

MCP consumes secret **references**; it never creates or carries secret **values**. When you supply `connection_profile`, put the user's secret reference there as structured data:

```json
{ "provider": "managed", "identifier": "sec_xxx" }
```

`environment` and `file` references are equally valid. Do not try to resolve or interpret what the reference points to.

Never ask the user to paste into an MCP call, and never pass through: a password, a token, an API key, a certificate PEM, a private key, or a private key password. If the user volunteers a plaintext secret, do not echo it back and do not forward it.

When a password- or TLS-protected source is needed and the user has no `SecretReference` yet, tell them a credential must first be created through an existing SmartData product entry point:

```text
SmartData CLI     e.g. smartdata credential add / smartdata certificate add-ca
SmartData HTTP API  POST /api/credentials, /api/certificates/ca, /api/certificates/client-identity
```

That entry point returns an opaque secret id; the user then brings `SecretReference` back for use here. A Web credential UI is planned but is not available yet — do not present it as a current entry point.

There is no MCP tool for creating, uploading or deleting credentials, certificates or private keys. Do not look for one.

## Progress is execution status, not reasoning

SmartData reports its stages as MCP progress notifications:

```text
accepted → intent_ready → retrieval_ready → grounding_ready → plan_ready → query_ready → execution_started → result_ready → done
```

These say what SmartData is doing — understanding the question, matching the data structure, generating a safe query, executing it. They are **not** the answer, and they are **not** the model's chain of thought. Never describe them as what the AI is thinking.

If the client shows no progress at all, the question still runs normally. Absence of a progress UI is not a failure.

## Reading the response

`status = completed` — answer strictly from `answer`, `result` and `evidence`. Never adjust, estimate or complete a number from your own knowledge. Every figure you state must appear in the SmartData response. If the rows are empty, say there were no records in scope — do not reinterpret that as zero unless SmartData returned zero. If `truncated = true`, say the result was cut off and do not infer a full ranking or total from the partial rows.

`status = clarification_required` — this is a normal product outcome, not an error. Pass SmartData's clarification question to the user; if it offers `options`, show the smallest useful choice. Then call `ask_data` again with the user's answer. Never guess a clarification yourself — if SmartData asks which time dimension, metric or datasource, ask the user rather than picking one.

MCP tool error — a stable SmartData code such as `datasource_not_ready`, `datasource_not_found`, `unsafe_query` or `model_invocation_failed`. State the actual problem and the smallest corrective action. Never claim the question was answered, and never respond by writing your own SQL or by querying several sources and merging them by hand.

If the user asks how a result was produced, `evidence` (`display_command`, `plan_id`, `datasource_id`, `scan_version`, `row_count`, `truncated`) explains it. That is execution evidence, not model reasoning. Offer raw SQL or rows only when the user asks or when they are needed to answer.

If the response carries suggested follow-up questions, offer at most two of them, and only when relevant. Do not invent a list of your own.

## Explicit SQL and safety

Send `sql` to `ask_data` only when the user explicitly supplies or explicitly requests one specific read-only query. It is never a fallback for a failed natural-language question.

Never submit `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `PRAGMA`, `ATTACH`, or more than one statement. Only SQL that SmartData's existing read-only validation accepts may be submitted.

SmartData's current planning scope decides what is answerable. Submit the question and let it answer `completed`, ask for clarification, or return an error — do not pre-emptively reject a question based on assumptions about what the planner can do.

Cross-source joins are not part of the current Roadshow scope. Do not query several datasources separately and stitch the numbers into a single claimed result; if a question genuinely needs unsupported cross-source analysis, explain the scope limit.

For concrete routing and failure examples, read [references/behavior-examples.md](references/behavior-examples.md) when the request is ambiguous or a tool call fails.
