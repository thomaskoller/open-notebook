# ADR-010: Own the podcast pipeline in-repo

- **Status**: Accepted
- **Date**: 2026-09-18
- **Related**: [ADR-002](ADR-002-external-libraries.md), [ADR-004](ADR-004-background-workers.md), [podcasts.md](../podcasts.md), [security.md](../security.md)

## Context

Podcast generation ran through `podcast-creator`, called as a single
`create_podcast()` invocation. An episode failed in production with:

```
Failed to parse ValidatedTranscript from completion {...}
transcript.2.dialogue  Field required
```

The transcript model had typed `"dialogle"` instead of `"dialogue"` in one of
eight dialogue items — a random slip that one re-sample fixes. The library
already wrapped each LLM call in a 3-attempt retry with parsing *inside* the
retried function, so it should have recovered. It did not, because the retry
used a **denylist** of "permanent" exceptions containing `ValueError`, and
LangChain declares `class OutputParserException(ValueError, LangChainException)`.
Every parse failure was therefore classified permanent and raised on the first
attempt, discarding the outline and every segment already generated and paid
for.

That was fixable upstream in a few lines. The reason we did not stop there is
what the investigation turned up around it:

- **The library validated a global config of every profile.** Generation went
  through `configure("episode_config", {...})` with *all* episode and speaker
  profiles, so one orphaned or unresolvable profile failed an unrelated
  episode. `commands/podcast_commands.py` carried ~80 lines whose only job was
  to rebuild that config and prune the broken entries on every run.
- **Validation rules disagreed.** `EpisodeProfile.num_segments` allows 3–20;
  the library capped it at 1–10, so a valid profile could not generate.
- **No progress visibility.** One `create_podcast()` call exposed no callback,
  so the UI got a single `generating_audio` message for a multi-minute run.
- **Failures were reported in-band**, as `"ERROR: ..."` strings in the result
  dict, which the command had to string-match to avoid reporting success for an
  episode with no audio.
- **`configure("templates", ...)` compiled caller strings as Jinja2 source** —
  the same SSTI shape as GHSA-f35w-wx37-26q7. Dormant, but a permanent
  footgun documented in `security.md`.
- **moviepy**, pulled in for audio concatenation, capped Pillow below 12.2.0,
  which has open advisories. That forced a `[tool.uv] override-dependencies`
  entry.
- The repo already owned the prompts (`prompts/podcast/*.jinja`), the profile
  models, model/credential resolution and the output paths. The library's
  remaining 2,559 lines were mostly a second copy of that machinery. The repo's
  template copies had also silently **lost the language block**, making
  `EpisodeProfile.language` a no-op.

## Decision

**Podcast orchestration lives in this repository**, as
`open_notebook/podcasts/pipeline.py`: outline call → per-segment transcript
calls → batched TTS → ffmpeg concat. The `podcast-creator` dependency is
removed.

This is not a reversal of [ADR-002](ADR-002-external-libraries.md). That ADR
delegates **platform/media integration** — provider APIs (esperanto) and
content extraction (content-core) — and that boundary is unchanged: the
pipeline reaches every model through `AIFactory`. What it delegated here was
prompt content and control flow, which is the knowledge layer, not integration.
The test is whether upstream would be doing heavy, ever-changing
platform-specific work on our behalf; for four sequential LLM/TTS calls
against our own prompts and our own profile models, it was not.

Specifics worth keeping fixed:

- **Retry classification is an allowlist, checked first**
  (`_is_transient`): parse failures (`OutputParserException`, pydantic
  `ValidationError`, `JSONDecodeError`) are always retried; our own
  `ConfigurationError` / `InvalidInputError` never are. A denylist cannot be
  written safely here, because the exception we most need to retry subclasses
  the one we most need to treat as permanent.
- **Stages are separate public functions**, not one orchestrator, so the Celery
  task persists the outline and transcript *before* TTS starts. An audio
  failure no longer discards the LLM output.
- **JSON object mode, not provider-enforced `json_schema`.** Schema mode makes
  esperanto `json.loads` the raw response, which breaks the reasoning models
  these prompts explicitly support (they emit `<think>` blocks that must be
  stripped first). The speaker names are still enforced locally, via a
  `Literal` in a per-episode schema.
- **ffmpeg for concatenation**, re-encoding rather than stream-copying, because
  per-speaker voice overrides mean two clips can come from different TTS
  providers. ffmpeg is already in the Dockerfile's `runtime-base` stage.
- **Failures raise.** No in-band error strings.

## Alternatives considered

- **Patch `_is_retryable` locally (monkeypatch) and keep the dependency** —
  ~8 lines, fixes the reported bug only, and leaves the global-config coupling,
  the validation mismatch, the missing progress reporting and the Pillow cap.
- **Upstream PR and wait for a release** — correct for the classification bug,
  but blocks the fix on someone else's release cadence and addresses none of
  the structural issues. Still worth sending; it costs us nothing now.
- **Vendor the library's files verbatim into the repo, then patch** — lower
  behavioral risk, but ~1,500 lines to maintain and it keeps the very
  indirection that produced the workarounds.

## Consequences

- `commands/podcast_commands.py` lost ~120 lines; podcast-creator, moviepy,
  pydub, imageio-ffmpeg and proglog left the lockfile, and Pillow now resolves
  to 12.3.0 with no override.
- ffmpeg becomes a hard runtime requirement for podcast audio (already present
  in the Docker images; a missing binary raises `ConfigurationError`).
- Prompt changes are now entirely ours: `prompts/podcast/*.jinja` is the only
  copy, with no upstream fallback to silently disagree with it.
- We own the failure modes we previously reported upstream. `tests/test_podcast_pipeline.py`
  pins the ones that caused this change, including the original malformed
  payload.
