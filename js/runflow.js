import { app } from "../../scripts/app.js";
import { openAutoSetupModal } from "./auto_setup.js";

function resolveWidget(node, name, settingId, fallback = "") {
    const widget = node.widgets.find(w => w.name === name);
    const value = widget ? widget.value : "";
    if (value) return value;
    try {
        const setting = app.ui.settings.getSettingValue(settingId);
        if (setting) return setting;
    } catch (err) {
        console.warn(`[Runflow] could not read setting ${settingId}`, err);
    }
    return fallback;
}

function slugify(s) {
    return (s || "")
        .toLowerCase()
        .trim()
        .replace(/\s+/g, "-")
        .replace(/[^a-z0-9_-]/g, "")
        .replace(/-+/g, "-")
        .replace(/^[-_]+|[-_]+$/g, "");
}

// Strip the `value` socket on every RunflowInput* node. Applied to the
// save-time copy only — the local Run flow still needs the connections.
export function scrubRunflowInputs(output, graph) {
    for (const node of Object.values(output || {})) {
        if (node && typeof node.class_type === "string" && node.class_type.startsWith("RunflowInput")) {
            if (node.inputs && "value" in node.inputs) {
                delete node.inputs.value;
            }
        }
    }
    if (!graph) return;
    const linksToDrop = new Set();
    for (const n of graph.nodes || []) {
        if (!(typeof n.type === "string" && n.type.startsWith("RunflowInput"))) continue;
        for (const inp of n.inputs || []) {
            if (inp && inp.name === "value" && inp.link != null) {
                linksToDrop.add(inp.link);
                inp.link = null;
            }
        }
    }
    if (Array.isArray(graph.links) && linksToDrop.size) {
        graph.links = graph.links.filter(l => !linksToDrop.has(Array.isArray(l) ? l[0] : l.id));
    }
}

// Resolve every model the workflow uses by asking the Python side to walk
// `widgets_values` against the local model index (primary) and fall back to
// `properties.models` (which is unreliable when the user changes dropdowns).
// Returns the dict keyed by `"<folder_type>/<rel>"` ready for the deploy body.
export async function resolveWorkflowModels(graph) {
    try {
        const resp = await fetch("/runflow/resolve-models", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ graph }),
        });
        if (!resp.ok) {
            console.warn(`[Runflow] /runflow/resolve-models failed (${resp.status})`);
            return {};
        }
        const { models = {} } = await resp.json();
        return models;
    } catch (err) {
        console.warn("[Runflow] resolve-models request failed", err);
        return {};
    }
}

function resolveCredentials(node) {
    const host = resolveWidget(node, "host", "Runflow.ApiUrl", "");
    const apiKey = resolveWidget(node, "api_key", "Runflow.ApiKey", "");
    if (!host) {
        alert("Runflow: API URL is not configured (set it in Settings → Runflow).");
        return null;
    }
    if (!apiKey) {
        alert("Runflow: API key is not configured (set it in Settings → Runflow).");
        return null;
    }
    return { host: host.replace(/\/$/, ""), apiKey };
}

function authHeaders(apiKey, withContentType = false) {
    const headers = { Authorization: `Bearer ${apiKey}` };
    if (withContentType) headers["Content-Type"] = "application/json";
    return headers;
}

// Format the Runflow API's structured error envelope `{code, message, errors[]}`
// into a single human-readable line for an alert. Falls back to the HTTP status
// when the body isn't JSON (e.g. CDN / proxy intercepts).
async function formatApiError(resp) {
    let envelope = null;
    try {
        envelope = await resp.json();
    } catch {
        // not JSON — fall through
    }
    if (!envelope || typeof envelope !== "object") {
        return `HTTP ${resp.status}`;
    }
    const code = envelope.code || `HTTP_${resp.status}`;
    const msg = envelope.message || "(no message)";
    const first = Array.isArray(envelope.errors) ? envelope.errors[0] : null;
    if (first && first.msg) {
        const loc = Array.isArray(first.loc) ? first.loc.join(".") : "";
        return `${code}: ${msg}${loc ? ` (${loc}: ${first.msg})` : ` (${first.msg})`}`;
    }
    return `${code}: ${msg}`;
}

