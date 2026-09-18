import time
import uuid
from pathlib import Path
from typing import Optional

from langchain_core.exceptions import OutputParserException
from loguru import logger

from open_notebook.celery_app import (
    TaskInput,
    TaskOutput,
    async_task,
    report_progress,
)
from open_notebook.config import PODCASTS_FOLDER
from open_notebook.database.repository import ensure_record_id
from open_notebook.podcasts.audio_paths import to_relative_audio_path
from open_notebook.podcasts.models import (
    EpisodeProfile,
    PodcastEpisode,
    SpeakerProfile,
)
from open_notebook.podcasts.pipeline import (
    generate_outline,
    generate_transcript,
    synthesize_audio,
)
from open_notebook.utils.model_utils import full_model_dump


def build_episode_output_dir(podcasts_folder: str = PODCASTS_FOLDER) -> tuple[str, Path]:
    """Build a filesystem-safe output directory path for a podcast episode.

    Uses a UUID as the directory name so the path is safe regardless of
    what the user typed as episode name (spaces, special chars, etc.).

    Builds under PODCASTS_FOLDER — the same root to_relative_audio_path()
    validates against at write time (#1030) — so the two can't drift apart.

    Returns:
        A tuple of (episode_dir_name, output_dir_path).
    """
    episode_dir_name = str(uuid.uuid4())
    output_dir = Path(podcasts_folder) / "episodes" / episode_dir_name
    return episode_dir_name, output_dir


class PodcastGenerationInput(TaskInput):
    episode_profile: str
    # Speaker profile record ID or name (the API boundary resolves the
    # user-facing name to a record ID before submitting; both are accepted
    # here for robustness).
    speaker_profile: Optional[str] = None
    episode_name: str
    content: str
    briefing_suffix: Optional[str] = None


class PodcastGenerationOutput(TaskOutput):
    success: bool
    episode_id: Optional[str] = None
    audio_file_path: Optional[str] = None
    transcript: Optional[dict] = None
    outline: Optional[dict] = None
    processing_time: float
    error_message: Optional[str] = None


