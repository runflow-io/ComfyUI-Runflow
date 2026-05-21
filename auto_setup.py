"""Auto-setup: install missing custom nodes + download missing models for a workflow.

Resolution sources:
- Custom nodes: ``api.comfy.org/nodes/<cnr_id>`` first (using ``cnr_id`` + ``ver``
  carried on each node's ``properties``), then ComfyUI Manager's
  ``custom-node-list.json`` as a fuzzy fallback by class_type.
- Models: the URL already carried on the workflow's ``properties.models`` (the
  resolver in ``nodes.py`` populates this), with Manager's ``model-list.json``
  as a filename-keyed fallback.

Cross-platform: assumes only ``python``, ``pip``, and ``git`` on PATH.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import aiohttp
import folder_paths

from .nodes import RunflowDeploy, _iter_all_nodes, resolve_workflow_models

logger = logging.getLogger(__name__)


REGISTRY_NODE_URL = "https://api.comfy.org/nodes/{cnr_id}"
REGISTRY_COMFY_NODE_URL = "https://api.comfy.org/comfy-nodes/{class_type}/node"
MANAGER_CUSTOM_NODES_URL = (
    "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/custom-node-list.json"
)
MANAGER_MODELS_URL = (
    "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/model-list.json"
)
MANAGER_EXT_NODE_MAP_URL = (
    "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/extension-node-map.json"
)
HUGGINGFACE_SEARCH_URL = "https://huggingface.co/api/models"
HUGGINGFACE_TREE_URL = "https://huggingface.co/api/models/{repo}/tree/main"


def _comfy_core_class_types() -> set[str]:
    """Class types that come from ComfyUI itself (and any already-installed
    custom nodes). Anything in this set is already runnable on this install.
    """
    try:
        import nodes as comfy_nodes  # ComfyUI's own module
        return set(comfy_nodes.NODE_CLASS_MAPPINGS.keys())
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Registry client (session-scoped, in-memory)
# ---------------------------------------------------------------------------


class RegistryClient:
    """Looks up custom-node provenance and model URLs from public registries.

    All caches are per-instance — callers re-use a single client for the
    duration of a setup job so the two checkboxes don't re-fetch the same
    JSON twice.
    """

    def __init__(self) -> None:
        self._node_cache: dict[str, dict | None] = {}
        self._class_type_cache: dict[str, dict | None] = {}
        self._manager_nodes: list[dict] | None = None
        self._manager_models: list[dict] | None = None
        self._ext_node_map: dict[str, list[str]] | None = None
        self._hf_cache: dict[str, dict | None] = {}
        self._lock = asyncio.Lock()

    async def _get_json(self, session: aiohttp.ClientSession, url: str) -> Any:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def resolve_by_cnr_id(self, cnr_id: str) -> dict | None:
        """Return the Comfy registry record for ``cnr_id``, or None on miss/error.

        The shape of interest is ``{"repository": "https://github.com/..."}``.
        """
        if not cnr_id or cnr_id == "comfy-core":
            return None
        if cnr_id in self._node_cache:
            return self._node_cache[cnr_id]
        try:
            async with aiohttp.ClientSession() as session:
                data = await self._get_json(session, REGISTRY_NODE_URL.format(cnr_id=cnr_id))
        except Exception as err:
            logger.info("comfy registry lookup failed for %s: %s", cnr_id, err)
            self._node_cache[cnr_id] = None
            return None
        self._node_cache[cnr_id] = data if isinstance(data, dict) else None
        return self._node_cache[cnr_id]

    async def _load_manager_nodes(self) -> list[dict]:
        async with self._lock:
            if self._manager_nodes is not None:
                return self._manager_nodes
            try:
                async with aiohttp.ClientSession() as session:
                    data = await self._get_json(session, MANAGER_CUSTOM_NODES_URL)
                self._manager_nodes = data.get("custom_nodes") or []
            except Exception as err:
                logger.info("manager custom-node-list fetch failed: %s", err)
                self._manager_nodes = []
            return self._manager_nodes

    async def _load_ext_node_map(self) -> dict[str, list[str]]:
        async with self._lock:
            if self._ext_node_map is not None:
                return self._ext_node_map
            try:
                async with aiohttp.ClientSession() as session:
                    data = await self._get_json(session, MANAGER_EXT_NODE_MAP_URL)
                # Shape: {repo_url: [[class_type, ...], {...metadata...}]}
                norm: dict[str, list[str]] = {}
                for repo_url, value in (data or {}).items():
                    if isinstance(value, list) and value and isinstance(value[0], list):
                        norm[repo_url] = [str(x) for x in value[0]]
                self._ext_node_map = norm
            except Exception as err:
                logger.info("manager extension-node-map fetch failed: %s", err)
                self._ext_node_map = {}
            return self._ext_node_map

    async def resolve_by_class_type_registry(self, class_type: str) -> dict | None:
        """Reverse-lookup a class_type against the Comfy registry.

        Endpoint: ``api.comfy.org/comfy-nodes/<class_type>/node`` returns the
        canonical custom-node record for whichever node provides this class.
        Built-in ComfyUI types 404 here — that's also the right signal: callers
        check ``loaded_class_types`` first, so reaching this path for a built-in
        means it wasn't loaded, and "not in registry" is a useful answer.
        """
        if not class_type:
            return None
        if class_type in self._class_type_cache:
            return self._class_type_cache[class_type]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    REGISTRY_COMFY_NODE_URL.format(class_type=class_type),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 404:
                        self._class_type_cache[class_type] = None
                        return None
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
        except Exception as err:
            logger.info("comfy registry class-type lookup failed for %s: %s", class_type, err)
            self._class_type_cache[class_type] = None
            return None
        if not isinstance(data, dict) or not data.get("repository"):
            self._class_type_cache[class_type] = None
            return None
        self._class_type_cache[class_type] = data
        return data

    @staticmethod
    def _camel_split(s: str) -> list[str]:
        """Split a CamelCase class name into lowercase tokens.

        Keeps runs of uppercase letters together (acronyms): ``UnetLoaderGGUF``
        → ``["unet", "loader", "gguf"]``, ``XMLHttpRequest`` →
        ``["xml", "http", "request"]``.
        """
        import re
        # Insert a boundary between lower→upper and between upper-run→upper+lower
        s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
        return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", s) if t]

    _BOILERPLATE_TOKENS = frozenset({
        "loader", "encoder", "decoder", "advanced", "simple", "load", "save",
        "node", "nodes", "model", "models", "comfyui", "comfy",
    })

    async def resolve_by_class_type_manager(self, class_type: str) -> dict | None:
        """Fallback: find a Manager extension-node-map entry providing ``class_type``.

        Multiple repos can declare the same class (forks, re-exports). Rank
        candidates by:
        1. How many class-type tokens (CamelCase split, minus generic words)
           appear in the repo name. ``UnetLoaderGGUF`` → ["unet","gguf"], so
           ``ComfyUI-GGUF`` scores higher than ``ComfyUI-Zlycoris``.
        2. Fewer total class types in the entry = more focused = more likely
           the canonical source rather than an aggregator/fork.
        3. Lexicographic repo URL for stability.
        """
        if not class_type:
            return None
        ext_map = await self._load_ext_node_map()
        matches: list[tuple[str, int]] = [
            (repo_url, len(class_types))
            for repo_url, class_types in ext_map.items()
            if class_type in class_types
        ]
        if not matches:
            return None

        tokens = [t for t in self._camel_split(class_type) if t not in self._BOILERPLATE_TOKENS]

        def _score(item: tuple[str, int]) -> tuple[int, int, str]:
            repo_url, class_count = item
            repo_name = repo_url.rsplit("/", 1)[-1].lower()
            name_matches = sum(1 for tok in tokens if tok in repo_name)
            # Lower score wins; negate name_matches so more matches sort first.
            return (-name_matches, class_count, repo_url)

        matches.sort(key=_score)
        return {"repository": matches[0][0]}

    async def resolve_by_class_type(self, class_type: str) -> dict | None:
        """Resolve a class_type to ``{"repository": "..."}``.

        Tries the comfy registry's reverse-lookup first (canonical), then
        Manager's extension-node-map with smart ranking as a fallback.
        """
        if not class_type:
            return None
        record = await self.resolve_by_class_type_registry(class_type)
        if record and record.get("repository"):
            return record
        return await self.resolve_by_class_type_manager(class_type)

    async def _load_manager_models(self) -> list[dict]:
        async with self._lock:
            if self._manager_models is not None:
                return self._manager_models
            try:
                async with aiohttp.ClientSession() as session:
                    data = await self._get_json(session, MANAGER_MODELS_URL)
                self._manager_models = data.get("models") or []
            except Exception as err:
                logger.info("manager model-list fetch failed: %s", err)
                self._manager_models = []
            return self._manager_models

    async def resolve_model_by_filename(self, filename: str) -> dict | None:
        """Find a Manager model entry matching ``filename`` (basename match).

        Returns ``{"url": "...", "save_path": "...", "filename": "..."}`` or None.
        """
        if not filename:
            return None
        models = await self._load_manager_models()
        for entry in models:
            if entry.get("filename") == filename:
                return entry
        return None

    async def resolve_by_huggingface_search(self, filename: str) -> dict | None:
        """Find a Hugging Face repo containing a file with exact basename ``filename``.

        Strategy: search the HF Models API (with and without the extension —
        relevance is usually better without), walk the top candidates, fetch
        each candidate's recursive file tree (lighter than the full model
        record), and return the URL of the first sibling whose basename
        matches. ``main`` branch is assumed; HF refers to the default branch
        by name in resolve URLs.

        Returns ``{"url", "repo", "path"}`` or None. No auth — gated repos
        will return 401/403 and we fall through. Results cached per filename
        for the duration of the registry client's lifetime.
        """
        if not filename:
            return None
        if filename in self._hf_cache:
            return self._hf_cache[filename]

        base = filename
        for ext in _MODEL_EXTENSIONS:
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break

        queries: list[str] = []
        if base:
            queries.append(base)
        if filename and filename not in queries:
            queries.append(filename)

        seen_repos: set[str] = set()
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for query in queries:
                    try:
                        async with session.get(
                            HUGGINGFACE_SEARCH_URL,
                            params={"search": query, "limit": 15},
                        ) as resp:
                            if resp.status != 200:
                                continue
                            results = await resp.json(content_type=None)
                    except Exception:
                        continue
                    if not isinstance(results, list):
                        continue

                    for repo_data in results:
                        model_id = repo_data.get("modelId") or repo_data.get("id")
                        if not model_id or model_id in seen_repos:
                            continue
                        seen_repos.add(model_id)
                        try:
                            async with session.get(
                                HUGGINGFACE_TREE_URL.format(repo=model_id),
                                params={"recursive": "true"},
                            ) as r:
                                if r.status != 200:
                                    continue
                                tree = await r.json(content_type=None)
                        except Exception:
                            continue
                        if not isinstance(tree, list):
                            continue
                        for item in tree:
                            if not isinstance(item, dict) or item.get("type") != "file":
                                continue
                            path = item.get("path") or ""
                            if os.path.basename(path) == filename:
                                url = f"https://huggingface.co/{model_id}/resolve/main/{path}"
                                result = {"url": url, "repo": model_id, "path": path}
                                self._hf_cache[filename] = result
                                return result
        except Exception as err:
            logger.info("HF search failed for %s: %s", filename, err)

        self._hf_cache[filename] = None
        return None


# ---------------------------------------------------------------------------
# Plan: diff workflow requirements against what's locally installed
# ---------------------------------------------------------------------------


def _normalize_origin(url: str | None) -> str:
    """Normalize a git origin URL for equality comparison.

    Strips ``.git`` and trailing slash, lowercases the host, drops scheme
    differences (https/git+https/ssh) by reducing to ``host/path``.
    """
    if not url:
        return ""
    s = url.strip().lower()
    if s.startswith("git+"):
        s = s[4:]
    if s.startswith("git@"):
        # git@github.com:owner/repo.git -> github.com/owner/repo.git
        s = s.replace(":", "/", 1)[4:]
    for scheme in ("https://", "http://", "ssh://"):
        if s.startswith(scheme):
            s = s[len(scheme):]
    if s.endswith(".git"):
        s = s[:-4]
    return s.rstrip("/")


def _installed_origins(installed: dict[str, dict]) -> set[str]:
    return {
        _normalize_origin(info.get("origin"))
        for info in installed.values()
        if info.get("origin")
    }


def _repo_name_from_origin(origin: str) -> str:
    """``https://github.com/owner/foo`` -> ``foo``."""
    norm = _normalize_origin(origin)
    if not norm:
        return ""
    return norm.rsplit("/", 1)[-1]


