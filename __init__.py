import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import uuid
import zipfile

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


SCAN_PORTS = [8188, 80, 8080, 443]


@PromptServer.instance.routes.get("/runflow/public-ip")
async def get_public_ip(request):
    """Return the server's public IP address."""
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.ipify.org?format=json") as resp:
            data = await resp.json()
            return web.json_response({"ip": data["ip"]})


@PromptServer.instance.routes.post("/runflow/port-scan")
async def port_scan(request):
    """Check if common ports are publicly reachable on the server's public IP."""
    body = await request.json()
    ip = body.get("ip", "")
    if not ip:
        return web.json_response({"error": "ip is required"}, status=400)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://portchecker.io/api/v1/query",
            json={"host": ip, "ports": SCAN_PORTS},
        ) as resp:
            data = await resp.json()
            return web.json_response({
                "ip": ip,
                "ports": data.get("check", []),
            })


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
