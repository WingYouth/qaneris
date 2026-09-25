# IQ-04 Diagnostic Agent

Diagnostic questions route from the conversation runtime when `RuleExtractor` identifies
`BusinessObjective.DIAGNOSIS`. A follow-up that explicitly asks to investigate further may also
continue the latest diagnostic context. Normal questions retain the IQ-03 Ask path.

The coordinator runs at most two planning rounds and five evidence questions. The model returns
business-language questions only. Every evidence task calls `AskCapability.execute`, including
after clarification or retry. The first verified result pins the datasource for the remaining
tasks; a one-datasource conversation scope pins it from the start. Multi-source scopes remain
blocked until IQ-05.

SQLite stores each task, Ask response, and diagnostic checkpoint. Planned batches and their budget
are committed together. Completed tasks are read from the checkpoint on retry and never queried
again. A process restart marks an interrupted running task retryable; it does not resume a query
without an explicit retry. Existing run events provide SSE replay without starting new work.

Only redacted, bounded rows from verified results reach planning and synthesis. Missing governed
data is recorded as unavailable and cannot support a conclusion. The final answer uses the shared
number guard, rejects unsupported causal wording, and falls back to a deterministic summary when
the synthesis model fails. A completed run writes an evidence-linked historical finding while
preserving the active semantic query context. Historical findings guide follow-up questions but
must be verified again against the current database state.

Local acceptance uses a real SQLite database for evidence execution. IQ-02 PostgreSQL, MongoDB,
and Redis mTLS environment blockers are inherited and are outside this card.