_NON_MODEL_FOLDER_TYPES = frozenset({
    "input", "output", "temp", "user", "custom_nodes", "configs",
})

_MODEL_EXTENSIONS = (
    ".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin",
    ".gguf", ".onnx", ".pb", ".engine",
)


def _detect_missing_widget_models(graph: dict) -> list[dict]:
    """Find widget values that look like model filenames but aren't installed
    in any model folder.

    Mirrors ComfyUI's own "missing model" detection. ComfyUI flags a COMBO
    widget red when its current value isn't in the option list — and for
    model-name widgets that option list comes from ``folder_paths``. We
    reproduce that signal here:

    1. Enumerate every file in every model folder_type into a set (both
       relative path and basename, since dropdowns can carry either shape).
    2. For each widget value in every node, if it has a model-ish extension
       and doesn't appear in that set, flag it.

    Returns ``[{"value": ..., "filename": ..., "node_type": ...}, ...]``.
    The caller resolves the URL + save_path via Manager's model-list.
    """
    installed: set[str] = set()
    for ft in folder_paths.folder_names_and_paths:
        if ft in _NON_MODEL_FOLDER_TYPES:
            continue
        try:
            files = folder_paths.get_filename_list(ft) or []
        except Exception:
            continue
        for f in files:
            installed.add(f)
            installed.add(os.path.basename(f))

    seen: set[str] = set()
    out: list[dict] = []
    for node in _iter_all_nodes(graph or {}):
        widgets_values = node.get("widgets_values")
        if not isinstance(widgets_values, list):
            continue
        for value in widgets_values:
            if not isinstance(value, str) or not value:
                continue
            if not value.lower().endswith(_MODEL_EXTENSIONS):
                continue
            if value in installed or os.path.basename(value) in installed:
                continue
            if value in seen:
                continue
            seen.add(value)
            out.append({
                "value": value,
                "filename": os.path.basename(value),
                "node_type": node.get("type") or "",
                "node": node,
            })
    return out


