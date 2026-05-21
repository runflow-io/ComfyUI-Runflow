import hashlib
import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import folder_paths

from ._git_info import read_git


logger = logging.getLogger(__name__)


_HASH_CACHE_FILE = Path(folder_paths.models_dir) / ".runflow-hash-cache.json"


def _load_hash_cache() -> dict:
    try:
        return json.loads(_HASH_CACHE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_hash_cache(cache: dict) -> None:
    try:
        tmp = _HASH_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
        tmp.replace(_HASH_CACHE_FILE)
    except OSError:
        pass


def _resolve_model_path(directory: str, name: str) -> Path | None:
    """Resolve a model file to its actual on-disk path.

    Uses ``folder_paths.get_full_path`` first so extra-model-paths configs
    (``extra_model_paths.yaml``) and multi-root setups are honored. Falls
    back to the legacy ``<models_dir>/<directory>/<name>`` lookup.
    """
    if not name:
        return None
    if directory:
        try:
            full = folder_paths.get_full_path(directory, name)
        except Exception:
            full = None
        if full:
            p = Path(full)
            if p.is_file():
                return p
    rel = os.path.join(directory, name) if directory else name
    root = Path(folder_paths.models_dir).resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target if target.is_file() else None


def _canonical_models_relpath(directory: str, name: str) -> str | None:
    """Return the file's path relative to ``folder_paths.models_dir``.

    Why: ComfyUI loaders are configured against *physical* subdirectories
    of ``models/``, but ``folder_paths.folder_names_and_paths`` keys are
    *logical* folder-type aliases that custom nodes can register against
    any physical path. ComfyUI-GGUF, for example, registers ``unet_gguf``
    as an alias for the existing ``models/unet/`` directory (scoped to
    ``.gguf`` files). Emitting the logical name ``unet_gguf`` into the
    deploy manifest then makes the worker materialise the file at
    ``models/unet_gguf/...`` — somewhere the GGUF loader never scans.

    By resolving through ``get_full_path`` and re-anchoring on
    ``models_dir``, the manifest faithfully describes where the file
    physically lives in the local install (including any subdirectory
    layout like ``loras/flux/a/b/x.safetensors``), and the worker
    reproduces that exact layout in its runtime tree.

    Returns None for files that don't exist or live outside
    ``models_dir`` (extra roots mounted elsewhere via
    ``extra_model_paths.yaml`` — the deploy worker has no convention
    for those; the caller falls back to the original directory/name).
    """
    if not name:
        return None
    target = _resolve_model_path(directory, name)
    if target is None:
        return None
    try:
        models_root = Path(folder_paths.models_dir).resolve()
        return target.resolve().relative_to(models_root).as_posix()
    except (OSError, ValueError):
        return None


def _sha256_at_path(target: Path) -> str | None:
    """Chunked sha256 with (mtime, size)-keyed memoisation.

    Shared by widget-derived (canonical-path) and properties.models-derived
    (folder_type-derived) hashing paths so the on-disk cache stays unified.
    """
    try:
        stat = target.stat()
    except OSError:
        return None
    key = str(target)
    cache = _load_hash_cache()
    entry = cache.get(key)
    if (
        isinstance(entry, dict)
        and entry.get("mtime") == stat.st_mtime
        and entry.get("size") == stat.st_size
        and entry.get("sha256")
    ):
        return entry["sha256"]

    h = hashlib.sha256()
    with target.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()

    cache[key] = {"sha256": digest, "mtime": stat.st_mtime, "size": stat.st_size}
    _save_hash_cache(cache)
    return digest


def sha256_model_file(directory: str, name: str) -> str | None:
    """Hash a model file resolved through ComfyUI's folder_paths, memoized by (mtime, size).

    Returns None if the file cannot be found in any configured model
    location. Safe for multi-GB files (chunked streaming hash).
    """
    target = _resolve_model_path(directory, name)
    if target is None:
        return None
    return _sha256_at_path(target)


# Folder types under folder_paths that hold non-model assets and should be
# excluded when indexing local models for widget-value lookup.
_NON_MODEL_FOLDER_TYPES = frozenset({"input", "output", "temp", "user", "custom_nodes", "configs"})


_ALIASES_LOGGED = False


def _log_folder_type_aliases() -> None:
    """Log every folder_type whose first registered physical directory has a
    different top-level name under ``models/``. Runs once per process — surfaces
    custom-node aliasing (ComfyUI-GGUF's ``unet_gguf`` → ``unet/`` and similar)
    so users can verify the manifest is routing files the way they expect.
    """
    global _ALIASES_LOGGED
    if _ALIASES_LOGGED:
        return
    _ALIASES_LOGGED = True
    try:
        models_root = Path(folder_paths.models_dir).resolve()
    except OSError:
        return
    aliases: list[tuple[str, str]] = []
    for ftype, value in folder_paths.folder_names_and_paths.items():
        if ftype in _NON_MODEL_FOLDER_TYPES:
            continue
        paths = value[0] if isinstance(value, tuple) and value else value
        for p in paths or ():
            try:
                rel = Path(p).resolve().relative_to(models_root)
            except (ValueError, OSError):
                continue
            top = rel.parts[0] if rel.parts else ""
            if top and top != ftype:
                aliases.append((ftype, rel.as_posix()))
                break
    if aliases:
        formatted = ", ".join(f"{ft}→models/{phys}/" for ft, phys in aliases)
        logger.info("Runflow: folder_type aliases detected (manifest uses physical path): %s", formatted)


def _build_local_model_index() -> dict[str, list[str]]:
    """Map every widget-string a node might reference to the *canonical*
    path(s) of matching files under ``folder_paths.models_dir``.

    Keys: basename, folder-type-relative path (as ComfyUI dropdowns
    present it), and the canonical path itself — any of these may be
    what a node's ``widgets_values`` stores. Values: paths relative to
    ``models_dir`` (i.e. the physical location). See
    ``_canonical_models_relpath`` for why the physical path matters.

    The same basename may resolve to multiple canonical paths when a
    model is physically present in more than one models subdirectory
    (e.g. both ``unet/x.safetensors`` and ``diffusion_models/x.safetensors``);
    the caller iterates all matches and emits a manifest entry per
    physical location.
    """
    _log_folder_type_aliases()
    index: dict[str, list[str]] = {}
    for folder_type in list(folder_paths.folder_names_and_paths.keys()):
        if folder_type in _NON_MODEL_FOLDER_TYPES:
            continue
        try:
            files = folder_paths.get_filename_list(folder_type)
        except Exception:
            continue
        for rel in files or []:
            canonical = _canonical_models_relpath(folder_type, rel)
            if not canonical:
                continue
            base = os.path.basename(canonical)
            for k in (base, rel, canonical):
                bucket = index.setdefault(k, [])
                if canonical not in bucket:
                    bucket.append(canonical)
    return index


def _iter_all_nodes(graph: dict):
    """Yield every node in the graph, descending into ComfyUI subgraphs.

    ComfyUI's newer subgraph feature splits a workflow across two locations:
    the top-level ``graph["nodes"]`` holds subgraph-*instance* nodes (whose
    ``widgets_values`` are typically empty or proxy-mapped), while the
    actual loader nodes that carry ``properties.models`` URLs live inside
    ``graph["definitions"]["subgraphs"][i]["nodes"]``. Walking only the top
    level misses every model declaration inside any subgraph.

    Strategy: walk top-level nodes, then walk every subgraph definition
    in ``definitions.subgraphs``. We don't try to track which definitions
    are *referenced* by instances — walking all of them is simpler, and
    the cost is bounded by the file size. Also recurses into any ``nodes``
    array attached to a node itself (some legacy group-style nestings
    use this shape too).
    """
    def _walk(nodes_list):
        for node in nodes_list or ():
            if not isinstance(node, dict):
                continue
            yield node
            inner = node.get("nodes")
            if isinstance(inner, list):
                yield from _walk(inner)

    yield from _walk(graph.get("nodes") or [])
    definitions = graph.get("definitions") or {}
    for sub in definitions.get("subgraphs") or []:
        if isinstance(sub, dict):
            yield from _walk(sub.get("nodes") or [])


def resolve_workflow_models(graph: dict) -> dict:
    """Return ``{"<models-relative-path>": {url?, sha256?}}`` for every model
    the workflow uses.

    Manifest keys are the file's path relative to ``folder_paths.models_dir``
    — its physical location in the local install. The deploy worker
    materialises each file at the same relative path inside its ComfyUI
    ``models/`` tree, so loaders configured to scan ``models/unet/`` find
    files keyed under ``unet/...`` regardless of which logical folder_type
    (e.g. ``unet_gguf``) the local custom node registered against that
    directory. Subdirectories are preserved verbatim
    (``loras/flux/a/b/lora.safetensors`` deploys as-is).

    Node enumeration descends into ComfyUI subgraphs
    (``definitions.subgraphs[*].nodes``) — model-loader nodes inside a
    subgraph carry the same ``properties.models``/``widgets_values`` shape
    as top-level nodes, and miss the manifest entirely if only the
    top-level ``graph["nodes"]`` array is walked.

    Primary: scan each node's ``widgets_values`` for strings that match a
    locally-installed model file (looked up via the canonical index).
    This catches the user's current dropdown selections, which the cached
    ``properties.models`` block may not reflect.

    URL fallback (per widget value): when a widget value names a file
    that isn't installed locally but the same node's ``properties.models``
    carries a URL for that basename, the model is still emitted into the
    manifest with the URL and no sha256. The deploy worker then fetches
    from the URL into its content-addressed cache. This lets a workflow
    declare a model dependency the user hasn't downloaded yet.

    Legacy fallback (per node): if a node has no widget values at all,
    its ``properties.models`` block is treated as authoritative. Nodes
    that have widget values but yielded no matches (neither local nor
    URL-fallback) are skipped to avoid resurrecting stale dropdown
    leftovers.

    URLs from ``properties.models`` are propagated to widget-derived
    matches when the canonical path or basename lines up, so the
    deployed worker still has a fetch source for any locally-found file
    the user originally pulled from a URL.
    """
    nodes = list(_iter_all_nodes(graph or {}))
    index = _build_local_model_index()

    url_by_basename: dict[str, str] = {}
    url_by_canonical: dict[str, str] = {}
    for node in nodes:
        for m in (node.get("properties") or {}).get("models") or []:
            if not isinstance(m, dict):
                continue
            url = m.get("url")
            name = m.get("name") or ""
            if not url or not name:
                continue
            url_by_basename.setdefault(os.path.basename(name), url)
            directory = m.get("directory") or ""
            canonical_hint = _canonical_models_relpath(directory, name) if directory else None
            if canonical_hint:
                url_by_canonical.setdefault(canonical_hint, url)

    out: dict[str, dict] = {}

    for node in nodes:
        widgets_values = node.get("widgets_values")
        props_models = (node.get("properties") or {}).get("models") or []

        # Per-node URL fallback table: a widget value that doesn't match
        # any locally-installed file can still be emitted if THIS node's
        # properties.models carries a URL for the same basename. Per-node
        # scoping (rather than graph-wide) keeps the live-vs-stale check
        # tight — a URL only counts as live for the node that references
        # the same filename in its current widgets.
        props_by_basename: dict[str, dict] = {}
        for m in props_models:
            if not isinstance(m, dict):
                continue
            n = m.get("name") or ""
            if not n:
                continue
            props_by_basename.setdefault(os.path.basename(n), m)

        node_hit = False
        if isinstance(widgets_values, list):
            for value in widgets_values:
                if not isinstance(value, str) or not value:
                    continue
                matches = index.get(value) or index.get(os.path.basename(value)) or []
                if matches:
                    for canonical in matches:
                        node_hit = True
                        if canonical in out:
                            continue
                        target = (Path(folder_paths.models_dir) / canonical).resolve()
                        sha = _sha256_at_path(target)
                        entry: dict = {}
                        url = url_by_canonical.get(canonical) or url_by_basename.get(os.path.basename(canonical))
                        if url:
                            entry["url"] = url
                        if sha:
                            entry["sha256"] = sha
                        out[canonical] = entry
                    continue

                # No locally-installed file matched. If the same node has a
                # properties.models entry with a URL for this basename, emit
                # a URL-only manifest entry — the deploy worker will fetch
                # the file from the URL on first use.
                basename = os.path.basename(value)
                m = props_by_basename.get(basename) or props_by_basename.get(value)
                if not m:
                    continue
                url = m.get("url")
                if not url:
                    continue
                directory = m.get("directory") or ""
                # Prefer the widget value when it carries subdirs (it
                # reflects the user's exact dropdown selection, including
                # nested layout). Otherwise compose from properties.models'
                # directory + basename. The result is treated as a physical
                # path under models/ by the deploy worker; for URL-only
                # models we have no canonical-resolution mechanism since
                # the file isn't on disk to inspect.
                if "/" in value or "\\" in value:
                    key = value.replace("\\", "/")
                else:
                    key = f"{directory}/{basename}" if directory else basename
                if key in out:
                    continue
                out[key] = {"url": url}
                node_hit = True

        if node_hit:
            continue

        # Legacy fallback: nodes with NO widget values at all (or no
        # widgets_values list — older graph shapes) fall through to the
        # entire properties.models block. Skip nodes that had widgets but
        # whose values neither matched the local index nor triggered the
        # URL fallback above — those values are typically non-model text
        # widgets, and resurrecting their properties.models would carry
        # stale entries from previous dropdown selections.
        if isinstance(widgets_values, list) and widgets_values:
            continue

        for m in props_models:
            if not isinstance(m, dict):
                continue
            name = m.get("name") or ""
            if not name:
                continue
            directory = m.get("directory") or ""
            canonical = _canonical_models_relpath(directory, name) or (
                f"{directory}/{name}" if directory else name
            )
            if canonical in out:
                continue
            sha = sha256_model_file(directory, name)
            entry = {}
            if m.get("url"):
                entry["url"] = m["url"]
            if sha:
                entry["sha256"] = sha
            out[canonical] = entry

    return out


class RunflowDeploy:
    """Virtual JS-only node (see js/runflow.js: isVirtualNode = true).

    Registered in Python so ComfyUI knows its schema, but it is never added
    to the executed prompt — the deploy action is handled entirely in the
    browser. Empty host/api_key fall back to the global Runflow settings.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "endpoint_name": ("STRING", {"default": "default"}),
                "host": ("STRING", {"default": ""}),
                "api_key": ("STRING", {"default": ""}),
            }
        }

    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "noop"
    CATEGORY = "Runflow"

    def noop(self, endpoint_name, host, api_key):
        return {}

    @staticmethod
    def get_installed_packages():
        result = subprocess.run(
            ["pip", "list", "--format=json"],
            capture_output=True, text=True,
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

    @staticmethod
    def get_cached_models():
        hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
        torch_cache = Path.home() / ".cache" / "torch" / "hub"

        models = {"huggingface": [], "pytorch": []}

        if hf_cache.exists():
            for item in hf_cache.iterdir():
                if item.is_dir() and item.name.startswith("models--"):
                    models["huggingface"].append(
                        item.name.replace("models--", "").replace("--", "/")
                    )

        if torch_cache.exists():
            for item in torch_cache.iterdir():
                if item.is_dir():
                    models["pytorch"].append(item.name)

        return models

    @staticmethod
    def get_models_directory():
        models_dir = folder_paths.models_dir
        files = []
        for root, _dirs, filenames in os.walk(models_dir):
            for f in filenames:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, models_dir)
                files.append(rel_path)
        return files

    @staticmethod
    def get_custom_nodes():
        """Return a dict keyed by directory name with ``{origin, commit, dirty}``."""
        custom_dir = Path(folder_paths.base_path) / "custom_nodes"
        if not custom_dir.is_dir():
            return {}
        candidates = sorted(
            p for p in custom_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".") and not p.name.startswith("__")
        )

        # Pin our own origin to the canonical public URL. The local remote
        # may be an SSH URL, a fork, or a contributor's mirror — but the
        # deploy worker clones over HTTPS from the canonical repo, so the
        # manifest must always list that one.
        self_dir = Path(__file__).resolve().parent

        def _entry(path: Path) -> tuple[str, dict]:
            info = read_git(path)
            origin = info.origin
            if path.resolve() == self_dir or path.name == "ComfyUI-Runflow":
                origin = "https://github.com/bettergroupinc/ComfyUI-Runflow"
            return path.name, {
                "origin": origin,
                "commit": info.commit,
                "dirty": info.dirty,
            }

        if not candidates:
            return {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            return dict(pool.map(_entry, candidates))

    @staticmethod
    def get_comfyui_git_info():
        info = read_git(Path(folder_paths.base_path))
        return {"origin": info.origin, "commit": info.commit, "dirty": info.dirty}
