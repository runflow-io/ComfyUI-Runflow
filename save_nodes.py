"""Encoding-bridge nodes: ComfyUI AUDIO / VIDEO / IMAGE sockets → file on disk
under ``output/`` → STRING filename emitted on the output socket.

The filename plugs straight into :class:`io_nodes.RunflowOutputFile`, which
announces the file under the worker's ``files`` artifact bucket so any audio /
video / 3D / archive deliverable rides the same R2 upload path as images.

These nodes are *not* part of the Runflow deploy I/O contract — they carry no
``RUNFLOW_IO`` / ``RUNFLOW_TYPE`` attrs, the deploy worker's manifest extractor
doesn't look at them, and they're invisible to ``services.comfyui_io_extraction``
on the API side. They're plain ComfyUI utility nodes that happen to ship with
this plugin for ergonomic reasons.

Format coverage mirrors stock ComfyUI's `Save Audio (FLAC/MP3/Opus)` and
`Save Video` / `Save WEBM` family. The audio nodes delegate to
``comfy_api.latest._ui.AudioSaveHelper.save_audio`` so encoding stays in lockstep
with whatever ComfyUI ships. The MP4 video node calls ``video.save_to(...)`` on
the VIDEO socket (only MP4/H264 is supported there today). The WEBM node takes an
IMAGE batch + fps + codec like stock ``SaveWEBM`` — the VIDEO socket's
``save_to`` doesn't expose a WEBM container yet, so we encode frames ourselves
with PyAV, mirroring the stock node's loop.

Each node ``OUTPUT_NODE = True`` so it fires standalone (and shows the preview
pane in ComfyUI's UI) AND emits the relative filename on its STRING output,
ready to feed ``RunflowOutputFile``.
"""

from __future__ import annotations

import logging
import os
from fractions import Fraction
from typing import Any

import folder_paths

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature detection: register only what the running ComfyUI can actually run.
# ---------------------------------------------------------------------------

try:
    from comfy_api.latest._ui import AudioSaveHelper, FolderType  # type: ignore[import-not-found]
    _AUDIO_HELPER_AVAILABLE = True
except ImportError:
    AudioSaveHelper = None  # type: ignore[assignment,misc]
    FolderType = None  # type: ignore[assignment,misc]
    _AUDIO_HELPER_AVAILABLE = False
    logger.warning(
        "Runflow: comfy_api.latest._ui.AudioSaveHelper unavailable; "
        "Runflow Save Audio nodes will not be registered."
    )

try:
    from comfy_api.latest._util import VideoCodec, VideoContainer  # type: ignore[import-not-found]
    _VIDEO_TYPES_AVAILABLE = True
except ImportError:
    VideoCodec = None  # type: ignore[assignment,misc]
    VideoContainer = None  # type: ignore[assignment,misc]
    _VIDEO_TYPES_AVAILABLE = False
    logger.warning(
        "Runflow: comfy_api.latest._util.{VideoContainer,VideoCodec} unavailable; "
        "RunflowSaveVideoMP4 will not be registered."
    )

try:
    import av  # type: ignore[import-not-found]
    _PYAV_AVAILABLE = True
