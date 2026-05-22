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

## Auto setup

The **Auto setup** button (directly below Deploy on the Runflow Deploy node) installs every custom node and downloads every model the active workflow needs. Useful when you load someone else's workflow and don't want to chase its dependencies by hand.

Clicking the button opens a modal with two checkboxes — *Install missing custom nodes* and *Download missing models* — and a Start button. While the job runs, the modal shows a byte-progress bar for the current download and a count-progress bar for the current custom-node install. Models and custom nodes run in parallel.

When the job finishes, click **Restart ComfyUI** in the modal: the server re-execs itself, the modal waits for it to come back, and the page full-reloads so the newly installed nodes register.

### Requirements

Auto setup assumes `python`, `pip`, and `git` are on PATH. It uses `git clone --depth=1` + `git fetch --depth=1 origin <sha>` + `git checkout <sha>` (falling back to a full clone if the host disables SHA-targeted fetches), then `python -m pip install -r requirements.txt` if the cloned repo carries one. Works on Linux, macOS, and Windows.
