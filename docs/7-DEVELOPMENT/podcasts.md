# Podcast Subsystem

How podcast generation is modeled and executed: the two-tier profile system, the model-registry references, the in-repo pipeline, and the deliberate no-auto-retry policy.

## Two-tier profile system (`open_notebook/podcasts/models.py`)

- **SpeakerProfile** — voice configuration: a `voice_model` (`record<model>` reference for TTS) plus 1–4 speakers (name, voice_id, backstory, personality). Individual speakers can override the profile's `voice_model`.
- **EpisodeProfile** — generation settings: `outline_llm` / `transcript_llm` (`record<model>` references), `language` (BCP 47, e.g. `pt-BR`), segment count (3–20), briefing template. It references a SpeakerProfile by name.
- **PodcastEpisode** — a generated episode. Links content, profiles and the async job (`command` field → `command` table RecordID).

## Model registry references, not strings

Profile fields reference `Model` records instead of raw provider/model strings. At generation time `_resolve_model_config(model_id)` loads the Model, resolves its linked credential (or falls back to `provision_provider_keys()`), and returns `(provider, model_name, config)` for the pipeline to hand to `AIFactory`.

The legacy string fields (`tts_provider`, `outline_provider`, …) that predated the registry were dropped by SQL migration 22 (#1107). The migration best-effort maps any still-unresolved profile to an existing `model` record (provider + name + type) before dropping the columns; profiles with no matching record stay unresolved — the UI already flags them as needing model selection and the user re-picks once. The old startup data migration (`open_notebook/podcasts/migration.py`) is gone.

## Profile snapshots

`PodcastEpisode` stores `episode_profile` and `speaker_profile` as **dicts (snapshots)**, not references. Editing a profile never retroactively changes past episodes — that's intentional. Corollary: deleting a profile does not cascade to episodes.

## The pipeline (`open_notebook/podcasts/pipeline.py`)

Generation is four steps, owned in-repo since [ADR-010](decisions/ADR-010-own-podcast-pipeline.md):

1. `generate_outline()` — one LLM call, rendering `prompts/podcast/outline.jinja`.
2. `generate_transcript()` — one LLM call **per outline segment**, each one seeing the transcript so far. Turns per segment come from its size (short 3 / medium 6 / long 10).
3. `synthesize_audio()` — one TTS clip per dialogue line, in concurrent batches of `TTS_BATCH_SIZE` (default 5), written as `clips/%04d.mp3`.
4. ffmpeg concatenates the clips into `audio/<episode>.mp3`, re-encoding (not stream-copying) because per-speaker voice overrides can mix TTS providers.

They are three separate functions, not one call, so `commands/podcast_commands.py` can persist the outline and the transcript **before** TTS starts and report progress between stages.

### Malformed model output is retried, not fatal

Both LLM stages run in JSON object mode and parse with a `PydanticOutputParser`, wrapped in a retry whose classifier (`_is_transient`) is an **allowlist checked first**: `OutputParserException`, pydantic `ValidationError` and `JSONDecodeError` are always retried, our own `ConfigurationError` / `InvalidInputError` never are. This shape is deliberate — LangChain's `OutputParserException` subclasses `ValueError`, so a denylist of "permanent" exception types silently swallows exactly the failure that most needs a re-sample. Tune with `PODCAST_RETRY_MAX_ATTEMPTS` / `PODCAST_RETRY_WAIT_MULTIPLIER`.

The transcript schema is built per episode with the speaker names as a `Literal`, so they reach the model in the prompt's format instructions and a hallucinated speaker is rejected (and retried) instead of failing later in the voice lookup.

### Prompts

`prompts/podcast/outline.jinja` and `transcript.jinja` are the only copies — there is no upstream fallback. Both take `language` (the resolved English language name, e.g. `pt-BR` → `Portuguese`) and skip their language block when it is absent.

## Job lifecycle and the retry policy

Generation runs as a `generate_podcast` task on the Celery worker:

- The command validates that `outline_llm`, `transcript_llm` and `voice_model` are set and pre-resolves the three model configs (so bad credentials fail before any tokens are spent), then runs the three pipeline stages.
- **`max_attempts: 1` — no automatic retries.** A mid-generation retry would create duplicate episode records (records are created during execution). Failed episodes are marked `failed` with an error message; retry is explicitly user-initiated via `POST /podcasts/episodes/{id}/retry`.
- Status tracking: `get_job_status()` / `get_job_detail()` read the `command` table and return `"unknown"` on failure rather than raising. Listing endpoints use the batched `get_job_details_for_commands()` so N episodes cost one status query, not N.
- A TTS or ffmpeg failure that survives its retries fails the episode — but the outline and transcript are already persisted, so the text is never lost with it.