# No automatic retry on purpose: a retry would generate a *second* episode
# (and burn a second round of TTS credits). Recovery is the explicit
# POST /podcasts/episodes/{id}/retry endpoint. See ADR-004.
@async_task("generate_podcast", max_retries=0)
async def generate_podcast_command(
    input_data: PodcastGenerationInput,
) -> PodcastGenerationOutput:
    """
    Generate a podcast episode from an Episode Profile.

    Runs outline -> transcript -> audio through open_notebook.podcasts.pipeline,
    persisting each stage as it completes.
    """
    start_time = time.time()
    command_id = (
        input_data.execution_context.command_id
        if input_data.execution_context
        else None
    )

    try:
        logger.info(
            f"Starting podcast generation for episode: {input_data.episode_name}"
        )
        await report_progress(command_id, message="resolving_profiles")
        logger.info(f"Using episode profile: {input_data.episode_profile}")

        # 1. Load Episode and Speaker profiles from SurrealDB
        episode_profile = await EpisodeProfile.get_by_name(input_data.episode_profile)
        if not episode_profile:
            raise ValueError(
                f"Episode profile '{input_data.episode_profile}' not found"
            )

        # Honor the explicitly requested speaker profile when provided,
        # falling back to the episode profile's configured speaker
        # (a speaker_profile record ID since migration 20, None when the
        # referenced profile no longer exists).
        speaker_ref = input_data.speaker_profile or episode_profile.speaker_config
        if not speaker_ref:
            raise ValueError(
                f"Episode profile '{episode_profile.name}' has no speaker "
                "profile configured. Please update the profile to select a "
                "speaker profile."
            )
        speaker_profile = await SpeakerProfile.resolve(speaker_ref)
        if not speaker_profile:
            if input_data.speaker_profile:
                raise ValueError(f"Speaker profile '{speaker_ref}' not found")
            raise ValueError(
                f"Episode profile '{episode_profile.name}' references a "
                "speaker profile that no longer exists. Please update the "
                "profile to select a speaker profile."
            )

        logger.info(f"Loaded episode profile: {episode_profile.name}")
        logger.info(f"Loaded speaker profile: {speaker_profile.name}")

        # 2. Validate that model registry fields are populated
        if not episode_profile.outline_llm:
            raise ValueError(
                f"Episode profile '{episode_profile.name}' has no outline model configured. "
                "Please update the profile to select an outline model."
            )
        if not episode_profile.transcript_llm:
            raise ValueError(
                f"Episode profile '{episode_profile.name}' has no transcript model configured. "
                "Please update the profile to select a transcript model."
            )
        if not speaker_profile.voice_model:
            raise ValueError(
                f"Speaker profile '{speaker_profile.name}' has no voice model configured. "
                "Please update the profile to select a voice model."
            )

        # 3. Resolve model configs with credentials
        outline_provider, outline_model_name, outline_config = (
            await episode_profile.resolve_outline_config()
        )
        transcript_provider, transcript_model_name, transcript_config = (
            await episode_profile.resolve_transcript_config()
        )
        tts_provider, tts_model_name, tts_config = (
            await speaker_profile.resolve_tts_config()
        )

        logger.info(
            f"Resolved models - outline: {outline_provider}/{outline_model_name}, "
            f"transcript: {transcript_provider}/{transcript_model_name}, "
            f"tts: {tts_provider}/{tts_model_name}"
        )

        # 4. Generate briefing
        briefing = episode_profile.default_briefing
        if input_data.briefing_suffix:
            briefing += f"\n\nAdditional instructions: {input_data.briefing_suffix}"

        # Create the record for the episode and associate with the ongoing command
        episode = PodcastEpisode(
            name=input_data.episode_name,
            episode_profile=full_model_dump(episode_profile.model_dump()),
            speaker_profile=full_model_dump(speaker_profile.model_dump()),
            command=ensure_record_id(command_id) if command_id else None,
            briefing=briefing,
            content=input_data.content,
            audio_file=None,
            transcript=None,
            outline=None,
        )
        await episode.save()

        logger.info(f"Generated briefing (length: {len(briefing)} chars)")

        # 5. Create output directory using UUID for filesystem-safe paths
        episode_dir_name, output_dir = build_episode_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Created output directory: {output_dir}")

        # 6. Generate the podcast: outline -> transcript -> audio. Each stage
        # is persisted the moment it completes, so a TTS or ffmpeg failure
        # leaves the (expensive) LLM output on the record instead of
        # discarding it.
        logger.info("Starting podcast generation")

        async def progress(
            message: str, current: Optional[int] = None, total: Optional[int] = None
        ) -> None:
            await report_progress(
                command_id, message=message, current=current, total=total
            )

        await progress("generating_outline")
        outline = await generate_outline(
            content=input_data.content,
            briefing=briefing,
            episode_profile=episode_profile,
            speaker_profile=speaker_profile,
        )
        episode.outline = full_model_dump(outline)
        await episode.save()

        transcript = await generate_transcript(
            content=input_data.content,
            briefing=briefing,
            outline=outline,
            episode_profile=episode_profile,
            speaker_profile=speaker_profile,
            on_progress=progress,
        )
        episode.transcript = {"transcript": full_model_dump(transcript)}
        await episode.save()

        audio_path = await synthesize_audio(
            transcript=transcript,
            speaker_profile=speaker_profile,
            output_dir=output_dir,
            episode_name=episode_dir_name,
            on_progress=progress,
        )

        # Store the audio path RELATIVE to PODCASTS_FOLDER (#1030). The
        # validation inside to_relative_audio_path guarantees the DB never
        # holds an absolute or root-escaping value; a violation raises
        # ValueError, which marks the job permanently failed (no retry).
        audio_file_rel = to_relative_audio_path(audio_path)
        episode.audio_file = audio_file_rel
        await episode.save()

        processing_time = time.time() - start_time
        logger.info(
            f"Successfully generated podcast episode: {episode.id} in {processing_time:.2f}s"
        )

        return PodcastGenerationOutput(
            success=True,
            episode_id=str(episode.id),
            audio_file_path=audio_file_rel,
            transcript={"transcript": full_model_dump(transcript)},
            outline=full_model_dump(outline),
            processing_time=processing_time,
        )

    except OutputParserException as e:
        # A malformed response that survived every retry. Handled before
        # ValueError (which OutputParserException subclasses) so the raw
        # LangChain dump doesn't reach the UI as-is.
        logger.error(f"Podcast generation failed to parse the model output: {e}")
        raise RuntimeError(
            "The model returned JSON that did not match the expected transcript "
            "format, and retrying did not help. Pick a different outline or "
            "transcript model in the episode profile."
        ) from e

    except ValueError:
        raise

    except Exception as e:
        logger.error(f"Podcast generation failed: {e}")
        logger.exception(e)

        error_msg = str(e)
        if "Invalid json output" in error_msg or "Expecting value" in error_msg:
            error_msg += (
                "\n\nNOTE: This error commonly occurs with GPT-5 models that use extended thinking. "
                "The model may be putting all output inside <think> tags, leaving nothing to parse. "
                "Try using gpt-4o, gpt-4o-mini, or gpt-4-turbo instead in your episode profile."
            )

        raise RuntimeError(error_msg) from e
