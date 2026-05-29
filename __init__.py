import asyncio
import base64
import copy
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path

import aiohttp
from aiohttp import web
import folder_paths

from .nodes import RunflowDeploy, resolve_hoisted_custom_nodes, resolve_workflow_models
from .io_nodes import (
    NODE_CLASS_MAPPINGS as IO_NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as IO_NODE_DISPLAY_NAME_MAPPINGS,
)
from .save_nodes import (
    NODE_CLASS_MAPPINGS as SAVE_NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as SAVE_NODE_DISPLAY_NAME_MAPPINGS,
)
from . import auto_setup
from server import PromptServer

logger = logging.getLogger(__name__)

NODE_CLASS_MAPPINGS = {
    "RunflowDeploy": RunflowDeploy,
    **IO_NODE_CLASS_MAPPINGS,
    **SAVE_NODE_CLASS_MAPPINGS,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RunflowDeploy": "Runflow Deploy",
    **IO_NODE_DISPLAY_NAME_MAPPINGS,
    **SAVE_NODE_DISPLAY_NAME_MAPPINGS,
}

WEB_DIRECTORY = "./js"

# ---------------------------------------------------------------------------
# Security: Basic HTTP authentication
# ---------------------------------------------------------------------------

SECURITY_FILE = os.path.join(folder_paths.base_path, "runflow_security.json")
_SECURITY_DEFAULT = {"enabled": False, "username": "", "password": ""}


def _load_security():
    """Always read fresh from disk so the middleware never uses stale values."""
    try:
        with open(SECURITY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_SECURITY_DEFAULT)


def _save_security(settings):
    with open(SECURITY_FILE, "w") as f:
        json.dump(settings, f, indent=2)