except ImportError:
    av = None  # type: ignore[assignment,misc]
    _PYAV_AVAILABLE = False
    logger.warning(
        "Runflow: PyAV (`av`) unavailable; RunflowSaveVideoWEBM will not be registered."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _relative_path(filename: str, subfolder: str) -> str:
    """Compose the ``{subfolder}/{filename}`` form ``RunflowOutputFile`` expects.
    Empty subfolder collapses to just the basename so the downstream node's
    path-traversal guard doesn't trip on a leading ``/``."""
    return f"{subfolder}/{filename}" if subfolder else filename


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def _make_audio_save_class(
    *,
    format: str,
    quality_options: list[str] | None,
    default_quality: str | None,
    class_suffix: str,
    display_format: str,
) -> type:
    """Build one ``RunflowSaveAudio<Format>`` class.

    Format-specific bits (FLAC has no quality; MP3 has ``V0``/``128k``/``320k``;
    Opus has bitrate kbps options) are parameters; everything else is identical
    across the three audio classes so a closure is cheaper than three near-copies.
    ``class_suffix`` is the camel-case piece of the ``__name__`` (e.g. ``"MP3"``)
    so it matches the registration key one-for-one.
    """

    has_quality = quality_options is not None

    def input_types(cls):
        inputs: dict[str, Any] = {
            "audio": ("AUDIO",),
            "filename_prefix": ("STRING", {"default": "runflow_audio"}),
        }
        if has_quality:
            inputs["quality"] = (quality_options, {"default": default_quality})
        return {"required": inputs}

    def save(self, audio, filename_prefix, quality=default_quality):
        results = AudioSaveHelper.save_audio(
            audio,
            filename_prefix=filename_prefix,
            folder_type=FolderType.output,
            cls=None,  # we're a legacy custom-node class, not a v3 ComfyNode; skips metadata embed
            format=format,
            quality=quality if has_quality else "128k",
        )
        if not results:
            raise RuntimeError(
                f"RunflowSaveAudio{class_suffix}: AudioSaveHelper returned no files."
            )

        batch_count = audio["waveform"].shape[0] if "waveform" in audio else len(results)
        if batch_count > 1:
            logger.warning(
                "Runflow Save Audio (%s): batch of %d items, returning first filename only "
                "(the rest are saved but not addressable via the STRING output).",
                display_format, batch_count,
            )

        first = results[0]
        return {
            "ui": {"audio": list(results)},
            "result": (_relative_path(first.filename, first.subfolder),),
        }

    return type(
        f"RunflowSaveAudio{class_suffix}",
        (),
        {
            "INPUT_TYPES": classmethod(input_types),
            "RETURN_TYPES": ("STRING",),
            "RETURN_NAMES": ("filename",),
            "FUNCTION": "save",
            "OUTPUT_NODE": True,
            "CATEGORY": "Runflow/Save",
            "save": save,
            "__doc__": (
                f"Saves a ComfyUI AUDIO input as {display_format} under ComfyUI's "
                f"output directory and emits the relative filename on the `filename` "
                f"socket. Wire `filename` into a Runflow Output (File) node to deliver."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Video — MP4 (VIDEO socket)
# ---------------------------------------------------------------------------


class RunflowSaveVideoMP4:
    """Saves a ComfyUI VIDEO input as MP4/H.264 under the output directory and
    emits the relative filename. Wire the `filename` socket into a Runflow Output
    (File) node to deliver."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("VIDEO",),
                "filename_prefix": ("STRING", {"default": "runflow_video"}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("filename",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "Runflow/Save"

    def save(self, video, filename_prefix):
        width, height = video.get_dimensions()
        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_output_directory(), width, height,
        )
        os.makedirs(full_output_folder, exist_ok=True)

        ext = VideoContainer.get_extension(VideoContainer.MP4)
        file = f"{filename}_{counter:05}_.{ext}"
        output_path = os.path.join(full_output_folder, file)

        video.save_to(output_path, format=VideoContainer.MP4, codec=VideoCodec.H264)

        return {
            "ui": {"images": [{"filename": file, "subfolder": subfolder, "type": "output"}]},
            "result": (_relative_path(file, subfolder),),
        }


# ---------------------------------------------------------------------------
# Video — WEBM (IMAGE batch, like stock SaveWEBM)
# ---------------------------------------------------------------------------


class RunflowSaveVideoWEBM:
    """Encodes an IMAGE batch as WebM (VP9 or AV1) at the given fps and emits the
    relative filename. Mirrors stock ComfyUI's `SaveWEBM` input shape — wire from
    an IMAGE source (e.g. a CreateVideo node's frames), not a VIDEO socket. Wire
    `filename` into a Runflow Output (File) node to deliver."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "filename_prefix": ("STRING", {"default": "runflow_video"}),
                "codec": (["vp9", "av1"], {"default": "vp9"}),
                "fps": ("FLOAT", {"default": 24.0, "min": 0.01, "max": 1000.0, "step": 0.01}),
                "crf": ("FLOAT", {"default": 32.0, "min": 0.0, "max": 63.0, "step": 1.0,
                                  "tooltip": "Higher = lower quality + smaller file. Lower = higher quality + larger file."}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("filename",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "Runflow/Save"

    def save(self, images, filename_prefix, codec, fps, crf):
        import torch  # local import — ComfyUI guarantees torch in the venv but our import block doesn't

        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_output_directory(), images[0].shape[1], images[0].shape[0],
        )
        os.makedirs(full_output_folder, exist_ok=True)

        file = f"{filename}_{counter:05}_.webm"
        output_path = os.path.join(full_output_folder, file)
        container = av.open(output_path, mode="w")

        codec_map = {"vp9": "libvpx-vp9", "av1": "libsvtav1"}
        stream = container.add_stream(codec_map[codec], rate=Fraction(round(fps * 1000), 1000))
        stream.width = images.shape[-2]
        stream.height = images.shape[-3]
        stream.pix_fmt = "yuv420p10le" if codec == "av1" else "yuv420p"
        stream.bit_rate = 0
        stream.options = {"crf": str(crf)}
        if codec == "av1":
            stream.options["preset"] = "6"

        for frame in images:
            video_frame = av.VideoFrame.from_ndarray(
                torch.clamp(frame[..., :3] * 255, min=0, max=255)
                .to(device=torch.device("cpu"), dtype=torch.uint8)
                .numpy(),
                format="rgb24",
            )
            for packet in stream.encode(video_frame):
                container.mux(packet)
        container.mux(stream.encode())
        container.close()

        return {
            "ui": {"images": [{"filename": file, "subfolder": subfolder, "type": "output"}]},
            "result": (_relative_path(file, subfolder),),
        }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


NODE_CLASS_MAPPINGS: dict[str, type] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}


if _AUDIO_HELPER_AVAILABLE:
    NODE_CLASS_MAPPINGS["RunflowSaveAudioFlac"] = _make_audio_save_class(
        format="flac", quality_options=None, default_quality=None,
        class_suffix="Flac", display_format="FLAC",
    )
    NODE_CLASS_MAPPINGS["RunflowSaveAudioMP3"] = _make_audio_save_class(
        format="mp3", quality_options=["V0", "128k", "320k"], default_quality="V0",
        class_suffix="MP3", display_format="MP3",
    )
    NODE_CLASS_MAPPINGS["RunflowSaveAudioOpus"] = _make_audio_save_class(
        format="opus", quality_options=["64k", "96k", "128k", "192k", "320k"],
        default_quality="128k", class_suffix="Opus", display_format="Opus",
    )
    NODE_DISPLAY_NAME_MAPPINGS["RunflowSaveAudioFlac"] = "Runflow Save Audio (FLAC)"
    NODE_DISPLAY_NAME_MAPPINGS["RunflowSaveAudioMP3"] = "Runflow Save Audio (MP3)"
    NODE_DISPLAY_NAME_MAPPINGS["RunflowSaveAudioOpus"] = "Runflow Save Audio (Opus)"

if _VIDEO_TYPES_AVAILABLE:
    NODE_CLASS_MAPPINGS["RunflowSaveVideoMP4"] = RunflowSaveVideoMP4
    NODE_DISPLAY_NAME_MAPPINGS["RunflowSaveVideoMP4"] = "Runflow Save Video (MP4)"

if _PYAV_AVAILABLE:
    NODE_CLASS_MAPPINGS["RunflowSaveVideoWEBM"] = RunflowSaveVideoWEBM
    NODE_DISPLAY_NAME_MAPPINGS["RunflowSaveVideoWEBM"] = "Runflow Save Video (WEBM)"