def _url_to_filename(url: str) -> str | None:
    """Best-effort filename extraction from a download URL."""
    if not url:
        return None
    # Strip query string, take the last path segment.
    base = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not base:
        return None
    name = base.rsplit("/", 1)[-1]
    return name or None


# Last-ditch fallback: when INPUT_TYPES introspection can't reveal a node's
# model folder_type (empty option lists on a fresh install, custom loaders
# without standard schemas), fall back to this map. Covers ~all common
# built-in + popular custom loaders.
_LOADER_FOLDER_HINTS: dict[str, str] = {
    "VAELoader": "vae",
    "CheckpointLoaderSimple": "checkpoints",
    "CheckpointLoader": "checkpoints",
    "unCLIPCheckpointLoader": "checkpoints",
    "LoraLoader": "loras",
    "LoraLoaderModelOnly": "loras",
    "ControlNetLoader": "controlnet",
    "ControlNetLoaderAdvanced": "controlnet",
    "DiffControlNetLoader": "controlnet",
    "UNETLoader": "unet",
    "UnetLoaderGGUF": "unet",
    "UnetLoaderGGUFAdvanced": "unet",
    "CLIPLoader": "clip",
    "CLIPLoaderGGUF": "clip",
    "DualCLIPLoader": "clip",
    "DualCLIPLoaderGGUF": "clip",
    "TripleCLIPLoader": "clip",
    "QuadrupleCLIPLoaderGGUF": "clip",
    "CLIPVisionLoader": "clip_vision",
    "UpscaleModelLoader": "upscale_models",
    "StyleModelLoader": "style_models",
    "GLIGENLoader": "gligen",
    "HypernetworkLoader": "hypernetworks",
    "DiffusersLoader": "diffusers",
    "PhotoMakerLoader": "photomaker",
    "IPAdapterModelLoader": "ipadapter",
}