@web.middleware
async def basic_auth_middleware(request, handler):
    settings = _load_security()

    # Only enforce when fully configured
    if (
        not settings.get("enabled")
        or not settings.get("username")
        or not settings.get("password")
    ):
        return await handler(request)

    # Let CORS preflight through
    if request.method == "OPTIONS":
        return await handler(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
            if username == settings["username"] and password == settings["password"]:
                return await handler(request)
        except Exception:
            logger.warning("Failed to decode Basic auth header", exc_info=True)

    logger.debug(
        "Auth rejected for %s %s (user=%r)",
        request.method,
        request.path,
        settings.get("username"),
    )
    return web.Response(
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="ComfyUI"'},
        text="Unauthorized",
    )


# Register middleware before the app is frozen (custom nodes load before startup)
try:
    PromptServer.instance.app._middlewares.append(basic_auth_middleware)
except Exception:
    logger.warning("Could not register auth middleware")


@PromptServer.instance.routes.get("/runflow/security")
async def get_security(request):
    return web.json_response(_load_security())


@PromptServer.instance.routes.post("/runflow/security")
async def post_security(request):
    data = await request.json()
    settings = {
        "enabled": bool(data.get("enabled", False)),
        "username": str(data.get("username", "")),
        "password": str(data.get("password", "")),
    }
    _save_security(settings)
    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------------
# Security: public port-exposure scan
# ---------------------------------------------------------------------------
#
# ComfyUI ships no authentication by default and its custom nodes can execute
# arbitrary code, so a ComfyUI instance reachable from the public internet is a
# real remote-code-execution risk. This scan asks a public service whether the
# machine's ports are reachable from the outside and warns the user.
#
# Robustness: the route never raises — it always returns HTTP 200 with a
# structured body. Public-IP lookup falls back across several services; the
# external port check is best-effort; and the listen-binding status (read
# locally from ComfyUI's CLI args, no network needed) is *always* reported so
# the UI can warn about exposure even when every external service is down.

# Ports worth checking in addition to ComfyUI's own listen port.
_EXTRA_SCAN_PORTS = [80, 8080, 443]

# Public "what is my IP" services, tried in order until one returns an IPv4.
_PUBLIC_IP_SERVICES = [
    ("https://api.ipify.org?format=json", "json"),  # {"ip": "..."}
    ("https://ifconfig.me/ip", "text"),
    ("https://icanhazip.com", "text"),
    ("https://checkip.amazonaws.com", "text"),
]

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


async def _detect_public_ip(session):
    """Return the server's public IPv4, or None if every service fails."""
    for url, kind in _PUBLIC_IP_SERVICES:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    continue
                if kind == "json":
                    data = await resp.json(content_type=None)
                    ip = str((data or {}).get("ip", "")).strip()
                else:
                    ip = (await resp.text()).strip()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            continue
        if _IPV4_RE.match(ip):
            return ip
    return None


def _detect_listen():
    """Return ``(listen, port, bound_all_interfaces)`` from ComfyUI's CLI args.

    Guarded: if ``comfy.cli_args`` can't be imported we fall back to ComfyUI's
    documented defaults (``127.0.0.1``:``8188`` — local-only). ``--listen`` with
    no value binds ``0.0.0.0,::`` (all interfaces), the dangerous case.
    """
    listen, port = "127.0.0.1", 8188
    try:
        from comfy.cli_args import args
        listen = str(getattr(args, "listen", None) or listen)
        port = int(getattr(args, "port", None) or port)
    except Exception:
        logger.debug("Runflow: comfy.cli_args unavailable; assuming defaults", exc_info=True)
    bound_all = ("0.0.0.0" in listen) or ("::" in listen)
    return listen, port, bound_all


async def _scan_via_portchecker(session, ip, ports):
    """Primary external scanner (portchecker.io batch API).

    Returns ``[{"port", "status"}, ...]`` or None if the service is unusable.
    """
    try:
        async with session.post(
            "https://portchecker.io/api/v1/query",
            json={"host": ip, "ports": ports},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    check = (data or {}).get("check")
    if not isinstance(check, list):
        return None
    out = []
    for c in check:
        try:
            out.append({"port": int(c["port"]), "status": bool(c["status"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out or None


@PromptServer.instance.routes.post("/runflow/port-scan")
async def port_scan(request):
    """Report whether this machine's ports are reachable from the public internet.

    Never raises: returns HTTP 200 with a structured body. ``bound_all_interfaces``
    (local, always known) lets the UI warn even when external services fail; a
    non-null ``error`` signals that the external scan couldn't be completed.
    """
    listen, port, bound_all = _detect_listen()
    ports = sorted({port, *_EXTRA_SCAN_PORTS})

    ip = None
    results = None
    source = None
    async with aiohttp.ClientSession() as session:
        ip = await _detect_public_ip(session)
        if ip:
            results = await _scan_via_portchecker(session, ip, ports)
            if results is not None:
                source = "portchecker.io"

    body = {
        "ip": ip,
        "listen": listen,
        "port": port,
        "bound_all_interfaces": bound_all,
        "scanned_ports": ports,
        "source": source,
        "ports": results or [],
        "error": None,
    }
    if not ip:
        body["error"] = (
            "Could not determine this machine's public IP address — "
            "check your internet connection."
        )
    elif results is None:
        body["error"] = (
            "The external port-check service is unavailable right now. "
            "The listen-binding status below is still accurate."
        )
    return web.json_response(body)


@PromptServer.instance.routes.post("/runflow/build-inputs-zip")
async def build_inputs_zip(request):
    """Zip every file under ComfyUI/input/ whose filename appears in the
    workflow JSON payload. Same substring-match pattern we use for models.
    """
    body = await request.json()
    workflow_str = json.dumps(body.get("workflow_json", {}))
    input_dir = folder_paths.get_input_directory()

    included = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, filenames in os.walk(input_dir):
            for fname in filenames:
                if fname in workflow_str:
                    full = os.path.join(root, fname)
                    arcname = os.path.relpath(full, input_dir)
                    zf.write(full, arcname)
                    included.append(arcname)

    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return web.json_response({"inputs_zip_b64": b64, "files": included})


def _collect_input_files(workflow_str: str) -> list[str]:
    """Return absolute paths under ComfyUI/input/ whose filename appears in the workflow."""
    input_dir = folder_paths.get_input_directory()
    found = []
    for root, _dirs, filenames in os.walk(input_dir):
        for fname in filenames:
            if fname in workflow_str:
                found.append(os.path.join(root, fname))
    return found


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@PromptServer.instance.routes.post("/runflow/upload-workflow-resources")
async def upload_workflow_resources(request):
    """Upload every file under ComfyUI/input/ referenced by the workflow JSON
    via Runflow's asset management API. Returns a list of resource records
    suitable for the deploy payload's top-level ``resources`` field.

    Request body: {"host", "api_key", "workflow_json"}.
    Response: {"resources": [{"filename", "asset_id", "url", "mime_type",
    "size_bytes", "sha256"}, ...]}.
    """
    body = await request.json()
    host = body["host"].rstrip("/")
    api_key = body.get("api_key", "")
    workflow_str = json.dumps(body.get("workflow_json", {}))
    if not api_key:
        return web.json_response({"error": "api_key is required"}, status=400)

    input_dir = folder_paths.get_input_directory()
    paths = _collect_input_files(workflow_str)

    resources = {}
    headers = {"Authorization": f"Bearer {api_key}"}
    async with aiohttp.ClientSession() as session:
        for full_path in paths:
            filename = os.path.basename(full_path)
            rel_path = os.path.relpath(full_path, input_dir)
            try:
                size_bytes = os.path.getsize(full_path)
            except OSError:
                continue
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            digest = _sha256_file(full_path)

            create_resp = await session.post(
                f"{host}/v1/asset-uploads",
                headers={**headers, "Content-Type": "application/json"},
                json={"filename": filename, "mime_type": mime_type, "size_bytes": size_bytes},
            )
            if create_resp.status != 201:
                logger.warning(
                    "asset-uploads create failed for %s (status=%s)",
                    filename, create_resp.status,
                )
                continue
            create_data = await create_resp.json()
            asset_id = create_data["asset_id"]
            upload_url = create_data["upload_url"]

            with open(full_path, "rb") as f:
                file_bytes = f.read()
            put_resp = await session.put(
                upload_url,
                data=file_bytes,
                headers={"Content-Type": mime_type},
            )
            if put_resp.status not in (200, 201, 204):
                logger.warning(
                    "presigned PUT failed for %s (status=%s)",
                    filename, put_resp.status,
                )
                continue

            confirm_resp = await session.post(
                f"{host}/v1/asset-uploads/{asset_id}/confirmations",
                headers={**headers, "Content-Type": "application/json"},
                json={},
            )
            if confirm_resp.status != 201:
                logger.warning(
                    "asset-uploads confirm failed for %s (status=%s)",
                    filename, confirm_resp.status,
                )
                continue
            asset = await confirm_resp.json()
            resources[rel_path] = {
                "asset_id": asset_id,
                "url": asset.get("url"),
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "sha256": digest,
            }

    return web.json_response({"resources": resources})


@PromptServer.instance.routes.post("/runflow/upload-input-file")
async def upload_input_file(request):
    """Save an uploaded file into ComfyUI's input/ directory and return its
    name. Backs the ``Runflow Input (File)`` node's upload button and the local
    playground's file control — a generic alternative to ``/upload/image`` that
    accepts any file type.

    Request: ``multipart/form-data`` with a single ``file`` field.
    Response: ``{"name": "<filename>"}``.
    """
    reader = await request.multipart()
    field = await reader.next()
    while field is not None and field.name != "file":
        field = await reader.next()
    if field is None:
        return web.json_response({"error": "missing 'file' field"}, status=400)

    # Strip any directory components (and Windows separators, which
    # os.path.basename leaves untouched on POSIX) so the upload can't escape
    # the input directory.
    filename = os.path.basename((field.filename or "").replace("\\", "/"))
    if not filename:
        return web.json_response({"error": "missing filename"}, status=400)

    input_dir = folder_paths.get_input_directory()
    os.makedirs(input_dir, exist_ok=True)
    dest = os.path.join(input_dir, filename)
    if os.path.realpath(os.path.dirname(dest)) != os.path.realpath(input_dir):
        return web.json_response({"error": "invalid filename"}, status=400)

    with open(dest, "wb") as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            f.write(chunk)

    return web.json_response({"name": filename})


@PromptServer.instance.routes.post("/runflow/resolve-models")
async def post_resolve_models(request):
    """Resolve every model the workflow uses and hash the local files.

    Request body: ``{"graph": {...workflow graph with widgets_values...}}``.
    Response: ``{"models": {"<folder_type>/<rel>": {"url"?, "sha256"?}, ...}}``.

    Primary detection scans ``widgets_values`` against the locally-installed
    model index (this catches dropdown selections); the unreliable
    ``properties.models`` cache is consulted as a fallback. URLs are
    propagated from ``properties.models`` when available.
    """
    body = await request.json()
    graph = body.get("graph") or {}
    return web.json_response({"models": resolve_workflow_models(graph)})


@PromptServer.instance.routes.get("/runflow/system-info")
async def get_system_info(request):
    installed_nodes = RunflowDeploy.get_custom_nodes()
    # Augment with comfy-env-root.toml [node_reqs] dependencies so the deploy
    # manifest is self-contained even when a node's transitive deps haven't
    # been installed locally yet (Pozzetti 3D-pipeline family).
    deploy_nodes = await resolve_hoisted_custom_nodes(installed_nodes)
    return web.json_response({
        "packages": RunflowDeploy.get_installed_packages(),
        "cached_models": RunflowDeploy.get_cached_models(),
        "models_directory": RunflowDeploy.get_models_directory(),
        "custom_nodes": deploy_nodes,
        "comfyui": RunflowDeploy.get_comfyui_git_info(),
    })


@PromptServer.instance.routes.post("/runflow/apply-outputs")
async def apply_outputs(request):
    """Download remote outputs to the local output directory and update the UI."""
    data = await request.json()
    host = data["host"].rstrip("/")
    api_key = data.get("api_key", "")
    outputs = data.get("outputs", {})
    prompt_id = data.get("prompt_id", str(uuid.uuid4()))

    output_dir = folder_paths.get_output_directory()
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"

    async with aiohttp.ClientSession(headers=headers) as session:
        for node_id, node_output in outputs.items():
            if "images" not in node_output:
                continue

            for image_info in node_output["images"]:
                filename = image_info["filename"]
                subfolder = image_info.get("subfolder", "")
                file_type = image_info.get("type", "output")

                # Download from remote server
                params = {
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": file_type,
                }
                async with session.get(f"{host}/view", params=params) as resp:
                    if resp.status != 200:
                        continue
                    content = await resp.read()

                # Save to local output directory
                local_dir = os.path.join(output_dir, subfolder) if subfolder else output_dir
                os.makedirs(local_dir, exist_ok=True)
                local_path = os.path.join(local_dir, filename)
                with open(local_path, "wb") as f:
                    f.write(content)

                # Register in the Assets tab
                try:
                    from app.assets.services.ingest import register_file_in_place
                    register_file_in_place(local_path, filename, ["output", "runflow"])
                except Exception:
                    pass

            # Notify the frontend so the output node displays the images
            PromptServer.instance.send_sync("executed", {
                "node": node_id,
                "display_node": node_id,
                "output": node_output,
                "prompt_id": prompt_id,
            })

    return web.json_response({"status": "ok", "prompt_id": prompt_id})


# ---------------------------------------------------------------------------
# Auto setup: plan, start, cancel, and restart
# ---------------------------------------------------------------------------


def _broadcast_auto_setup(event: dict) -> None:
    """Push a progress event to every connected ComfyUI client.

    ``send_sync`` schedules onto the running loop, so this is safe to call
    from any task or thread.
    """
    PromptServer.instance.send_sync("runflow.auto_setup", event)


async def _send_event(event: dict) -> None:
    _broadcast_auto_setup(event)


@PromptServer.instance.routes.post("/runflow/auto-setup/plan")
async def post_auto_setup_plan(request):
    body = await request.json()
    graph = body.get("graph") or {}
    plan = await auto_setup.plan_setup(graph)
    return web.json_response(plan)


@PromptServer.instance.routes.post("/runflow/auto-setup/start")
async def post_auto_setup_start(request):
    body = await request.json()
    plan = body.get("plan") or {}
    options = body.get("options") or {}
    job_id = auto_setup.start_job(options, plan, _send_event)
    return web.json_response({"job_id": job_id})


@PromptServer.instance.routes.post("/runflow/auto-setup/cancel")
async def post_auto_setup_cancel(request):
    body = await request.json()
    job_id = body.get("job_id") or ""
    ok = auto_setup.cancel_job(job_id)
    return web.json_response({"ok": ok})


@PromptServer.instance.routes.post("/runflow/restart")
async def post_restart(request):
    """Schedule a server restart on a short delay so the HTTP response can
    flush before ``os.execv`` replaces the process image. The frontend polls
    ``/system_stats`` to detect that the new process is up, then full-reloads.
    """
    asyncio.create_task(auto_setup.schedule_restart(delay=0.25))
    return web.json_response({"status": "restarting"})


# ---------------------------------------------------------------------------
# Local Playground: test a workflow through its Runflow Input/Output surface
# without touching the node graph. See README "Local Playground" and
# docs/exec-plans/active/2026-05-29-comfyui-runflow-local-playground.md.
# ---------------------------------------------------------------------------

# Process-memory caches. Lost on restart — recapturing via the Local Playground
# button on the Deploy node refreshes them.
_PLAYGROUND_WORKFLOWS: dict[str, dict] = {}
_PLAYGROUND_RUNS: dict[str, dict] = {}

_PLAYGROUND_DIR = Path(__file__).resolve().parent / "js" / "playground"
_PLAYGROUND_RUN_TIMEOUT_S = 30 * 60  # 30 minutes, matches the cloud worker

# Runflow Input variants — class-suffix → wire type. Keep in sync with
# io_nodes._INPUT_TYPES (plus the standalone RunflowInputFile).
_RUNFLOW_INPUT_TYPES = {"String", "Int", "Float", "Boolean", "Image", "File"}

# Extension → preview kind. Buckets returned by ComfyUI's history outputs
# already give us the kind for the common cases (images / gifs / audios /
# videos / model_files); extensions backstop the "files" bucket where the
# kind has to be inferred.
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv"}
_AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a"}
_THREED_EXTS = {".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx", ".usdz"}

_PLAYGROUND_INDEX_TEMPLATE: str | None = None


def _load_playground_index_template() -> str:
    """Read js/playground/index.html once and cache it. Raise if missing — the
    plugin install is broken if the template is gone, and the request handler
    should surface a 500 rather than serve an empty page."""
    global _PLAYGROUND_INDEX_TEMPLATE
    if _PLAYGROUND_INDEX_TEMPLATE is None:
        _PLAYGROUND_INDEX_TEMPLATE = (_PLAYGROUND_DIR / "index.html").read_text(encoding="utf-8")
    return _PLAYGROUND_INDEX_TEMPLATE


def _runflow_input_type(class_type: str) -> str | None:
    """``RunflowInputString`` → ``"STRING"``. None for anything that isn't a
    recognised Runflow Input class."""
    if not class_type.startswith("RunflowInput"):
        return None
    suffix = class_type[len("RunflowInput"):]
    return suffix.upper() if suffix in _RUNFLOW_INPUT_TYPES else None


def _runflow_output_type(class_type: str) -> str | None:
    """``RunflowOutputImage`` → ``"IMAGE"``. None for non-output classes."""
    if not class_type.startswith("RunflowOutput"):
        return None
    return class_type[len("RunflowOutput"):].upper()


def _is_link(value) -> bool:
    """Prompt-JSON socket links are ``[upstream_node_id, slot_index]``."""
    return isinstance(value, list) and len(value) == 2


def _extract_playground_schema(workflow_json: dict) -> tuple[list[dict], list[dict]]:
    """Walk the captured prompt JSON for ``RunflowInput*`` / ``RunflowOutput*``
    nodes. Returns ``(inputs, outputs)``.

    Each input row: ``{input_id, type, display_name, description, default_value}``.
    Each output row: ``{output_id, type, output_name}``.

    Skips nodes with empty ``input_id`` / ``output_id`` so a half-configured
    graph still loads — the form just renders fewer fields.
    """
    inputs: list[dict] = []
    outputs: list[dict] = []
    for node in workflow_json.values():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type", "")
        node_inputs = node.get("inputs") or {}

        in_type = _runflow_input_type(class_type)
        if in_type is not None:
            input_id = str(node_inputs.get("input_id") or "").strip()
            if not input_id:
                continue
            # FILE carries its local default in the `file_name` widget rather
            # than the `value` socket (which is the run-time injection point).
            raw_default = node_inputs.get("file_name") if in_type == "FILE" else node_inputs.get("value")
            default_value = None if _is_link(raw_default) else raw_default
            inputs.append({
                "input_id": input_id,
                "type": in_type,
                "display_name": str(node_inputs.get("display_name") or "").strip() or input_id,
                "description": str(node_inputs.get("description") or "").strip(),
                "default_value": default_value,
            })
            continue

        out_type = _runflow_output_type(class_type)
        if out_type is not None:
            output_id = str(node_inputs.get("output_id") or "").strip()
            if not output_id:
                continue
            outputs.append({
                "output_id": output_id,
                "type": out_type,
                "output_name": str(node_inputs.get("output_name") or "").strip() or output_id,
            })
    return inputs, outputs


def _coerce_input_value(in_type: str, raw):
    """Cast a form-submitted value into the type the ComfyUI socket expects.
    Raises ``ValueError`` with a user-facing message on mismatch."""
    if in_type in ("STRING", "FILE"):
        # FILE is a filename string in the graph; the upload widget supplies it.
        return "" if raw is None else str(raw)
    if in_type == "INT":
        if isinstance(raw, bool):
            raise ValueError("expected an integer, got a boolean")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expected an integer, got {raw!r}") from exc
    if in_type == "FLOAT":
        if isinstance(raw, bool):
            raise ValueError("expected a number, got a boolean")
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expected a number, got {raw!r}") from exc
    if in_type == "BOOLEAN":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            if raw.lower() in ("true", "1", "yes", "on"):
                return True
            if raw.lower() in ("false", "0", "no", "off", ""):
                return False
        raise ValueError(f"expected a boolean, got {raw!r}")
    raise ValueError(f"unsupported input type: {in_type}")


def _allocate_node_id(prompt: dict) -> str:
    """Return a fresh string node id that doesn't collide with existing keys.

    Strategy: take the max numeric existing id (ignoring non-numeric keys
    from subgraph instances etc.), add 1. If no numeric keys exist at all,
    start at 1000 to stay clear of the user's hand-typed range.
    """
    max_id = 0
    for key in prompt:
        if isinstance(key, str) and key.isdigit():
            max_id = max(max_id, int(key))
    candidate = max_id + 1 if max_id else 1000
    while str(candidate) in prompt:
        candidate += 1
    return str(candidate)


def _inject_image_input(prompt: dict, target_node: dict, filename: str) -> None:
    """Wire a fresh ``LoadImage`` node into ``target_node.inputs.value``.

    The ``RunflowInputImage`` socket is typed ``IMAGE`` — a literal filename
    string would fail ComfyUI's type check. ``LoadImage`` reads the file from
    ``input/`` (where ``POST /upload/image`` puts it) and emits the IMAGE
    tensor the rest of the graph expects.
    """
    new_id = _allocate_node_id(prompt)
    prompt[new_id] = {
        "class_type": "LoadImage",
        "inputs": {
            "image": filename,
            "upload": "image",
        },
    }
    target_node.setdefault("inputs", {})["value"] = [new_id, 0]


def _inject_run_values(prompt: dict, inputs_schema: list[dict], values: dict) -> list[str]:
    """Apply form values to the prompt JSON in place. Returns the list of
    missing required ``input_id``s — the caller turns that into a 400.

    For each ``RunflowInput*`` node we look up its declared ``input_id`` and:
    overwrite ``inputs.value`` with the form value (drops any existing link
    by construction) for scalars, or inject a ``LoadImage`` for ``IMAGE``.
    """
    by_input_id: dict[str, list[tuple[str, dict, str]]] = {}
    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            continue
        in_type = _runflow_input_type(node.get("class_type", ""))
        if in_type is None:
            continue
        input_id = str((node.get("inputs") or {}).get("input_id") or "").strip()
        if not input_id:
            continue
        by_input_id.setdefault(input_id, []).append((node_id, node, in_type))

    missing: list[str] = []
    for schema in inputs_schema:
        input_id = schema["input_id"]
        in_type = schema["type"]
        if input_id not in values or values[input_id] in (None, ""):
            # IMAGE without a file is missing; for the other types the empty
            # string is still a legitimate-if-odd value, but we reject it for
            # consistency with the form's required-field rule.
            missing.append(input_id)
            continue
        for _node_id, node, node_type in by_input_id.get(input_id, []):
            if node_type == "IMAGE":
                _inject_image_input(prompt, node, str(values[input_id]))
            else:
                node.setdefault("inputs", {})["value"] = _coerce_input_value(node_type, values[input_id])
    return missing


def _classify_output_kind(bucket: str, filename: str) -> str:
    """Map a (history bucket, filename) pair to a preview kind."""
    if bucket == "images" or bucket == "gifs":
        return "image"
    if bucket == "videos":
        return "video"
    if bucket == "audios":
        return "audio"
    if bucket == "model_files":
        return "3d"
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _THREED_EXTS:
        return "3d"
    return "file"


def _build_view_url(entry: dict) -> str:
    """Build a relative ``/view`` URL for an output entry. Relative so the
    page works regardless of which host/port the user opened it on."""
    params = urllib.parse.urlencode({
        "filename": str(entry.get("filename") or ""),
        "subfolder": str(entry.get("subfolder") or ""),
        "type": str(entry.get("type") or "output"),
    })
    return f"/view?{params}"


def _build_outputs_from_history(
    workflow_json: dict,
    outputs_schema: list[dict],
    history_outputs: dict,
) -> dict:
    """Walk every cached output node, look up its history entry, and produce
    ``{output_id: [{kind, url, filename}, ...]}``. Missing entries become
    empty lists so the frontend can still render an "empty output" pill.
    """
    output_id_by_node_id: dict[str, str] = {}
    for node_id, node in workflow_json.items():
        if not isinstance(node, dict):
            continue
        if _runflow_output_type(node.get("class_type", "")) is None:
            continue
        output_id = str((node.get("inputs") or {}).get("output_id") or "").strip()
        if output_id:
            output_id_by_node_id[node_id] = output_id

    by_output: dict[str, list[dict]] = {o["output_id"]: [] for o in outputs_schema}
    for node_id, node_output in history_outputs.items():
        output_id = output_id_by_node_id.get(node_id)
        if output_id is None or not isinstance(node_output, dict):
            continue
        for bucket, entries in node_output.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                filename = str(entry.get("filename") or "")
                if not filename:
                    continue
                by_output.setdefault(output_id, []).append({
                    "kind": _classify_output_kind(bucket, filename),
                    "url": _build_view_url(entry),
                    "filename": filename,
                })
    return by_output


async def _drive_playground_run(run_id: str, slug: str, prompt: dict, client_id: str) -> None:
    """Run the modified prompt against the local ComfyUI: open WS → POST
    /prompt → drain until completion → GET /history → store parsed outputs.

    Mirrors ``bg-brain/workers/comfyui-deploy-worker/comfyui_runner.py``'s
    proven loop, scaled down to a single in-process call.
    """
    try:
        _, port, _ = _detect_listen()
        base = f"http://127.0.0.1:{port}"
        ws_uri = f"ws://127.0.0.1:{port}/ws?clientId={client_id}"

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_uri) as ws:
                async with session.post(
                    f"{base}/prompt",
                    json={"prompt": prompt, "client_id": client_id},
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:500]
                        raise RuntimeError(f"ComfyUI rejected prompt ({resp.status}): {body}")
                    payload = await resp.json()
                    prompt_id = payload.get("prompt_id")
                    if not isinstance(prompt_id, str):
                        raise RuntimeError(f"ComfyUI /prompt response missing prompt_id: {payload!r}")

                await _drain_until_complete(ws, prompt_id)

            async with session.get(f"{base}/history/{prompt_id}") as resp:
                resp.raise_for_status()
                history = await resp.json()

        entry = (history or {}).get(prompt_id, {})
        status = entry.get("status") or {}
        if isinstance(status, dict) and status.get("status_str") == "error":
            message = _summarize_execution_error(status.get("messages") or [])
            raise RuntimeError(f"workflow errored: {message}")

        cached = _PLAYGROUND_WORKFLOWS.get(slug) or {}
        workflow_json = cached.get("workflow_json") or {}
        outputs_schema = cached.get("outputs") or []
        outputs = _build_outputs_from_history(workflow_json, outputs_schema, entry.get("outputs") or {})

        _PLAYGROUND_RUNS[run_id] = {
            "status": "succeeded",
            "slug": slug,
            "prompt_id": prompt_id,
            "outputs": outputs,
        }
    except asyncio.CancelledError:
        _PLAYGROUND_RUNS[run_id] = {"status": "failed", "slug": slug, "error": "cancelled"}
        raise
    except Exception as exc:
        logger.exception("Runflow playground run %s failed", run_id)
        _PLAYGROUND_RUNS[run_id] = {"status": "failed", "slug": slug, "error": str(exc)}


async def _drain_until_complete(ws, prompt_id: str) -> None:
    """Drain ``ws`` until ComfyUI emits ``executing`` with ``data.node == null``
    for our ``prompt_id``. Times out after ``_PLAYGROUND_RUN_TIMEOUT_S``."""
    deadline = time.monotonic() + _PLAYGROUND_RUN_TIMEOUT_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"workflow did not complete within {_PLAYGROUND_RUN_TIMEOUT_S}s")
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"workflow did not complete within {_PLAYGROUND_RUN_TIMEOUT_S}s") from exc
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            raise RuntimeError("ComfyUI WebSocket closed before workflow completion event")
        if msg.type == aiohttp.WSMsgType.ERROR:
            raise RuntimeError(f"ComfyUI WebSocket error: {ws.exception()!r}")
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except (TypeError, ValueError):
            continue
        if (
            data.get("type") == "executing"
            and (data.get("data") or {}).get("node") is None
            and (data.get("data") or {}).get("prompt_id") == prompt_id
        ):
            return


def _summarize_execution_error(messages: list) -> str:
    """Pluck the first ``execution_error`` / ``execution_interrupted`` payload
    out of ComfyUI's ``status.messages`` list."""
    for msg in messages:
        if not isinstance(msg, list) or len(msg) < 2:
            continue
        msg_type, payload = msg[0], msg[1]
        if msg_type in ("execution_error", "execution_interrupted") and isinstance(payload, dict):
            node_type = payload.get("node_type", "?")
            exception_message = payload.get("exception_message") or payload.get("error", "")
            return f"{msg_type} in {node_type}: {str(exception_message)[:300]}"
    return "unknown error (no execution_error message in ComfyUI status)"


@PromptServer.instance.routes.post("/runflow/playground/workflows/{slug}")
async def post_playground_workflow(request):
    """Cache a captured workflow + extracted schema, keyed by slug."""
    slug = request.match_info["slug"]
    body = await request.json()
    workflow_json = body.get("workflow_json")
    if not isinstance(workflow_json, dict) or not workflow_json:
        return web.json_response({"error": "workflow_json is required"}, status=400)
    inputs, outputs = _extract_playground_schema(workflow_json)
    _PLAYGROUND_WORKFLOWS[slug] = {
        "endpoint_name": str(body.get("endpoint_name") or slug),
        "captured_at": time.time(),
        "workflow_json": workflow_json,
        "inputs": inputs,
        "outputs": outputs,
    }
    return web.json_response({
        "ok": True,
        "slug": slug,
        "input_count": len(inputs),
        "output_count": len(outputs),
    })


@PromptServer.instance.routes.get("/runflow/playground/{slug}")
async def get_playground_page(request):
    """Serve the playground HTML shell with bootstrap JSON injected."""
    slug = request.match_info["slug"]
    cached = _PLAYGROUND_WORKFLOWS.get(slug)
    if cached is None:
        return web.Response(
            status=404,
            text=(
                "Runflow Playground: no workflow cached for this slug. "
                "Open ComfyUI, click 'Local Playground' on the Runflow Deploy node, "
                "then reload."
            ),
            content_type="text/plain",
        )
    bootstrap = {
        "slug": slug,
        "endpoint_name": cached["endpoint_name"],
        "captured_at": cached["captured_at"],
        "inputs": cached["inputs"],
        "outputs": cached["outputs"],
    }
    try:
        template = _load_playground_index_template()
    except OSError as exc:
        logger.exception("Runflow playground: failed to read index.html")
        return web.Response(status=500, text=f"playground template missing: {exc}", content_type="text/plain")
    # Escape `</` so a user-supplied string (e.g. an input description containing
    # ``</script>``) can't break out of the embedded JSON script tag.
    safe_json = json.dumps(bootstrap).replace("</", "<\\/")
    html = template.replace("{{BOOTSTRAP_JSON}}", safe_json)
    return web.Response(text=html, content_type="text/html")


@PromptServer.instance.routes.get("/runflow/playground/_static/{filename}")
async def get_playground_static(request):
    """Serve playground.css / playground.js from js/playground/. The filename
    is hard-restricted to a basename allowlist so this can't be coaxed into a
    path-traversal — we never construct a path from arbitrary input.
    """
    filename = request.match_info["filename"]
    if filename not in {"playground.css", "playground.js"}:
        return web.Response(status=404, text="not found", content_type="text/plain")
    path = _PLAYGROUND_DIR / filename
    if not path.is_file():
        return web.Response(status=404, text="not found", content_type="text/plain")
    return web.FileResponse(path)


@PromptServer.instance.routes.post("/runflow/playground/{slug}/run")
async def post_playground_run(request):
    """Start a playground run. Returns ``{run_id}`` immediately; the
    background task drains the WS and stores the result in ``_PLAYGROUND_RUNS``.
    """
    slug = request.match_info["slug"]
    cached = _PLAYGROUND_WORKFLOWS.get(slug)
    if cached is None:
        return web.json_response({"error": "workflow not cached; reopen Local Playground"}, status=404)
    body = await request.json()
    values = body.get("values") or {}
    if not isinstance(values, dict):
        return web.json_response({"error": "values must be an object"}, status=400)

    prompt = copy.deepcopy(cached["workflow_json"])
    try:
        missing = _inject_run_values(prompt, cached["inputs"], values)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if missing:
        return web.json_response(
            {"error": f"missing required inputs: {', '.join(missing)}", "missing": missing},
            status=400,
        )

    run_id = uuid.uuid4().hex
    client_id = uuid.uuid4().hex
    _PLAYGROUND_RUNS[run_id] = {"status": "running", "slug": slug}
    asyncio.create_task(_drive_playground_run(run_id, slug, prompt, client_id))
    return web.json_response({"run_id": run_id})


@PromptServer.instance.routes.get("/runflow/playground/{slug}/runs/{run_id}")
async def get_playground_run(request):
    """Poll a playground run by id."""
    run_id = request.match_info["run_id"]
    record = _PLAYGROUND_RUNS.get(run_id)
    if record is None:
        return web.json_response({"error": "run not found"}, status=404)
    if record.get("slug") != request.match_info["slug"]:
        return web.json_response({"error": "run not found"}, status=404)
    return web.json_response(record)