// ---------- shared state + button helpers ----------

function state(node) {
    if (!node._runflow) {
        node._runflow = { deployStatus: "unknown", runStatus: "idle" };
    }
    return node._runflow;
}

function findButton(node, prefix) {
    return node.widgets.find(
        w => w.type === "button" && typeof w.name === "string" && w.name.startsWith(prefix)
    );
}

function setButtonLabel(node, prefix, label) {
    const btn = findButton(node, prefix);
    if (!btn) return;
    btn.name = label;
    node.setDirtyCanvas(true, true);
}

function refreshRunButton(node) {
    const s = state(node);
    if (s.runStatus !== "idle") return;
    setButtonLabel(node, "Run", s.deployStatus === "ready" ? "Run" : "Run (deploy first)");
}

// ---------- existence check ----------

// Look up an existing workflow by slug in the caller's organization.
// Auth: Bearer token. The API derives the org from the key, and list endpoints
// are org-scoped via CrudProxy.build_scope_filter — the slug filter therefore
// runs only against rows owned by that org.
async function findExistingBySlug(host, apiKey, slug) {
    const q = encodeURIComponent(`slug.eq:'${slug}'`);
    const url = `${host}/v1/comfyui-workflows?q=${q}&limit=1`;
    const resp = await fetch(url, { headers: authHeaders(apiKey) });
    if (!resp.ok) {
        throw new Error(await formatApiError(resp));
    }
    const body = await resp.json();
    const items = Array.isArray(body?.items) ? body.items : [];
    return items[0] || null;
}

// ---------- deploy ----------

async function deployWorkflow(node) {
    const creds = resolveCredentials(node);
    if (!creds) return;
    const { host, apiKey } = creds;

    const endpointName = node.widgets.find(w => w.name === "endpoint_name")?.value || "default";
    const slug = slugify(endpointName);
    if (!slug) {
        alert(
            "Runflow: endpoint name must be URL-safe (a-z, 0-9, '-', '_') " +
            "and unique within your organization."
        );
        return;
    }

    setButtonLabel(node, "Deploy", "Deploying…");

    try {
        const graphPrompt = await app.graphToPrompt();
        const sysInfoResp = await fetch("/runflow/system-info");
        if (!sysInfoResp.ok) {
            throw new Error(`/runflow/system-info returned ${sysInfoResp.status}`);
        }
        const sysInfo = await sysInfoResp.json();

        // Save a scrubbed copy (no local wires into RunflowInput.value) so the
        // deployed workflow represents just its API surface. Deep-copy first so
        // the live graph in the UI stays connected.
        const scrubbedOutput = JSON.parse(JSON.stringify(graphPrompt.output));
        const scrubbedGraph = JSON.parse(JSON.stringify(graphPrompt.workflow));
        scrubRunflowInputs(scrubbedOutput, scrubbedGraph);
        const models = await resolveWorkflowModels(scrubbedGraph);

        let resources = {};
        try {
            const resResp = await fetch("/runflow/upload-workflow-resources", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ host, api_key: apiKey, workflow_json: scrubbedOutput }),
            });
            if (resResp.ok) {
                ({ resources = {} } = await resResp.json());
            } else {
                console.warn(`[Runflow] /runflow/upload-workflow-resources failed (${resResp.status})`);
            }
        } catch (err) {
            console.warn("[Runflow] upload-workflow-resources request failed", err);
        }

        const body = {
            slug,
            name: endpointName,
            workflow_json: scrubbedOutput,
            graph: scrubbedGraph,
            environment: {
                packages: sysInfo.packages,
                cached_models: sysInfo.cached_models,
                comfyui: sysInfo.comfyui,
            },
            custom_nodes: sysInfo.custom_nodes,
            models,
            resources,
        };

        let existing;
        try {
            existing = await findExistingBySlug(host, apiKey, slug);
        } catch (err) {
            alert(`Runflow: lookup failed — ${err.message || err}`);
            setButtonLabel(node, "Deploy", "Deploy");
            return;
        }

        let resp;
        if (existing) {
            const { slug: _slug, ...patch } = body;
            resp = await fetch(`${host}/v1/comfyui-workflows/${existing.id}`, {
                method: "PATCH",
                headers: authHeaders(apiKey, true),
                body: JSON.stringify(patch),
            });
        } else {
            resp = await fetch(`${host}/v1/comfyui-workflows`, {
                method: "POST",
                headers: authHeaders(apiKey, true),
                body: JSON.stringify(body),
            });
        }

        if (!resp.ok) {
            const err = await formatApiError(resp);
            alert(`Runflow: ${existing ? "update" : "create"} failed — ${err}`);
            setButtonLabel(node, "Deploy", "Deploy");
            return;
        }

        await resp.json();
        state(node).deployStatus = "ready";
        setButtonLabel(node, "Deploy", "Deployed ✓");
        setTimeout(() => setButtonLabel(node, "Deploy", "Deploy"), 3000);
    } catch (err) {
        alert(`Runflow: deploy failed — ${err.message || err}`);
        setButtonLabel(node, "Deploy", "Deploy");
    }
}