def _guess_folder_type_for_node(node: dict) -> str | None:
    """Identify the model folder_type a node loads from.

    Tries INPUT_TYPES introspection first — finds the first COMBO input whose
    option list equals a model folder's file list. If introspection fails or
    the option list is empty (fresh install with no files yet), falls back to
    the explicit ``_LOADER_FOLDER_HINTS`` map keyed by class_type.
    """
    class_type = node.get("type") or ""
    try:
        import nodes as comfy_nodes
    except Exception:
        comfy_nodes = None  # type: ignore

    if comfy_nodes is not None:
        cls = comfy_nodes.NODE_CLASS_MAPPINGS.get(class_type)
        if cls is not None:
            try:
                input_types = cls.INPUT_TYPES()
            except Exception:
                input_types = None
            if isinstance(input_types, dict):
                folder_sets: dict[str, frozenset[str]] = {}
                for ft in folder_paths.folder_names_and_paths:
                    if ft in _NON_MODEL_FOLDER_TYPES:
                        continue
                    try:
                        folder_sets[ft] = frozenset(folder_paths.get_filename_list(ft) or [])
                    except Exception:
                        continue
                for section in ("required", "optional"):
                    for input_spec in (input_types.get(section) or {}).values():
                        if not isinstance(input_spec, (list, tuple)) or not input_spec:
                            continue
                        first = input_spec[0]
                        if not isinstance(first, list) or not first:
                            continue
                        opt_set = frozenset(first)
                        for ft, files in folder_sets.items():
                            if files and opt_set == files:
                                return ft

    return _LOADER_FOLDER_HINTS.get(class_type)


