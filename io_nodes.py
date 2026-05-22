"""Typed input/output nodes for Runflow.

Locally, every input node is a pure pass-through of its `value` socket.
The deploy-time rewriter (future work in the inference service) consumes
the class attrs below to inject caller-supplied values without executing
Python here.

Inputs cover STRING / INT / FLOAT / BOOLEAN / IMAGE. Outputs cover IMAGE
(saves each batch image as PNG) and FILE (announces an arbitrary file
already sitting under ComfyUI's output directory — videos, 3D meshes,
audio, archives). MASK / LATENT and the Seed input variant remain off
the surface pending end-to-end deploy support.

Both output node kinds emit `{"ui": {...}, "result": (value,)}` so each
artifact lands in `/history/{prompt_id}.outputs` keyed by the output
node's own id. The deploy worker then maps that node id to the
customer-facing `output_id` via class_type
(`outputs.py:extract_output_id_map`) and uploads everything under a
known artifact bucket (`images`, `gifs`, `audios`, `videos`,
`model_files`, `files`).

Contract consumed by the rewriter:
- RUNFLOW_IO     "input" | "output"
- RUNFLOW_TYPE   ComfyUI socket type (e.g. "IMAGE", "STRING", "FILE")

`input_id` / `output_id` are the stable join keys. They are never
mutated at deploy time — the rewriter rewires the `value` socket's
upstream to inject a caller-supplied value for input nodes.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import folder_paths
import numpy as np
from PIL import Image as PILImage


_INPUT_TYPES: tuple[str, ...] = ("STRING", "INT", "FLOAT", "BOOLEAN", "IMAGE")


def _make_input_class(type_name: str) -> type:
    def input_types(cls):
        return {
            "required": {
                "input_id": ("STRING", {"default": f"{type_name.lower()}_input"}),
                "display_name": ("STRING", {"default": ""}),
                "description": ("STRING", {"default": "", "multiline": True}),
            },
            "optional": {"value": (type_name,)},
        }

    def passthrough(self, input_id, display_name, description, value=None):
        return (value,)

    return type(
        f"RunflowInput{type_name.capitalize()}",
        (),
        {
            "INPUT_TYPES": classmethod(input_types),
            "RETURN_TYPES": (type_name,),
            "RETURN_NAMES": ("value",),
            "FUNCTION": "passthrough",
            "CATEGORY": "Runflow/Input",
            "RUNFLOW_IO": "input",
            "RUNFLOW_TYPE": type_name,
            "passthrough": passthrough,
            "__doc__": f"Runflow typed input ({type_name}). Connect the `value` "
                       f"socket locally; value is injected at deploy time.",
        },
    )


class RunflowOutputImage:
    """Runflow named output (IMAGE). Saves each image in the batch as PNG
    to ComfyUI's output directory and emits a UI dict so the artifact lands
    in /history outputs keyed by this node's id."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "output_id": ("STRING", {"default": "image_output"}),
                "output_name": ("STRING", {"default": ""}),
                "value": ("IMAGE",),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("value",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "Runflow/Output"

    RUNFLOW_IO = "output"
    RUNFLOW_TYPE = "IMAGE"

    def save(self, output_id, output_name, value):
        full_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            output_id, folder_paths.get_output_directory(),
            value.shape[2], value.shape[1],
        )
        os.makedirs(full_folder, exist_ok=True)
        results: list[dict] = []
        for image in value:
            arr = 255.0 * image.cpu().numpy()
            img = PILImage.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
            file = f"{filename}_{counter:05}_.png"
            img.save(os.path.join(full_folder, file), compress_level=4)
            results.append({"filename": file, "subfolder": subfolder, "type": "output"})
            counter += 1
        return {"ui": {"images": results}, "result": (value,)}


class RunflowOutputFile:
    """Runflow named output (FILE). Announces a file already sitting under
    ComfyUI's output directory (written by an upstream save-* node) so the
    deploy worker uploads it as the run's artifact for ``output_id``.

    The ``value`` socket is the filename as a STRING — optionally including
    a subfolder prefix relative to ``folder_paths.get_output_directory()``.
    Subfolder + basename are split before reporting so the worker's R2 key
    stays ``runs/{run_id}/{output_id}/{basename}``."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "output_id": ("STRING", {"default": "file_output"}),
                "output_name": ("STRING", {"default": ""}),
                "value": ("STRING", {"forceInput": True}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("value",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "Runflow/Output"

    RUNFLOW_IO = "output"
    RUNFLOW_TYPE = "FILE"

    def save(self, output_id, output_name, value):
        if not isinstance(value, str) or not value:
            raise ValueError(
                "RunflowOutputFile: `value` must be a non-empty filename string "
                "relative to ComfyUI's output directory."
            )

        normalized = value.replace("\\", "/")
        rel = PurePosixPath(normalized)
        basename = rel.name
        if not basename:
            raise ValueError(
                f"RunflowOutputFile: cannot derive a filename from value={value!r}."
            )
        subfolder = str(rel.parent) if rel.parent != PurePosixPath(".") else ""

        output_dir = Path(folder_paths.get_output_directory()).resolve()
        candidate = (output_dir / subfolder / basename).resolve()
        if not candidate.is_relative_to(output_dir):
            raise ValueError(
                f"RunflowOutputFile: refusing path that escapes the output directory: {value!r}."
            )
        if not candidate.is_file():
            raise FileNotFoundError(
                f"RunflowOutputFile: expected file at {candidate} (from value={value!r}); "
                "did the upstream save node run?"
            )

        entry = {"filename": basename, "subfolder": subfolder, "type": "output"}
        return {"ui": {"files": [entry]}, "result": (value,)}


RUNFLOW_INPUT_CLASSES: dict[str, type] = {
    f"RunflowInput{t.capitalize()}": _make_input_class(t) for t in _INPUT_TYPES
}

RUNFLOW_OUTPUT_CLASSES: dict[str, type] = {
    "RunflowOutputImage": RunflowOutputImage,
    "RunflowOutputFile": RunflowOutputFile,
}


def display_name(class_name: str) -> str:
    if class_name.startswith("RunflowInput"):
        return f"Runflow Input ({class_name[len('RunflowInput'):]})"
    if class_name.startswith("RunflowOutput"):
        return f"Runflow Output ({class_name[len('RunflowOutput'):]})"
    return class_name


NODE_CLASS_MAPPINGS: dict[str, type] = {**RUNFLOW_INPUT_CLASSES, **RUNFLOW_OUTPUT_CLASSES}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {
    name: display_name(name) for name in NODE_CLASS_MAPPINGS
}