// ---------- run (disabled — reimplementation pending) ----------

const _runPollers = new WeakMap();

function stopRunPolling(node) {
    const existing = _runPollers.get(node);
    if (existing) {
        clearInterval(existing.poll);
        clearInterval(existing.anim);
        _runPollers.delete(node);
    }
}

function watchRun(node, host, apiKey, runId) {
    stopRunPolling(node);
    const s = state(node);
    s.runStatus = "running";

    const frames = ["Running", "Running.", "Running..", "Running..."];
    let i = 0;
    setButtonLabel(node, "Run", frames[0]);
    const anim = setInterval(() => {
        i = (i + 1) % frames.length;
        setButtonLabel(node, "Run", frames[i]);
    }, 400);

    const poll = setInterval(async () => {
        try {
            const resp = await fetch(`${host}/v1/test-run/${runId}`, {
                headers: { "X-Api-Key": apiKey },
            });
            if (!resp.ok) return;
            const run = await resp.json();
            if (run.status === "succeeded") {
                stopRunPolling(node);
                s.runStatus = "idle";
                try {
                    await fetch("/runflow/apply-outputs", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ host, api_key: apiKey, outputs: run.outputs || {} }),
                    });
                } catch (err) {
                    console.warn("[Runflow] apply-outputs failed", err);
                }
                refreshRunButton(node);
            } else if (run.status === "failed") {
                stopRunPolling(node);
                setButtonLabel(node, "Run", "Failed ✗");
                s.runStatus = "idle";
                alert(`Runflow run failed: ${run.error || "unknown error"}`);
                setTimeout(() => refreshRunButton(node), 5000);
            }
        } catch (err) {
            console.warn("[Runflow] run poll error", err);
        }
    }, 2000);

    _runPollers.set(node, { anim, poll });
}

async function runWorkflow(node) {
    // Run uses an older test-run endpoint set that does not exist on the real
    // Runflow API. The button is hidden; this guard keeps the path inert if
    // anything calls into it. Re-enable once run creation against the real API
    // ships.
    alert("Runflow: Run is not available in this build. Use Deploy to publish workflows; runs will land in a follow-up.");
    return;

    // eslint-disable-next-line no-unreachable
    const s = state(node);
    if (s.deployStatus !== "ready") {
        alert("Runflow: click Deploy and wait for 'Ready ✓' before running.");
        return;
    }
    if (s.runStatus !== "idle") return;

    const creds = resolveCredentials(node);
    if (!creds) return;
    const { host, apiKey } = creds;

    const endpointName = node.widgets.find(w => w.name === "endpoint_name")?.value || "default";
    const graphPrompt = await app.graphToPrompt();

    const zipResp = await fetch("/runflow/build-inputs-zip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow_json: graphPrompt.output }),
    });
    if (!zipResp.ok) {
        alert(`Runflow: could not build inputs zip (${zipResp.status})`);
        return;
    }
    const { inputs_zip_b64 } = await zipResp.json();

    const submitResp = await fetch(`${host}/v1/test-run`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Api-Key": apiKey },
        body: JSON.stringify({
            endpoint_name: endpointName,
            workflow_json: graphPrompt.output,
            inputs_zip_b64,
        }),
    });
    if (!submitResp.ok) {
        alert(`Runflow: test-run submit failed (${submitResp.status}): ${await submitResp.text()}`);
        return;
    }
    const run = await submitResp.json();
    watchRun(node, host, apiKey, run.id);
}