async def plan_setup(graph: dict) -> dict:
    """Compute the diff between what the workflow needs and what's installed.

    Returns ``{"missing_models": [...], "missing_custom_nodes": [...], "unresolved": [...]}``.
    Each missing_model is ``{rel_path, filename, url, sha256?, total_bytes?}``.
    Each missing_custom_node is ``{name, origin, commit?, source}``.
    Unresolved entries are class_types / filenames we couldn't map to a source.
    """
    registry = RegistryClient()
    installed = RunflowDeploy.get_custom_nodes()
    installed_origins = _installed_origins(installed)
    loaded_class_types = _comfy_core_class_types()

    # ---- Custom nodes ------------------------------------------------------
    missing_nodes: dict[str, dict] = {}  # keyed by normalized origin to dedupe
    unresolved: list[dict] = []
    seen_class_types: set[str] = set()

    for node in _iter_all_nodes(graph or {}):
        class_type = node.get("type") or ""
        if class_type in seen_class_types:
            continue
        seen_class_types.add(class_type)

        props = node.get("properties") or {}
        cnr_id = props.get("cnr_id")
        ver = props.get("ver")

        # Already runnable in this install? Skip.
        if class_type in loaded_class_types:
            continue
        if cnr_id == "comfy-core":
            continue

        origin: str | None = None
        commit: str | None = None
        source = ""
        name = ""

        if cnr_id:
            record = await registry.resolve_by_cnr_id(cnr_id)
            if record and record.get("repository"):
                origin = record["repository"]
                commit = ver or None
                source = "comfy_registry"
                name = cnr_id

        if not origin and class_type:
            record = await registry.resolve_by_class_type(class_type)
            if record and record.get("repository"):
                origin = record["repository"]
                source = "manager"
                name = _repo_name_from_origin(origin)

        if not origin:
            if class_type:
                unresolved.append({"kind": "custom_node", "class_type": class_type})
            continue

        norm = _normalize_origin(origin)
        if norm in installed_origins:
            continue
        if norm in missing_nodes:
            # Prefer the entry with a commit pin if a later node carries one.
            if commit and not missing_nodes[norm].get("commit"):
                missing_nodes[norm]["commit"] = commit
            continue
        missing_nodes[norm] = {
            "name": name or _repo_name_from_origin(origin),
            "origin": origin,
            "commit": commit,
            "source": source,
        }

    # ---- Models ------------------------------------------------------------
    models = resolve_workflow_models(graph or {})
    missing_models: list[dict] = []
    models_root = Path(folder_paths.models_dir)
    planned_filenames: set[str] = set()

    for rel_path, info in models.items():
        target = (models_root / rel_path).resolve()
        try:
            target.relative_to(models_root.resolve())
        except ValueError:
            continue
        if target.is_file():
            continue

        url = info.get("url")
        filename = Path(rel_path).name
        if not url:
            entry = await registry.resolve_model_by_filename(filename)
            if entry and entry.get("url"):
                url = entry["url"]

        if not url:
            unresolved.append({"kind": "model", "rel_path": rel_path, "filename": filename})
            continue

        missing_models.append({
            "rel_path": rel_path,
            "filename": filename,
            "url": url,
            "sha256": info.get("sha256"),
        })
        planned_filenames.add(filename)

    # Second pass: widget values that look like model filenames but aren't
    # installed and weren't covered above. resolve_workflow_models() silently
    # skips widget values for which it can find neither a local file nor a
    # URL in the same node's properties.models — the case ComfyUI itself flags
    # as "Missing Models" with the red node outline. We pick those up here
    # and look up the URL + save_path via two registries:
    #
    # 1. Manager's model-list — curated, carries an explicit save_path.
    # 2. Hugging Face search — long-tail fallback, no curated save_path so
    #    we infer it from the node's INPUT_TYPES / a small hint map.
    #
    # _detect_missing_widget_models() carries the originating node dict so
    # the folder_type can be inferred per-node without re-walking the graph.
    for entry in _detect_missing_widget_models(graph or {}):
        filename = entry["filename"]
        if filename in planned_filenames:
            continue

        url: str | None = None
        save_path = ""
        source = ""

        manager_entry = await registry.resolve_model_by_filename(filename)
        if manager_entry and manager_entry.get("url"):
            url = manager_entry["url"]
            save_path = (manager_entry.get("save_path") or "").strip("/")
            source = "manager"
        else:
            hf_entry = await registry.resolve_by_huggingface_search(filename)
            if hf_entry and hf_entry.get("url"):
                url = hf_entry["url"]
                # No save_path from HF — infer from the node that referenced it.
                save_path = (_guess_folder_type_for_node(entry["node"]) or "").strip("/")
                source = "huggingface"

        if not url:
            unresolved.append({
                "kind": "model",
                "filename": filename,
                "value": entry["value"],
            })
            continue

        # If the widget value carries a subdir (``loras/foo/x.safetensors``),
        # honor it — that's where ComfyUI is looking. Otherwise compose from
        # the inferred save_path.
        if "/" in entry["value"] or "\\" in entry["value"]:
            rel_path = entry["value"].replace("\\", "/")
        else:
            rel_path = f"{save_path}/{filename}" if save_path else filename

        missing_models.append({
            "rel_path": rel_path,
            "filename": filename,
            "url": url,
            "sha256": None,
            "source": source,
        })
        planned_filenames.add(filename)

    return {
        "missing_models": missing_models,
        "missing_custom_nodes": list(missing_nodes.values()),
        "unresolved": unresolved,
    }


