"""The podcast pipeline must survive a malformed model response.

Regression cover for the failure that motivated owning this pipeline: an
episode died because the transcript model typed `"dialogle"` instead of
`"dialogue"` in one dialogue item. podcast-creator classified the resulting
LangChain `OutputParserException` as permanent (its retry denylist contained
`ValueError`, which that exception subclasses), so the whole episode was thrown
away instead of the segment being re-sampled.
"""

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.exceptions import OutputParserException

from open_notebook.exceptions import ConfigurationError, InvalidInputError
from open_notebook.podcasts.models import EpisodeProfile, SpeakerProfile
from open_notebook.podcasts.pipeline import (
    Dialogue,
    Outline,
    _is_transient,
    generate_transcript,
    synthesize_audio,
)

SPEAKERS = ["Jamie Rodriguez", "Dr. Alex Chen"]

# The real payload from the incident: item 2 carries "dialogle".
MALFORMED = json.dumps(
    {
        "transcript": [
            {"speaker": "Jamie Rodriguez", "dialogue": "Let's start with the goal."},
            {"speaker": "Dr. Alex Chen", "dialogue": "That is where I want to press."},
            {"speaker": "Jamie Rodriguez", "dialogle": "Right, and that changes it."},
        ]
    }
)

VALID = json.dumps(
    {
        "transcript": [
            {"speaker": "Jamie Rodriguez", "dialogue": "Let's start with the goal."},
            {"speaker": "Dr. Alex Chen", "dialogue": "That is where I want to press."},
            {"speaker": "Jamie Rodriguez", "dialogue": "Right, and that changes it."},
        ]
    }
)

UNKNOWN_SPEAKER = json.dumps(
    {"transcript": [{"speaker": "Someone Else", "dialogue": "Hello."}]}
)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Keep the retry logic but drop the sleeps."""
    monkeypatch.setenv("PODCAST_RETRY_WAIT_MULTIPLIER", "0")


@pytest.fixture
def episode_profile():
    return EpisodeProfile(
        name="Test episode",
        default_briefing="Discuss the source",
        outline_llm="model:outline",
        transcript_llm="model:transcript",
        num_segments=3,
    )


@pytest.fixture
def speaker_profile():
    return SpeakerProfile(
        name="Two hosts",
        voice_model="model:voice",
        speakers=[
            {
                "name": name,
                "voice_id": f"voice-{i}",
                "backstory": "A host",
                "personality": "Curious",
            }
            for i, name in enumerate(SPEAKERS)
        ],
    )


@pytest.fixture
def outline():
    return Outline.model_validate(
        {"segments": [{"name": "Intro", "description": "Set up", "size": "short"}]}
    )


class _FakeLanguageModel:
    """Returns each scripted response in turn, and counts the calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def to_langchain(self):
        return self

    async def ainvoke(self, _prompt):
        self.calls += 1
        # Run off the end -> keep returning the last response.
        index = min(self.calls - 1, len(self.responses) - 1)
        return type("AIMessage", (), {"content": self.responses[index]})()


async def _run_transcript(fake, outline, episode_profile, speaker_profile):
    with (
        patch.object(
            EpisodeProfile,
            "resolve_transcript_config",
            AsyncMock(return_value=("openai", "gpt-test", {})),
        ),
        patch(
            "open_notebook.podcasts.pipeline.AIFactory.create_language",
            return_value=fake,
        ),
    ):
        # Awaited inside the patch context: the patches must still be in
        # place while the call actually runs.
        return await generate_transcript(
            content="Some source content",
            briefing="Discuss it",
            outline=outline,
            episode_profile=episode_profile,
            speaker_profile=speaker_profile,
        )


@pytest.mark.asyncio
async def test_malformed_response_is_retried(outline, episode_profile, speaker_profile):
    """The incident payload: a bad first response must not fail the episode."""
    fake = _FakeLanguageModel([MALFORMED, VALID])

    transcript = await _run_transcript(fake, outline, episode_profile, speaker_profile)

    assert fake.calls == 2, "the malformed response should have been re-sampled"
    assert [d.speaker for d in transcript] == [
        "Jamie Rodriguez",
        "Dr. Alex Chen",
        "Jamie Rodriguez",
    ]
    assert all(isinstance(d, Dialogue) and d.dialogue for d in transcript)


