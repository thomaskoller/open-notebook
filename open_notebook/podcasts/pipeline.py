"""Podcast generation: outline -> transcript -> TTS clips -> one mp3.

Replaces the podcast-creator dependency (see
docs/7-DEVELOPMENT/decisions/ADR-010-own-podcast-pipeline.md). The three stages
are separate public functions rather than one orchestrator, so the Celery task
can persist the expensive LLM output *before* TTS starts and report progress
between stages.

Everything here is provider-agnostic: models come from the episode/speaker
profiles via the existing `resolve_*_config()` helpers, and prompts come from
`prompts/podcast/*.jinja`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Type,
    Union,
)

import pycountry
from ai_prompter import Prompter
from esperanto import AIFactory
from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers.pydantic import PydanticOutputParser
from loguru import logger
from pydantic import BaseModel, Field, ValidationError, create_model
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from open_notebook.exceptions import ConfigurationError, InvalidInputError
from open_notebook.podcasts.models import (
    EpisodeProfile,
    SpeakerProfile,
    _resolve_model_config,
)
from open_notebook.utils.text_utils import clean_thinking_content, extract_text_content

Content = Union[str, List[str]]
# Mirrors `report_progress(message=, current=, total=)`. `message` is an i18n
# key rendered by the jobs drawer, so it must stay static -- counts go through
# current/total, never into the message text.
ProgressCallback = Callable[[str, Optional[int], Optional[int]], Awaitable[Any]]

# Dialogue turns to ask for, by segment size.
_TURNS_BY_SIZE = {"short": 3, "medium": 6, "long": 10}

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_WAIT_MULTIPLIER = 2.0

# Output budget per stage when the episode profile sets no `max_tokens`.
# esperanto's own default is 850 tokens, which truncates a transcript segment
# mid-sentence; a truncated response is then unparseable JSON.
_OUTLINE_MAX_TOKENS = 3000
_TRANSCRIPT_MAX_TOKENS = 5000


# ---------------------------------------------------------------------------
# Schemas (the shapes stored on `episode.outline` / `episode.transcript`)
# ---------------------------------------------------------------------------


class Segment(BaseModel):
    name: str = Field(..., description="Name of the segment")
    description: str = Field(..., description="Description of the segment")
    size: Literal["short", "medium", "long"] = Field(
        default="medium", description="Size of the segment"
    )


class Outline(BaseModel):
    segments: List[Segment] = Field(..., description="List of segments")


class Dialogue(BaseModel):
    speaker: str = Field(..., description="Speaker name")
    dialogue: str = Field(..., description="Dialogue")


class Transcript(BaseModel):
    transcript: List[Dialogue] = Field(..., description="Transcript")


def _transcript_schema(speaker_names: Sequence[str]) -> Type[BaseModel]:
    """A Transcript schema with the episode's speaker names baked in.

    The names land in the prompt's format instructions and in local validation,
    so a hallucinated speaker is rejected (and retried) instead of reaching the
    voice lookup in `synthesize_audio` as a KeyError.
    """
    speaker_type: Any = Literal[tuple(speaker_names)]  # type: ignore[valid-type]
    dialogue = create_model(
        "Dialogue",
        speaker=(speaker_type, Field(..., description="Speaker name")),
        dialogue=(str, Field(..., description="Dialogue")),
    )
    return create_model(
        "Transcript",
        transcript=(List[dialogue], Field(..., description="Transcript")),  # type: ignore[valid-type]
    )


# ---------------------------------------------------------------------------
# Retry classification
# ---------------------------------------------------------------------------

# Checked FIRST, and deliberately an allowlist. podcast-creator denylisted
# `ValueError` as "permanent", and LangChain declares
# `class OutputParserException(ValueError, ...)` -- so every malformed-JSON
# response was misread as permanent and killed the whole episode on the first
# attempt, even though re-sampling fixes it. Naming the parse family
# explicitly makes that mistake impossible to repeat here.
_PARSE_ERRORS: tuple = (OutputParserException, ValidationError, json.JSONDecodeError)

# Our own errors: a misconfigured profile will not fix itself on attempt 2.
_PERMANENT_ERRORS: tuple = (
    ConfigurationError,
    InvalidInputError,
    AssertionError,
    TypeError,
    KeyError,
)


def _is_transient(exc: BaseException) -> bool:
    """Whether `exc` is worth another attempt."""
    if isinstance(exc, _PARSE_ERRORS):
        return True
    if isinstance(exc, _PERMANENT_ERRORS):
        return False
    # Most provider SDKs expose HTTP status this way. 4xx is permanent, except
    # 429, which is exactly what backoff is for.
    status_code = getattr(exc, "status_code", None)
    if status_code is not None and 400 <= status_code < 500 and status_code != 429:
        return False
    # Anything left is network/provider flakiness.
    return True


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    wait = state.next_action.sleep if state.next_action else 0
    logger.warning(
        f"Podcast step failed (attempt {state.attempt_number}) with "
        f"{type(exc).__name__}: {exc}. Retrying in {wait:.1f}s."
    )


def _max_attempts() -> int:
    raw = os.getenv("PODCAST_RETRY_MAX_ATTEMPTS")
    return int(raw) if raw else _DEFAULT_MAX_ATTEMPTS


def _wait_multiplier() -> float:
    """Backoff multiplier in seconds. 0 disables the wait (used by tests)."""
    raw = os.getenv("PODCAST_RETRY_WAIT_MULTIPLIER")
    return float(raw) if raw else _DEFAULT_WAIT_MULTIPLIER


def _retrying() -> Any:
    return retry(
        stop=stop_after_attempt(_max_attempts()),
        wait=wait_exponential(multiplier=_wait_multiplier(), max=30),
        retry=retry_if_exception(_is_transient),
        before_sleep=_log_retry,
        reraise=True,
    )


# ---------------------------------------------------------------------------
# LLM stages
# ---------------------------------------------------------------------------


async def _call_json(
    *,
    provider: str,
    model_name: str,
    config: Dict[str, Any],
    prompt: str,
    parser: PydanticOutputParser,
    max_tokens: int,
) -> Any:
    """One JSON-returning LLM call, parsed and validated, with retries.

    Stays in JSON *object* mode rather than provider-enforced json_schema:
    schema mode makes esperanto `json.loads` the raw response, which breaks the
    reasoning models these prompts explicitly support (they emit `<think>`
    blocks that have to be stripped first).
    """
    # `config` goes last: an episode profile's explicit max_tokens wins.
    model = AIFactory.create_language(
        provider,
        model_name,
        config={"max_tokens": max_tokens, "structured": {"type": "json"}, **config},
    ).to_langchain()

    @_retrying()
    async def _once() -> Any:
        result = await model.ainvoke(prompt)
        text = clean_thinking_content(extract_text_content(result.content))
        return parser.invoke(text)

    return await _once()


async def generate_outline(
    *,
    content: Content,
    briefing: str,
    episode_profile: EpisodeProfile,
    speaker_profile: SpeakerProfile,
) -> Outline:
    """Generate the episode outline."""
    provider, model_name, config = await episode_profile.resolve_outline_config()
    parser: PydanticOutputParser = PydanticOutputParser(pydantic_object=Outline)
    prompt = Prompter(prompt_template="podcast/outline", parser=parser).render(  # type: ignore[arg-type]
        {
            "briefing": briefing,
            "num_segments": episode_profile.num_segments,
            "context": content,
            "speakers": speaker_profile.speakers,
            "language": _language_name(episode_profile.language),
        }
    )
    outline = await _call_json(
        provider=provider,
        model_name=model_name,
        config=config,
        prompt=prompt,
        parser=parser,
        max_tokens=_OUTLINE_MAX_TOKENS,
    )
    logger.info(f"Generated outline with {len(outline.segments)} segments")
    return outline


async def generate_transcript(
    *,
    content: Content,
    briefing: str,
    outline: Outline,
    episode_profile: EpisodeProfile,
    speaker_profile: SpeakerProfile,
    on_progress: Optional[ProgressCallback] = None,
) -> List[Dialogue]:
    """Generate the transcript, one LLM call per outline segment."""
    provider, model_name, config = await episode_profile.resolve_transcript_config()
    speaker_names = [s["name"] for s in speaker_profile.speakers]
    parser: PydanticOutputParser = PydanticOutputParser(
        pydantic_object=_transcript_schema(speaker_names)
    )
    language = _language_name(episode_profile.language)

    transcript: List[Dialogue] = []
    total = len(outline.segments)
    for index, segment in enumerate(outline.segments):
        logger.info(f"Transcript segment {index + 1}/{total}: {segment.name}")
        if on_progress:
            await on_progress("generating_transcript", index + 1, total)

        prompt = Prompter(prompt_template="podcast/transcript", parser=parser).render(  # type: ignore[arg-type]
            {
                "briefing": briefing,
                "outline": outline,
                "context": content,
                "segment": segment,
                "is_final": index == total - 1,
                "turns": _TURNS_BY_SIZE.get(segment.size, 6),
                "speakers": speaker_profile.speakers,
                "speaker_names": speaker_names,
                "transcript": transcript,
                "language": language,
            }
        )
        result = await _call_json(
            provider=provider,
            model_name=model_name,
            config=config,
            prompt=prompt,
            parser=parser,
            max_tokens=_TRANSCRIPT_MAX_TOKENS,
        )
        # The dynamic schema's items are structurally Dialogue; re-validate so
        # callers always get this module's type.
        transcript.extend(
            Dialogue.model_validate(d.model_dump()) for d in result.transcript
        )

    logger.info(f"Generated transcript with {len(transcript)} dialogue lines")
    return transcript


def _language_name(language_code: Optional[str]) -> Optional[str]:
    """Resolve a BCP 47 / ISO 639 code to an English language name.

    Returns None for a blank code (the templates then skip their language
    block). An unrecognized code is a profile problem, not a transient one.
    """
    if not language_code or not language_code.strip():
        return None

    code = language_code.strip().split("-")[0].lower()
    lang = pycountry.languages.get(alpha_2=code) or pycountry.languages.get(
        alpha_3=code
    )
    if lang is None:
        raise InvalidInputError(
            f"Invalid language code '{language_code}' on the episode profile. "
            "Use ISO 639-1 (e.g. 'pt') or BCP 47 (e.g. 'pt-BR')."
        )
    # pycountry returns compound names like "Spanish; Castilian".
    return lang.name.split(";")[0].strip()


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Voice:
    provider: str
    model: str
    config: Dict[str, Any]
    voice_id: str


async def _resolve_voices(speaker_profile: SpeakerProfile) -> Dict[str, _Voice]:
    """Per-speaker TTS settings, falling back to the profile's own model."""
    provider, model, config = await speaker_profile.resolve_tts_config()

    voices: Dict[str, _Voice] = {}
    for speaker in speaker_profile.speakers:
        name = speaker["name"]
        resolved = (provider, model, config)
        if speaker.get("voice_model"):
            try:
                resolved = await _resolve_model_config(str(speaker["voice_model"]))
            except Exception as e:
                logger.warning(
                    f"Failed to resolve the voice override for speaker '{name}', "
                    f"using the speaker profile's model instead: {e}"
                )
        voices[name] = _Voice(
            resolved[0], resolved[1], dict(resolved[2]), speaker["voice_id"]
        )
    return voices