# ---------------------------------------------------------------------------
# Workers: download models, install custom nodes
# ---------------------------------------------------------------------------


class CancelledByUser(Exception):
    """Raised from inside a worker when the job's cancel flag is set."""


ProgressCb = Callable[[dict], Awaitable[None]]


async def download_model(
    url: str,
    target_path: Path,
    expected_sha256: str | None,
    progress_cb: ProgressCb,
    is_cancelled: Callable[[], bool],
) -> None:
    """Stream-download ``url`` into ``target_path`` with sha256 verification.

    Writes to ``<target>.part`` and atomically renames on success. Verifies
    sha256 if provided; mismatched files are deleted before raising.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = target_path.with_name(target_path.name + ".part")

    timeout = aiohttp.ClientTimeout(total=None, sock_read=60, sock_connect=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            resp.raise_for_status()
            total_header = resp.headers.get("Content-Length")
            total_bytes = int(total_header) if total_header and total_header.isdigit() else 0
            downloaded = 0
            hasher = hashlib.sha256() if expected_sha256 else None
            last_emit = 0
            with open(part_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    if is_cancelled():
                        raise CancelledByUser()
                    f.write(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    downloaded += len(chunk)
                    # Throttle progress events to ~10/s worth of bytes
                    if downloaded - last_emit >= 256 * 1024:
                        await progress_cb({
                            "type": "model_progress",
                            "bytes": downloaded,
                            "total_bytes": total_bytes,
                        })
                        last_emit = downloaded

    if hasher is not None and expected_sha256:
        actual = hasher.hexdigest()
        if actual.lower() != expected_sha256.lower():
            try:
                part_path.unlink()
            except OSError:
                pass
            raise ValueError(f"sha256 mismatch (got {actual[:12]}…, expected {expected_sha256[:12]}…)")

    # Atomic rename across the same filesystem; if target exists (race), replace.
    part_path.replace(target_path)
    await progress_cb({
        "type": "model_progress",
        "bytes": downloaded,
        "total_bytes": total_bytes or downloaded,
    })


def _run_subprocess(
    argv: list[str],
    cwd: Path | None,
    log_cb: Callable[[str], None],
    timeout: float | None = None,
) -> int:
    """Run a subprocess streaming combined stdout/stderr line-by-line to log_cb.

    Returns the exit code. Raises ``subprocess.TimeoutExpired`` if ``timeout``
    elapses. No ``shell=True`` — argv is explicit.
    """
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log_cb(line)
        proc.wait(timeout=timeout)
    except Exception:
        proc.kill()
        raise
    return proc.returncode


def install_custom_node_sync(
    origin: str,
    commit: str | None,
    name: str,
    custom_nodes_dir: Path,
    log_cb: Callable[[str], None],
    is_cancelled: Callable[[], bool],
) -> None:
    """Clone ``origin`` and checkout ``commit`` into ``custom_nodes/<name>/``.

    Runs entirely synchronously — call via ``asyncio.to_thread`` from async code.
    Uses ``git clone --depth=1`` + ``git fetch --depth=1 origin <sha>`` for the
    fast path, and falls back to a full clone if SHA-targeted fetch fails.
    Then ``python -m pip install -r requirements.txt`` if that file is present.
    """
    if is_cancelled():
        raise CancelledByUser()
    custom_nodes_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="runflow-cn-"))
    target_in_staging = staging / name
    try:
        log_cb(f"git clone {origin}")
        rc = _run_subprocess(
            ["git", "clone", "--depth=1", origin, str(target_in_staging)],
            cwd=None,
            log_cb=log_cb,
            timeout=600,
        )
        if rc != 0:
            raise RuntimeError(f"git clone failed (exit {rc})")

        if commit:
            if is_cancelled():
                raise CancelledByUser()
            log_cb(f"git checkout {commit[:12]}…")
            rc = _run_subprocess(
                ["git", "fetch", "--depth=1", "origin", commit],
                cwd=target_in_staging,
                log_cb=log_cb,
                timeout=300,
            )
            if rc == 0:
                rc = _run_subprocess(
                    ["git", "checkout", commit],
                    cwd=target_in_staging,
                    log_cb=log_cb,
                    timeout=120,
                )
            if rc != 0:
                # Fallback: full clone, then checkout. Some hosts disable
                # uploadpack.allowReachableSHA1InWant, which makes the
                # depth-1 fetch above reject the SHA.
                log_cb("falling back to full clone")
                shutil.rmtree(target_in_staging, ignore_errors=True)
                rc = _run_subprocess(
                    ["git", "clone", origin, str(target_in_staging)],
                    cwd=None,
                    log_cb=log_cb,
                    timeout=900,
                )
                if rc != 0:
                    raise RuntimeError(f"git clone (full) failed (exit {rc})")
                rc = _run_subprocess(
                    ["git", "checkout", commit],
                    cwd=target_in_staging,
                    log_cb=log_cb,
                    timeout=120,
                )
                if rc != 0:
                    raise RuntimeError(f"git checkout {commit[:12]}… failed (exit {rc})")

        # Move into custom_nodes/. Replace any existing dir of the same name.
        final = custom_nodes_dir / name
        if final.exists():
            shutil.rmtree(final, ignore_errors=False)
        shutil.move(str(target_in_staging), str(final))

        # requirements.txt — run via python -m pip, never bare `pip`.
        req = final / "requirements.txt"
        if req.is_file():
            if is_cancelled():
                raise CancelledByUser()
            log_cb("python -m pip install -r requirements.txt")
            rc = _run_subprocess(
                [sys.executable, "-m", "pip", "install", "-r", str(req)],
                cwd=final,
                log_cb=log_cb,
                timeout=1800,
            )
            if rc != 0:
                raise RuntimeError(f"pip install failed (exit {rc})")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Job orchestrator
# ---------------------------------------------------------------------------


class _Job:
    def __init__(self, job_id: str, options: dict, plan: dict) -> None:
        self.job_id = job_id
        self.options = options
        self.plan = plan
        self.cancel = False
        self.task: asyncio.Task | None = None


_JOBS: dict[str, _Job] = {}


def get_job(job_id: str) -> _Job | None:
    return _JOBS.get(job_id)


def cancel_job(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if not job:
        return False
    job.cancel = True
    return True


async def run_setup_job(job: _Job, send: ProgressCb) -> None:
    """Drive the install + download steps for one job.

    Events emitted via ``send``:
    - ``{"type": "job_start", "job_id", "models_total", "custom_nodes_total"}``
    - ``{"type": "model_start", "index", "total", "filename", "rel_path"}``
    - ``{"type": "model_progress", "bytes", "total_bytes"}``
    - ``{"type": "model_done", "index"}``
    - ``{"type": "model_error", "index", "error"}``
    - ``{"type": "custom_node_start", "index", "total", "name", "origin"}``
    - ``{"type": "custom_node_log", "line"}``
    - ``{"type": "custom_node_done", "index"}``
    - ``{"type": "custom_node_error", "index", "error"}``
    - ``{"type": "job_done", "had_errors": bool, "errors": [...]}``
    - ``{"type": "job_cancelled"}``
    """
    plan = job.plan
    opts = job.options
    do_models = bool(opts.get("download_models", True))
    do_nodes = bool(opts.get("install_custom_nodes", True))

    models = plan.get("missing_models", []) if do_models else []
    nodes = plan.get("missing_custom_nodes", []) if do_nodes else []
    errors: list[dict] = []

    await send({
        "type": "job_start",
        "job_id": job.job_id,
        "models_total": len(models),
        "custom_nodes_total": len(nodes),
    })

    models_root = Path(folder_paths.models_dir)
    custom_nodes_dir = Path(folder_paths.base_path) / "custom_nodes"

    def _is_cancelled() -> bool:
        return job.cancel

    # Models and custom nodes run in parallel — they're independent activities,
    # and the spec ("downloading in the background … below that we see custom
    # node install status") implies both sections are live at the same time.
    # Within each phase, items run sequentially to match the "N of M" UI.

    async def _drive_models() -> None:
        for index, model in enumerate(models):
            if job.cancel:
                return
            await send({
                "type": "model_start",
                "index": index,
                "total": len(models),
                "filename": model["filename"],
                "rel_path": model["rel_path"],
            })
            target = models_root / model["rel_path"]
            try:
                async def _pcb(event: dict, _i=index) -> None:
                    event = dict(event)
                    event["index"] = _i
                    event["total"] = len(models)
                    await send(event)
                await download_model(
                    url=model["url"],
                    target_path=target,
                    expected_sha256=model.get("sha256"),
                    progress_cb=_pcb,
                    is_cancelled=_is_cancelled,
                )
                await send({"type": "model_done", "index": index, "total": len(models)})
            except CancelledByUser:
                return
            except Exception as err:
                logger.exception("auto-setup: model download failed for %s", model["filename"])
                errors.append({"kind": "model", "name": model["filename"], "error": str(err)})
                await send({"type": "model_error", "index": index, "error": str(err)})

    async def _drive_custom_nodes() -> None:
        loop = asyncio.get_running_loop()
        for index, cn in enumerate(nodes):
            if job.cancel:
                return
            await send({
                "type": "custom_node_start",
                "index": index,
                "total": len(nodes),
                "name": cn["name"],
                "origin": cn["origin"],
            })

            def _log_sync(line: str, _i=index, _loop=loop) -> None:
                asyncio.run_coroutine_threadsafe(
                    send({"type": "custom_node_log", "index": _i, "line": line}),
                    _loop,
                )

            try:
                await asyncio.to_thread(
                    install_custom_node_sync,
                    cn["origin"],
                    cn.get("commit"),
                    cn["name"],
                    custom_nodes_dir,
                    _log_sync,
                    _is_cancelled,
                )
                await send({"type": "custom_node_done", "index": index, "total": len(nodes)})
            except CancelledByUser:
                return
            except Exception as err:
                logger.exception("auto-setup: custom node install failed for %s", cn["name"])
                errors.append({"kind": "custom_node", "name": cn["name"], "error": str(err)})
                await send({"type": "custom_node_error", "index": index, "error": str(err)})

    await asyncio.gather(_drive_models(), _drive_custom_nodes())

    if job.cancel:
        await send({"type": "job_cancelled"})
        return
    await send({"type": "job_done", "had_errors": bool(errors), "errors": errors})


def start_job(options: dict, plan: dict, send: ProgressCb) -> str:
    """Register a new job and kick off its driver task. Returns the job_id."""
    job_id = uuid.uuid4().hex
    job = _Job(job_id, options, plan)
    _JOBS[job_id] = job
    job.task = asyncio.create_task(run_setup_job(job, send))
    return job_id


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


async def schedule_restart(delay: float = 0.25) -> None:
    """Re-exec the current Python process after ``delay`` seconds.

    Re-using ``sys.executable`` + ``sys.argv`` preserves whatever launch flags
    the user originally passed (port, listen address, custom-args). Works on
    Linux, macOS, and Windows.
    """
    await asyncio.sleep(delay)
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception:
        logger.exception("auto-setup: os.execv restart failed")
        # Hard exit so a supervisor (systemd, launchd, the comfy-cli wrapper)
        # can bring us back. Better than a half-restart that leaves no log.
        os._exit(1)
