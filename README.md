# ComfyUI-Runflow

A ComfyUI custom-node plugin that publishes a workflow to [Runflow](https://runflow.io) as a callable API. The plugin captures the workflow graph, the runtime manifest (ComfyUI commit, custom-node commits, package versions, cached-model state), and uploads it to Runflow with a single click.

## Installation

Symlink or clone this repository into your ComfyUI `custom_nodes/` directory and restart ComfyUI:

```bash
cd <ComfyUI>/custom_nodes
git clone https://github.com/runflow-io/ComfyUI-Runflow.git
# or, for development against this checkout:
ln -s /path/to/ComfyUI-Runflow ComfyUI-Runflow
```

## Configuration

In ComfyUI: **Settings → Runflow → Connection**.

| Setting | Default | What it is |
|---------|---------|-----------|
| `Runflow.ApiUrl` | `https://api.runflow.io` | Runflow API base URL. Override when running against a local/staging stack. |
| `Runflow.ApiKey` | _(empty)_ | A `rf_live_*` API key issued for your organization. |

Both fields can also be overridden per-workflow on the `RunflowDeploy` node's widgets (`host`, `api_key`).

### API key scopes

Issue a key with these scopes — the Deploy button uses all three:

- `comfyui-workflows:read` (lookup-by-slug to detect existing rows)
- `comfyui-workflows:create` (first deploy)
- `comfyui-workflows:edit` (subsequent redeploys to the same slug)

`comfyui-workflows:delete` isn't used by Deploy but is required if you plan to remove workflows from this client later.

The plugin sends `Authorization: Bearer <key>` and lets the API derive your organization from the key — no organization UUID is needed.

## How Deploy works

1. Add a **Runflow Deploy** node to your workflow and set its `endpoint_name` widget. The slug is derived from this name (lowercased, URL-safe).
2. Click **Deploy**.

The HTTP response is the terminal state — there is no separate deployment job to poll.

## Workflow I/O

Place a `Runflow Input (…)` node for each value your endpoint takes and a `Runflow Output (…)` node for each artifact it returns. The `input_id` / `output_id` widgets are the stable keys callers use against the API.

| Node | Purpose |
|------|---------|
| `Runflow Input (String / Int / Float / Boolean / Image)` | Typed inputs. Locally each one is a pass-through; at deploy time the rewriter injects caller-supplied values. |
| `Runflow Output (Image)` | Saves each image in the batch as PNG to ComfyUI's output directory. |
| `Runflow Output (File)` | Marks a file already written under ComfyUI's `output/` directory (by an upstream save-* node) as the run's deliverable. Use for videos, 3D meshes, audio, archives, etc. Connect any node's filename/path string output to its `value` socket — subfolders relative to `output/` are supported. |

### Encoder bridges (Runflow/Save category)

These nodes encode ComfyUI native sockets to a file in `output/` and emit the relative filename on a STRING output socket — ready to wire into `Runflow Output (File)`.

| Node | Input | Format / codec |
|------|-------|----------------|
| `Runflow Save Audio (FLAC)` | AUDIO | FLAC, lossless |
| `Runflow Save Audio (MP3)` | AUDIO | MP3 / libmp3lame, with `quality` widget (`V0`, `128k`, `320k`) |
| `Runflow Save Audio (Opus)` | AUDIO | Opus / libopus, with bitrate widget (`64k`–`320k`) |
| `Runflow Save Video (MP4)` | VIDEO | MP4 / H.264 (delegates to `VIDEO.save_to`) |
| `Runflow Save Video (WEBM)` | IMAGE batch + fps | WebM / VP9 or AV1 — mirrors stock ComfyUI's `SaveWEBM` input shape (the VIDEO socket's `save_to` doesn't expose a WebM container yet) |

The audio nodes delegate to ComfyUI's own `AudioSaveHelper`, so encoding stays in lockstep with ComfyUI's stock `Save Audio` family. The MP4 node calls the VIDEO socket's own `save_to(...)` method. The WEBM node uses PyAV directly because the VIDEO socket only supports MP4/H.264 today.

Requires a recent ComfyUI (`comfy_api.latest` module). On older installs the audio and video save nodes are skipped at plugin load with a single warning — the rest of the plugin still works.

## Auto setup

The **Auto setup** button (directly below Deploy on the Runflow Deploy node) installs every custom node and downloads every model the active workflow needs. Useful when you load someone else's workflow and don't want to chase its dependencies by hand.

Clicking the button opens a modal with two checkboxes — *Install missing custom nodes* and *Download missing models* — and a Start button. While the job runs, the modal shows a byte-progress bar for the current download and a count-progress bar for the current custom-node install. Models and custom nodes run in parallel.

When the job finishes, click **Restart ComfyUI** in the modal: the server re-execs itself, the modal waits for it to come back, and the page full-reloads so the newly installed nodes register.

### Requirements

Auto setup assumes `python`, `pip`, and `git` are on PATH. It uses `git clone --depth=1` + `git fetch --depth=1 origin <sha>` + `git checkout <sha>` (falling back to a full clone if the host disables SHA-targeted fetches), then `python -m pip install -r requirements.txt` if the cloned repo carries one. Works on Linux, macOS, and Windows.

## Security

ComfyUI ships **no authentication** by default, and custom nodes can execute arbitrary code on the host. If you start ComfyUI with `--listen` (binding to `0.0.0.0`, i.e. all network interfaces) and the port is reachable from the public internet, anyone who finds it can run code on your machine. The Runflow security panel helps you detect and close that exposure.

In ComfyUI: **Settings → Runflow → Authentication**.

### Password authentication

| Setting | Default | What it is |
|---------|---------|-----------|
| `Enable password authentication` | _off_ | Require HTTP Basic auth for **all** requests to this ComfyUI server. |
| `Username` / `Password` | _(empty)_ | Credentials checked by the auth layer. |

When enabled with both a username and password set, a Basic-auth middleware guards every route (the credentials are stored in `runflow_security.json` next to your ComfyUI install). Browsers prompt once and cache the credentials for the session.

### Port exposure scan

Click **Run security scan** in the same panel to check whether this machine is exposed:

1. It reports ComfyUI's **listen binding** (read locally from ComfyUI's `--listen` / `--port` args). Binding to `0.0.0.0`/`::` (all interfaces) is flagged in red; `127.0.0.1` (local only) is green.
2. It looks up your **public IP** (via `api.ipify.org`, falling back to `ifconfig.me`, `icanhazip.com`, and `checkip.amazonaws.com`).
3. It asks the public service **portchecker.io** whether your ComfyUI port (plus `80`, `8080`, `443`) is reachable from the internet, and shows each port as OPEN or closed.

> **Privacy note:** the scan sends your public IP address to the third-party service portchecker.io so it can probe your ports from the outside. No workflow data is sent. If the external service is unreachable, the scan still reports the local listen-binding status so you aren't left without a signal.

**If a port shows OPEN** (or you're bound to all interfaces), close the exposure by any of:

- Enabling **password authentication** above.
- Restricting access with a firewall / security group, or putting ComfyUI behind an authenticated reverse proxy.
- Binding to localhost only — start ComfyUI **without** `--listen` (or with `--listen 127.0.0.1`).