@pytest.mark.asyncio
async def test_retries_are_bounded(
    outline, episode_profile, speaker_profile, monkeypatch
):
    """A model that never recovers fails after the configured attempts."""
    monkeypatch.setenv("PODCAST_RETRY_MAX_ATTEMPTS", "3")
    fake = _FakeLanguageModel([MALFORMED])

    with pytest.raises(OutputParserException):
        await _run_transcript(fake, outline, episode_profile, speaker_profile)

    assert fake.calls == 3


@pytest.mark.asyncio
async def test_unknown_speaker_is_rejected(outline, episode_profile, speaker_profile):
    """A hallucinated speaker must never reach the voice lookup."""
    fake = _FakeLanguageModel([UNKNOWN_SPEAKER])

    with pytest.raises(OutputParserException):
        await _run_transcript(fake, outline, episode_profile, speaker_profile)


@pytest.mark.parametrize(
    "exc, transient",
    [
        (OutputParserException("Failed to parse"), True),
        (json.JSONDecodeError("bad", "{}", 0), True),
        (ConnectionError("reset by peer"), True),
        (ConfigurationError("no model configured"), False),
        (InvalidInputError("bad language code"), False),
        (KeyError("speaker"), False),
    ],
)
def test_error_classification(exc, transient):
    """Parse failures are transient; our own configuration errors are not.

    The denylist this replaces called every ValueError permanent, and
    OutputParserException subclasses ValueError.
    """
    assert _is_transient(exc) is transient


@pytest.mark.parametrize(
    "status_code, transient", [(401, False), (429, True), (503, True)]
)
def test_http_status_classification(status_code, transient):
    exc = type("ProviderError", (Exception,), {"status_code": status_code})()
    assert _is_transient(exc) is transient


# --- audio ------------------------------------------------------------------