// ---------- local playground ----------

// Capture the live workflow under `endpoint_name`'s slug, then open the
// in-process playground in a new tab. The playground page is served by the
// plugin itself (see __init__.py "Local Playground" section) and runs the
// workflow with disconnected `RunflowInput*.value` sockets replaced by
// form-provided values.
async function openLocalPlayground(node) {
    const endpointName = node.widgets.find(w => w.name === "endpoint_name")?.value || "default";
    const slug = slugify(endpointName);
    if (!slug) {
        alert(
            "Runflow: endpoint name must be URL-safe (a-z, 0-9, '-', '_') " +
            "to open the local playground."
        );
        return;
    }
    // Keep the "Local Playground" prefix on the label so the restore below can
    // still find the widget via `setButtonLabel`'s prefix lookup.
    setButtonLabel(node, "Local Playground", "Local Playground…");
    try {
        const { output, workflow } = await app.graphToPrompt();
        const resp = await fetch(`/runflow/playground/workflows/${encodeURIComponent(slug)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                workflow_json: output,
                graph: workflow,
                endpoint_name: endpointName,
            }),
        });
        if (!resp.ok) {
            let detail = `HTTP ${resp.status}`;
            try {
                const body = await resp.json();
                if (body && body.error) detail = body.error;
            } catch { /* not JSON */ }
            alert(`Runflow: could not prepare playground — ${detail}`);
            return;
        }
        window.open(`/runflow/playground/${encodeURIComponent(slug)}`, "_blank", "noopener");
    } catch (err) {
        alert(`Runflow: could not prepare playground — ${err.message || err}`);
    } finally {
        setButtonLabel(node, "Local Playground", "Local Playground");
    }
}

// Upload a local file into ComfyUI's input/ dir and return its stored name.
async function uploadInputFile(file) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    const resp = await fetch("/runflow/upload-input-file", { method: "POST", body: fd });
    if (!resp.ok) {
        const detail = await resp.text().catch(() => `HTTP ${resp.status}`);
        throw new Error(`upload failed: ${detail || resp.status}`);
    }
    return resp.json();
}

app.registerExtension({
    name: "Runflow.InputFile",
    async nodeCreated(node) {
        if (node.comfyClass !== "RunflowInputFile") return;
        const fileNameWidget = node.widgets?.find(w => w.name === "file_name");
        if (!fileNameWidget) return;

        const button = node.addWidget("button", "📁 Choose file", null, () => {
            const picker = document.createElement("input");
            picker.type = "file";
            picker.style.display = "none";
            picker.addEventListener("change", async () => {
                const picked = picker.files && picker.files[0];
                picker.remove();
                if (!picked) return;
                button.name = "Uploading…";
                node.setDirtyCanvas(true, true);
                try {
                    const uploaded = await uploadInputFile(picked);
                    fileNameWidget.value = uploaded.name;
                    button.name = "📁 Choose file";
                } catch (err) {
                    console.error("[Runflow] file upload failed", err);
                    button.name = "📁 Choose file (upload failed)";
                }
                node.setDirtyCanvas(true, true);
            });
            document.body.appendChild(picker);
            picker.click();
        });
    },
});

app.registerExtension({
    name: "Runflow.Deploy",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "RunflowDeploy") return;
        nodeType.prototype.isVirtualNode = true;
    },
    async nodeCreated(node) {
        if (node.comfyClass !== "RunflowDeploy") return;
        // The Run button is hidden until run creation against the real Runflow
        // API ships. Uncomment once runWorkflow() is wired to the live endpoint.
        // node.addWidget("button", "Run (deploy first)", null, () => runWorkflow(node));
        node.addWidget("button", "Deploy", null, () => deployWorkflow(node));
        node.addWidget("button", "Local Playground", null, () => openLocalPlayground(node));
        node.addWidget("button", "Auto setup", null, () => openAutoSetupModal());
    },
});