async def _generate_clip(voice: _Voice, text: str, path: Path) -> Path:
    config = dict(voice.config)
    api_key = config.pop("api_key", None)
    base_url = config.pop("base_url", None)
    tts = AIFactory.create_text_to_speech(
        voice.provider, voice.model, api_key=api_key, base_url=base_url, **config
    )

    @_retrying()
    async def _once() -> Path:
        await tts.agenerate_speech(text=text, voice=voice.voice_id, output_file=path)
        return path

    return await _once()


async def synthesize_audio(
    *,
    transcript: Sequence[Dialogue],
    speaker_profile: SpeakerProfile,
    output_dir: Path,
    episode_name: str,
    on_progress: Optional[ProgressCallback] = None,
) -> Path:
    """Render every line to a clip, then combine them into one mp3."""
    if not transcript:
        raise InvalidInputError("Cannot synthesize audio from an empty transcript")

    voices = await _resolve_voices(speaker_profile)
    missing = {line.speaker for line in transcript} - set(voices)
    if missing:
        raise ConfigurationError(
            f"Transcript uses speakers with no voice configured: {sorted(missing)}. "
            f"Speaker profile '{speaker_profile.name}' defines: {sorted(voices)}."
        )

    clips_dir = output_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    # Providers rate-limit hard, so clips go out in small concurrent batches.
    batch_size = max(1, int(os.getenv("TTS_BATCH_SIZE", "5")))
    total = len(transcript)
    lines = list(enumerate(transcript))
    clips: List[Path] = []

    for start in range(0, total, batch_size):
        batch = lines[start : start + batch_size]
        logger.info(
            f"Generating audio clips {start + 1}-{start + len(batch)} of {total}"
        )
        if on_progress:
            await on_progress("generating_audio", start + 1, total)

        clips.extend(
            await asyncio.gather(
                *(
                    _generate_clip(
                        voices[line.speaker], line.dialogue, clips_dir / f"{i:04d}.mp3"
                    )
                    for i, line in batch
                )
            )
        )
        if start + batch_size < total:
            await asyncio.sleep(1)

    if on_progress:
        await on_progress("combining_audio", None, None)

    audio_dir = output_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    target = audio_dir / f"{episode_name}.mp3"
    await _combine_clips(clips, target)
    logger.info(f"Combined {len(clips)} clips into {target}")
    return target


async def _combine_clips(clips: Sequence[Path], target: Path) -> None:
    """Concatenate mp3 clips with ffmpeg.

    Re-encodes rather than stream-copying: per-speaker voice overrides mean two
    clips can come from different TTS providers, and stream-copying mismatched
    sample rates produces a subtly broken file.
    """
    if shutil.which("ffmpeg") is None:
        raise ConfigurationError(
            "ffmpeg is required to combine podcast audio but was not found on PATH."
        )

    # The list file goes in the clips directory and names its entries bare
    # (`0000.mp3`): the concat demuxer resolves relative entries against the
    # list file's own directory, and every name in it is one we generated, so
    # there is nothing to quote or escape.
    clips_dir = Path(clips[0]).parent
    listing = clips_dir / "concat.txt"
    listing.write_text("".join(f"file '{Path(c).name}'\n" for c in clips))

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed to combine {len(clips)} audio clips "
            f"(exit {process.returncode}): {stderr.decode(errors='replace')[-2000:]}"
        )
