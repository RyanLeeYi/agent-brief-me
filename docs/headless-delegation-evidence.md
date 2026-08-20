# Headless Delegation Evidence

This document records an existing observation that a headless `claude -p`
session can successfully dispatch a subagent (`Explore`), and explains how to
audit any session's transcript to verify the same claim independently. No
experiment is re-run by this document; it reports what was already observed.

## Observation (2026-08-20, session `ba5dd2b3-d18d-4fda-9d88-73decb0db19b`)

Environment: Windows, invoked non-interactively via `claude -p`. The prompt
asked the session to dispatch an `Explore` subagent to list files in a
directory.

Result: the session dispatched an `Explore` subagent, which returned a file
listing, and the top-level session relayed that listing verbatim as its final
output: `DELEGATED_OK: sample-a.md, sample-b.md`.

### Evidence

The transcript and usage data for this session support the delegation claim
on four independent points:

- **(a) Agent tool call with `subagent_type: "Explore"`.** The transcript
  contains a top-level `Agent` tool call whose `subagent_type` parameter is
  `"Explore"`, and a corresponding tool result of `"sample-a.md\nsample-b.md"`.
- **(b) `modelUsage` includes Haiku usage.** The session's `modelUsage`
  contains entries for both the top-level model and `claude-haiku-4-5`
  (approximately 23k cache tokens, 638 output tokens on the Haiku entry).
  This matches the project convention of pinning the `Explore` role to Haiku,
  and is independent evidence that a subagent actually ran on its own model
  rather than the top-level session merely claiming to have delegated.
- **(c) `permission_denials` is empty.** The session's `permission_denials`
  array is `[]`, so the delegation was not blocked by a permission gate; the
  subagent ran to completion under normal authorization.
- **(d) Final output relays the subagent's report verbatim.** The top-level
  session's final reply, `DELEGATED_OK: sample-a.md, sample-b.md`, reproduces
  the `Explore` subagent's tool result without alteration, showing the
  top-level session consumed the subagent's actual output rather than
  fabricating a result.

Together, (a)-(d) establish that headless (`claude -p`) sessions can dispatch
subagents end-to-end: the dispatch call is present, the subagent executed on
its pinned model, no permission gate blocked it, and its output flowed back
into the top-level session's reply unmodified.

## Auditing any dispatched session

Any Claude Code session's full transcript is stored as JSONL at:

```
~/.claude/projects/<cwd-encoded>/<session_id>.jsonl
```

- `<session_id>` is the session's UUID (e.g. the `ba5dd2b3-d18d-4fda-9d88-73decb0db19b`
  session referenced above).
- `<cwd-encoded>` is the absolute path of the session's working directory,
  with path separators and the drive colon replaced by hyphens. For example,
  a Windows working directory of the shape `C:\Users\<name>\project` encodes
  to `C--Users-<name>-project`; a POSIX working directory of the shape
  `/home/<name>/project` encodes to `-home-<name>-project`.

To audit a session for evidence of successful subagent delegation, inspect
that JSONL file for the same four signals used above:

1. Search for a top-level `Agent` (or equivalent dispatch) tool call and note
   its `subagent_type` and the following tool result entry.
2. Check the session's `modelUsage` for an entry on the subagent's pinned
   model (e.g. Haiku for `Explore`), confirming the subagent ran rather than
   being simulated by the top-level model.
3. Check that `permission_denials` is empty (or, if not empty, that no denial
   applies to the dispatch call being audited).
4. Compare the top-level session's final output against the subagent's tool
   result to confirm the report was relayed rather than altered or invented.

Do not use a real user's home directory path as a literal example when
writing about this procedure; use the `<cwd-encoded>` / `<session_id>`
placeholders shown above.
