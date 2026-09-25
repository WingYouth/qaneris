# IQ-06 — Conversational Web acceptance

Status: **BLOCKED for release freeze**. The implementation and the tested SQLite browser flows pass. Several required release scenarios have not been verified in this run, so IQ-06 is not declared PASS and the feature freeze has not begun.

Repository: `WingYouth/qaneris`  
Baseline: `main@d43bfd9eead66c2060b248f4290c659fec1f4fe4`

## Frontend architecture

- Analyze now mounts `ConversationWorkspace`. Datasource Manager and Excel Import keep their existing navigation and components.
- `api/conversations.js` uses the existing relative-URL JSON client. `api/runStream.js` observes durable Run events by GET and resumes from the last sequence. `api/sse.js` is shared with the legacy Ask stream parser.
- `conversationState.js` stores messages, runs, events, and sequences by `run_id`. Historic answers render from persisted messages; complete historic results load when requested. The most recent Run and its public events replay on reopen.
- Run views branch on the explicit `run_kind`: normal reuses `askViewFromResponse`, `VisualizationPanel`, and `EvidencePanel`; diagnostic shows evidence question progress and refs; federated projects `merged_result` into the existing visualization model and shows source tasks, merge state, and evidence.
- The current Conversation ID is kept in the URL. The browser does not store credentials or semantic memory. Backend content is rendered as React text.

## Connected backend capabilities

IQ-01/02: existing governed Ask and real read-only SQLite execution. IQ-03: durable Conversation, Run, semantic memory, clarification, cancellation, retry, and event replay. IQ-04: diagnostic evidence questions and synthesis. IQ-05: federated source tasks, controlled merge, confirmed key mapping, and BLOCKED mapping refusal.

## Real Chrome + FastAPI + SQLite observations

The browser used Google Chrome on `127.0.0.1:5173`, Vite's `/api` proxy, real FastAPI endpoints, durable Run Runtime, and SQLite source databases. The deterministic test model and in-memory published graph metadata are reused from the existing integration fixtures; SQL execution is against real SQLite files. The fixture launcher is `tests/e2e/iq06_browser_fixture.py`.

| Flow | Observation |
| --- | --- |
| New conversation and datasource scope | Created conversations with one and two READY sources. |
| Normal, multi-turn | Orders sales returned 150; payments returned 130. Both message-level result tables remained visible. |
| Reopen and lazy history | Refresh and sidebar reopen restored messages, latest result and timeline; loading an older Run restored its own 150 result and evidence. |
| Visualization | The diagnostic fixture's normal sales query rendered a two-point bar chart. |
| Diagnostic | A follow-up produced `run_kind=diagnostic`, two verified evidence questions, and a final answer. |
| Clarification | A diagnostic question entered `WAITING_USER`; free-text clarification resumed the same Run and completed. Refresh restored the four chronological messages. |
| Federation | Orders + payments returned a merged 150 / 130 / 20 comparison with two completed source tasks, timestamps, input/output counts, and the independent-snapshot notice. |
| Unconfirmed mapping | The paid-but-unshipped question returned `BLOCKED` with a specific confirmed-key message and no fake empty result. |
| Confirmed key join | Two SQLite sources returned unpaid shipment exception order `2` through a confirmed anti join. |
| Mid-run reload | A slow Run was visible as executing, Chrome refreshed, and the same conversation recovered the completed Run and result. |
| Cancel | A slow Run was cancelled; the UI reached `CANCELLED` and displayed no assistant answer. |
| Retry | A retryable graph failure exposed Retry; the same message gained a second attempt in its timeline. The underlying graph mismatch persisted, so success after retry was not demonstrated. |

The existing local `sales` catalog source refused execution due to a missing scan version; the existing `零售订单` source refused execution due to graph/catalog revision mismatch. Those are environment data issues, and the UI showed the failures rather than invented answers.

## Tests and regression

- Frontend: `npm test` — 59 passed; `npm run build` — passed.
- Backend: `QANERIS_ARTIFACT_ROOT=/private/tmp/qaneris-iq06-artifacts .venv/bin/pytest -q` — 1587 passed, 3 skipped. The artifact override is needed because the default external upload directory is outside the writable sandbox.
- `ruff check .` and `git diff --check` — passed.
- The full backend suite covers IQ-01 through IQ-05, legacy Ask API, CLI, MCP, Excel, and datasource behavior. No backend production code was changed for IQ-06.

## Security

The new UI renders user, answer, error, and evidence text through React. It uses the backend's `evidence.display_command`, never bound parameters or private Run internals. Run timeline labels come from a whitelist of public event types; unknown events and raw payloads are not displayed. The existing frontend security test passes. A complete browser Network/DOM credential scan across all supported drivers was not performed.

## Remaining release checks

- Complete the exact four-question semantic-memory browser scenario and follow-up after reopen; the underlying SQLite conversation integration test passes.
- Exercise an actual browser stream disconnect followed by `after_sequence > 0` reconnect. Unit tests cover reconnect, sequence validation, and all settled statuses; browser mid-run page reload passed.
- Verify successful same-Run retry after an injected transient failure, answer-model 429 fallback for each run kind, and a browser Network/DOM credential scan.
- Run browser-level regression through Datasource Manager and Excel Import. Their frontend and backend suites passed.
- Mixed-driver smoke and PostgreSQL/MongoDB/Redis mTLS acceptance were not run in this IQ-06 session. Their current external availability is unverified.

The release status remains BLOCKED until these checks are completed. Do not label the IQ-01–IQ-06 series complete or enter feature freeze on the basis of this report.