def _tone(path: Path) -> None:
    """A real 0.4s mp3, so ffmpeg has something valid to concatenate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.4",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            "-y",
            str(path),
        ],
        check=True,
    )


class _FakeTTS:
    def __init__(self):
        self.voices_used = []

    async def agenerate_speech(self, text, voice, output_file):
        self.voices_used.append(voice)
        _tone(Path(output_file))


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_synthesize_audio_combines_every_clip(speaker_profile, tmp_path):
    transcript = [
        Dialogue(speaker=SPEAKERS[i % 2], dialogue=f"line {i}") for i in range(3)
    ]
    tts = _FakeTTS()

    with (
        patch.object(
            SpeakerProfile,
            "resolve_tts_config",
            AsyncMock(return_value=("openai", "tts-test", {})),
        ),
        patch(
            "open_notebook.podcasts.pipeline.AIFactory.create_text_to_speech",
            return_value=tts,
        ),
    ):
        audio = await synthesize_audio(
            transcript=transcript,
            speaker_profile=speaker_profile,
            output_dir=tmp_path,
            episode_name="episode",
            on_progress=None,
        )

    assert audio.exists() and audio.stat().st_size > 0
    assert tts.voices_used == ["voice-0", "voice-1", "voice-0"]
    duration = float(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(audio),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    # All three clips, not just the last one: 3 x 0.4s.
    assert duration == pytest.approx(1.2, abs=0.15)


@pytest.mark.asyncio
async def test_audio_requires_a_configured_voice(speaker_profile, tmp_path):
    """A speaker with no voice fails loudly instead of raising KeyError."""
    with patch.object(
        SpeakerProfile,
        "resolve_tts_config",
        AsyncMock(return_value=("openai", "tts-test", {})),
    ):
        with pytest.raises(ConfigurationError, match="no voice configured"):
            await synthesize_audio(
                transcript=[Dialogue(speaker="Ghost", dialogue="hi")],
                speaker_profile=speaker_profile,
                output_dir=tmp_path,
                episode_name="episode",
            )


@pytest.mark.asyncio
async def test_empty_transcript_is_rejected(speaker_profile, tmp_path):
    with pytest.raises(InvalidInputError):
        await synthesize_audio(
            transcript=[],
            speaker_profile=speaker_profile,
            output_dir=tmp_path,
            episode_name="episode",
        )


# --- prompts ----------------------------------------------------------------
#
# The repo's `prompts/podcast/*.jinja` are the only copies, so nothing upstream
# catches a variable the pipeline stopped passing. That is not hypothetical:
# these templates silently lost their `{% if language %}` block, which made
# EpisodeProfile.language a no-op until it was noticed.


def _render(template, parser, data):
    from ai_prompter import Prompter

    return Prompter(prompt_template=template, parser=parser).render(data)


def test_outline_prompt_renders_every_variable():
    from langchain_core.output_parsers.pydantic import PydanticOutputParser

    from open_notebook.podcasts.pipeline import Outline

    parser: PydanticOutputParser = PydanticOutputParser(pydantic_object=Outline)
    data = {
        "briefing": "Discuss the source",
        "num_segments": 4,
        "context": "Some source text",
        "speakers": [
            {"name": n, "voice_id": "v", "backstory": "b", "personality": "p"}
            for n in SPEAKERS
        ],
        "language": "Portuguese",
    }

    prompt = _render("podcast/outline", parser, data)

    assert "Discuss the source" in prompt
    assert "4 main segments" in prompt, "num_segments must reach the template"
    assert "Portuguese" in prompt, "the language block must be rendered"
    assert '"segments"' in prompt, "format_instructions must be injected"

    without_language = _render("podcast/outline", parser, {**data, "language": None})
    assert "LANGUAGE INSTRUCTION" not in without_language


@pytest.mark.parametrize(
    "speaker_count, marker", [(1, "SOLO podcast"), (2, "turns of messages")]
)
def test_transcript_prompt_renders_every_variable(outline, speaker_count, marker):
    from langchain_core.output_parsers.pydantic import PydanticOutputParser

    from open_notebook.podcasts.pipeline import _transcript_schema

    names = SPEAKERS[:speaker_count]
    parser: PydanticOutputParser = PydanticOutputParser(
        pydantic_object=_transcript_schema(names)
    )
    prompt = _render(
        "podcast/transcript",
        parser,
        {
            "briefing": "Discuss the source",
            "outline": outline,
            "context": ["piece one", "piece two"],
            "segment": outline.segments[0],
            "is_final": True,
            "turns": 6,
            "speakers": [
                {"name": n, "voice_id": "v", "backstory": "b", "personality": "p"}
                for n in names
            ],
            "speaker_names": names,
            "transcript": [Dialogue(speaker=names[0], dialogue="said already")],
            "language": "Portuguese",
        },
    )

    assert marker in prompt, "the speaker-count branch must render"
    assert "piece one" in prompt and "piece two" in prompt, "list content must render"
    assert "said already" in prompt, "the transcript so far must be included"
    assert "at least 6" in prompt
    assert "final segment" in prompt
    assert "Portuguese" in prompt
    # The valid speaker names must reach the model, via the schema.
    instructions = prompt.split("Formatting instructions:")[1]
    assert all(name in instructions for name in names)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile_max_tokens, expected",
    [(None, 5000), (12000, 12000)],
    ids=["stage default", "profile override"],
)
async def test_transcript_output_budget(
    outline, speaker_profile, profile_max_tokens, expected
):
    """Each stage sets an output budget; the profile's value wins when set.

    esperanto defaults max_tokens to 850, which truncates a segment
    mid-sentence -- and a truncated response is unparseable JSON, so this is
    not a cosmetic default.
    """
    profile = EpisodeProfile(
        name="Test episode",
        default_briefing="Discuss the source",
        outline_llm="model:outline",
        transcript_llm="model:transcript",
        num_segments=3,
        max_tokens=profile_max_tokens,
    )
    # _resolve_model_config only puts max_tokens in the config when the
    # profile sets one.
    resolved_config = (
        {} if profile_max_tokens is None else {"max_tokens": profile_max_tokens}
    )
    captured = {}

    def create_language(_provider, _model, config):
        captured.update(config)
        return _FakeLanguageModel([VALID])

    with (
        patch.object(
            EpisodeProfile,
            "resolve_transcript_config",
            AsyncMock(return_value=("openai", "gpt-test", resolved_config)),
        ),
        patch(
            "open_notebook.podcasts.pipeline.AIFactory.create_language",
            side_effect=create_language,
        ),
    ):
        await generate_transcript(
            content="Some source content",
            briefing="Discuss it",
            outline=outline,
            episode_profile=profile,
            speaker_profile=speaker_profile,
        )

    assert captured["max_tokens"] == expected
    assert captured["structured"] == {"type": "json"}
