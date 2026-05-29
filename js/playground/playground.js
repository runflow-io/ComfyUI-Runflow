// Runflow Local Playground frontend.
//
// Reads the bootstrap JSON injected by the GET /runflow/playground/<slug>
// handler, renders a typed form for the workflow's RunflowInput nodes, and
// runs the workflow against the local ComfyUI by polling the plugin's run
// endpoint. Output preview supports image / video / audio / 3D (glb/gltf
// via the lazily-loaded model-viewer element); other file types fall back
// to a download link.
//
// No build step — plain ES module, matches the rest of js/runflow.js.

const POLL_INTERVAL_MS = 1000;
const MODEL_VIEWER_SRC = "https://unpkg.com/@google/model-viewer@4/dist/model-viewer.min.js";

const bootstrap = (() => {
    const tag = document.getElementById("rf-playground-bootstrap");
    try {
        return JSON.parse((tag && tag.textContent) || "{}");
    } catch (err) {
        console.error("Runflow Playground: bootstrap JSON parse failed", err);
        return {};
    }
})();

const state = {
    values: Object.create(null),
    pendingUploads: 0,
    running: false,
    pollTimer: null,
    modelViewerLoaded: false,
};

const $form = document.getElementById("input-form");
const $runBtn = document.getElementById("run-btn");
const $resetBtn = document.getElementById("reset-btn");
const $formError = document.getElementById("form-error");
const $output = document.getElementById("output-area");
const $endpointPill = document.getElementById("endpoint-pill");
const $capturedAt = document.getElementById("captured-at");
const $inputCount = document.getElementById("input-count");
const $outputCount = document.getElementById("output-count");

// ── Header / counters ─────────────────────────────────────────────────────

function renderHeader() {
    const name = bootstrap.endpoint_name || bootstrap.slug || "playground";
    document.title = `${name} — Runflow Playground`;
    $endpointPill.textContent = name;
    if (bootstrap.captured_at) {
        const d = new Date(bootstrap.captured_at * 1000);
        $capturedAt.textContent = `captured ${formatRelative(d)}`;
    }
    $inputCount.textContent = `${(bootstrap.inputs || []).length} field${(bootstrap.inputs || []).length === 1 ? "" : "s"}`;
    $outputCount.textContent = `${(bootstrap.outputs || []).length} output${(bootstrap.outputs || []).length === 1 ? "" : "s"}`;
}

function formatRelative(date) {
    const seconds = Math.max(1, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    return date.toLocaleString();
}

// ── Form ──────────────────────────────────────────────────────────────────

function renderForm() {
    $form.innerHTML = "";
    const inputs = bootstrap.inputs || [];
    if (inputs.length === 0) {
        const empty = document.createElement("p");
        empty.className = "field-description";
        empty.textContent = "No Runflow Input nodes in this workflow. Add some, then reopen.";
        $form.appendChild(empty);
        return;
    }
    for (const input of inputs) {
        $form.appendChild(buildField(input));
        if (input.default_value !== undefined && input.default_value !== null) {
            state.values[input.input_id] = input.default_value;
        }
    }
}

function buildField(input) {
    const wrap = document.createElement("div");
    wrap.className = "field";

    const label = document.createElement("label");
    label.className = "field-label";
    label.setAttribute("for", controlId(input.input_id));
    label.textContent = input.display_name;
    const typeTag = document.createElement("span");
    typeTag.className = "field-type";
    typeTag.textContent = input.type.toLowerCase();
    label.appendChild(typeTag);
    wrap.appendChild(label);

    if (input.description) {
        const desc = document.createElement("p");
        desc.className = "field-description";
        desc.textContent = input.description;
        wrap.appendChild(desc);
    }

    wrap.appendChild(buildControl(input));
    return wrap;
}

function controlId(inputId) {
    return `rf-field-${inputId}`;
}

function buildControl(input) {
    switch (input.type) {
        case "STRING":  return buildStringControl(input);
        case "INT":     return buildNumberControl(input, /* step */ "1");
        case "FLOAT":   return buildNumberControl(input, /* step */ "any");
        case "BOOLEAN": return buildBooleanControl(input);
        case "IMAGE":   return buildImageControl(input);
        case "FILE":    return buildFileControl(input);
        default: {
            const note = document.createElement("p");
            note.className = "field-description";
            note.textContent = `(${input.type} is not supported by this playground yet.)`;
            return note;
        }
    }
}

function buildStringControl(input) {
    // Textareas for likely-long fields; single-line input otherwise. The
    // heuristic mirrors how the cloud playground decides — heuristic, not
    // contract, so it's OK to be approximate.
    const looksLong = /prompt|description|text/i.test(input.input_id) || /prompt|description|text/i.test(input.display_name);
    const el = document.createElement(looksLong ? "textarea" : "input");
    if (!looksLong) el.type = "text";
    el.className = "field-control";
    el.id = controlId(input.input_id);
    if (input.default_value != null) {
        el.value = String(input.default_value);
        state.values[input.input_id] = String(input.default_value);
    }
    el.placeholder = input.description || "";
    el.addEventListener("input", () => {
        state.values[input.input_id] = el.value;
    });
    return el;
}

function buildNumberControl(input, step) {
    const el = document.createElement("input");
    el.type = "number";
    el.step = step;
    el.className = "field-control";
    el.id = controlId(input.input_id);
    if (input.default_value != null && input.default_value !== "") {
        el.value = String(input.default_value);
        state.values[input.input_id] = Number(input.default_value);
    }
    el.addEventListener("input", () => {
        state.values[input.input_id] = el.value === "" ? undefined : Number(el.value);
    });
    return el;
}

function buildBooleanControl(input) {
    const wrap = document.createElement("label");
    wrap.className = "toggle";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.id = controlId(input.input_id);
    if (input.default_value) {
        checkbox.checked = true;
        state.values[input.input_id] = true;
    }
    const track = document.createElement("span");
    track.className = "toggle-track";
    const thumb = document.createElement("span");
    thumb.className = "toggle-thumb";
    track.appendChild(thumb);
    const text = document.createElement("span");
    text.className = "toggle-label";
    text.textContent = checkbox.checked ? "On" : "Off";
    checkbox.addEventListener("change", () => {
        state.values[input.input_id] = checkbox.checked;
        text.textContent = checkbox.checked ? "On" : "Off";
    });
    wrap.appendChild(checkbox);
    wrap.appendChild(track);
    wrap.appendChild(text);
    return wrap;
}

function buildImageControl(input) {
    const wrap = document.createElement("div");
    wrap.className = "file-row";
    const id = controlId(input.input_id);
    const file = document.createElement("input");
    file.type = "file";
    file.id = id;
    file.accept = "image/*";
    const trigger = document.createElement("label");
    trigger.className = "file-trigger";
    trigger.setAttribute("for", id);
    trigger.textContent = "Choose image";
    const nameEl = document.createElement("span");
    nameEl.className = "file-name";
    nameEl.textContent = "No file selected";
    const progress = document.createElement("span");
    progress.className = "file-progress";

    file.addEventListener("change", async () => {
        const picked = file.files && file.files[0];
        if (!picked) return;
        nameEl.textContent = picked.name;
        progress.textContent = "Uploading…";
        state.pendingUploads += 1;
        refreshRunButton();
        try {
            const uploaded = await uploadImage(picked);
            // ComfyUI returns {name, subfolder, type}. LoadImage accepts either
            // the bare name when subfolder is empty (the common case) or
            // "subfolder/name" otherwise.
            const wireName = uploaded.subfolder ? `${uploaded.subfolder}/${uploaded.name}` : uploaded.name;
            state.values[input.input_id] = wireName;
            progress.textContent = "✓";
        } catch (err) {
            console.error(err);
            progress.textContent = "upload failed";
            state.values[input.input_id] = undefined;
        } finally {
            state.pendingUploads -= 1;
            refreshRunButton();
        }
    });

    wrap.appendChild(file);
    wrap.appendChild(trigger);
    wrap.appendChild(nameEl);
    wrap.appendChild(progress);
    return wrap;
}

async function uploadImage(file) {
    const fd = new FormData();
    fd.append("image", file, file.name);
    fd.append("type", "input");
    const resp = await fetch("/upload/image", { method: "POST", body: fd });
    if (!resp.ok) {
        const detail = await resp.text().catch(() => `HTTP ${resp.status}`);
        throw new Error(`upload failed: ${detail || resp.status}`);
    }
    return resp.json();
}

function buildFileControl(input) {
    // Like the image control, but accepts any file type and uploads through
    // the Runflow generic endpoint (writes to ComfyUI/input/). The workflow
    // receives the uploaded filename as a plain string.
    const wrap = document.createElement("div");
    wrap.className = "file-row";
    const id = controlId(input.input_id);
    const file = document.createElement("input");
    file.type = "file";
    file.id = id;
    const trigger = document.createElement("label");
    trigger.className = "file-trigger";
    trigger.setAttribute("for", id);
    trigger.textContent = "Choose file";
    const nameEl = document.createElement("span");
    nameEl.className = "file-name";
    const progress = document.createElement("span");
    progress.className = "file-progress";

    // Pre-fill from the node's file_name widget default, if any.
    if (input.default_value) {
        nameEl.textContent = String(input.default_value);
        state.values[input.input_id] = String(input.default_value);
    } else {
        nameEl.textContent = "No file selected";
    }

    file.addEventListener("change", async () => {
        const picked = file.files && file.files[0];
        if (!picked) return;
        nameEl.textContent = picked.name;
        progress.textContent = "Uploading…";
        state.pendingUploads += 1;
        refreshRunButton();
        try {
            const uploaded = await uploadFile(picked);
            state.values[input.input_id] = uploaded.name;
            progress.textContent = "✓";
        } catch (err) {
            console.error(err);
            progress.textContent = "upload failed";
            state.values[input.input_id] = undefined;
        } finally {
            state.pendingUploads -= 1;
            refreshRunButton();
        }
    });

    wrap.appendChild(file);
    wrap.appendChild(trigger);
    wrap.appendChild(nameEl);
    wrap.appendChild(progress);
    return wrap;
}

async function uploadFile(file) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    const resp = await fetch("/runflow/upload-input-file", { method: "POST", body: fd });
    if (!resp.ok) {
        const detail = await resp.text().catch(() => `HTTP ${resp.status}`);
        throw new Error(`upload failed: ${detail || resp.status}`);
    }
    return resp.json();
}

// ── Run lifecycle ─────────────────────────────────────────────────────────

function refreshRunButton() {
    const busy = state.pendingUploads > 0 || state.running;
    $runBtn.disabled = busy;
    if (state.pendingUploads > 0) {
        $runBtn.textContent = "Uploading…";
    } else if (state.running) {
        $runBtn.textContent = "Running…";
    } else {
        $runBtn.textContent = "Run";
    }
}

function setError(message) {
    if (!message) {
        $formError.hidden = true;
        $formError.textContent = "";
        return;
    }
    $formError.hidden = false;
    $formError.textContent = message;
}

async function onRun() {
    setError("");
    if (state.pendingUploads > 0) {
        setError("Wait for uploads to finish before running.");
        return;
    }
    const missing = (bootstrap.inputs || [])
        .filter((i) => state.values[i.input_id] === undefined || state.values[i.input_id] === "")
        .map((i) => i.display_name);
    if (missing.length > 0) {
        setError(`Please fill in: ${missing.join(", ")}.`);
        return;
    }
    state.running = true;
    refreshRunButton();
    renderRunning();
    try {
        const resp = await fetch(`/runflow/playground/${encodeURIComponent(bootstrap.slug)}/run`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ values: state.values }),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            renderError(body.error || `Run failed to start (HTTP ${resp.status})`);
            return;
        }
        pollRun(body.run_id);
    } catch (err) {
        renderError(err.message || String(err));
    }
}

function pollRun(runId) {
    if (state.pollTimer) clearInterval(state.pollTimer);
    const url = `/runflow/playground/${encodeURIComponent(bootstrap.slug)}/runs/${encodeURIComponent(runId)}`;
    let inFlight = false;
    state.pollTimer = setInterval(async () => {
        if (inFlight) return;
        inFlight = true;
        try {
            const resp = await fetch(url);
            if (!resp.ok) return;
            const record = await resp.json();
            if (record.status === "running") return;
            clearInterval(state.pollTimer);
            state.pollTimer = null;
            if (record.status === "succeeded") {
                renderOutputs(record.outputs || {});
            } else {
                renderError(record.error || "run failed");
            }
        } catch (err) {
            console.warn("Runflow playground poll error", err);
        } finally {
            inFlight = false;
        }
    }, POLL_INTERVAL_MS);
}

function onReset() {
    if (state.pollTimer) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
    }
    state.values = Object.create(null);
    state.running = false;
    setError("");
    renderForm();
    renderIdle();
    refreshRunButton();
}

// ── Output rendering ──────────────────────────────────────────────────────

function clearOutput() {
    $output.innerHTML = "";
}

function renderIdle() {
    clearOutput();
    const wrap = document.createElement("div");
    wrap.className = "output-idle";
    const icon = document.createElement("div");
    icon.className = "output-idle-icon";
    const text = document.createElement("p");
    text.textContent = "No output yet. Fill the form and click Run.";
    wrap.append(icon, text);
    $output.appendChild(wrap);
}

function renderRunning() {
    clearOutput();
    const wrap = document.createElement("div");
    wrap.className = "output-running";
    const spinner = document.createElement("div");
    spinner.className = "spinner";
    spinner.setAttribute("aria-hidden", "true");
    const text = document.createElement("p");
    text.className = "output-status-text";
    text.textContent = "Running workflow";
    wrap.append(spinner, text);
    $output.appendChild(wrap);
}

function renderError(message) {
    state.running = false;
    refreshRunButton();
    clearOutput();
    const wrap = document.createElement("div");
    wrap.className = "output-error";
    const head = document.createElement("strong");
    head.textContent = "Run failed";
    const body = document.createElement("span");
    body.textContent = message;
    wrap.append(head, body);
    $output.appendChild(wrap);
}

function renderOutputs(outputs) {
    state.running = false;
    refreshRunButton();
    clearOutput();
    const schema = bootstrap.outputs || [];
    const ids = schema.length > 0 ? schema.map((o) => o.output_id) : Object.keys(outputs);
    if (ids.length === 0) {
        renderIdle();
        return;
    }
    let renderedSomething = false;
    for (const outputId of ids) {
        const entries = outputs[outputId] || [];
        const meta = schema.find((o) => o.output_id === outputId);
        const group = buildOutputGroup(outputId, meta, entries);
        $output.appendChild(group);
        if (entries.length > 0) renderedSomething = true;
    }
    if (!renderedSomething) {
        const note = document.createElement("p");
        note.className = "output-empty";
        note.textContent = "Workflow finished but produced no files.";
        $output.appendChild(note);
    }
    // Lazy-load model-viewer if any 3D entries appeared.
    const hasThreeD = Object.values(outputs).some((arr) => Array.isArray(arr) && arr.some((e) => e.kind === "3d"));
    if (hasThreeD) ensureModelViewer();
}

function buildOutputGroup(outputId, meta, entries) {
    const group = document.createElement("div");
    group.className = "output-group";
    const head = document.createElement("div");
    head.className = "output-group-header";
    const title = document.createElement("span");
    title.className = "output-group-title";
    title.textContent = (meta && meta.output_name) || outputId;
    const count = document.createElement("span");
    count.className = "output-group-count";
    count.textContent = `${entries.length} item${entries.length === 1 ? "" : "s"}`;
    head.append(title, count);
    group.appendChild(head);

    if (entries.length === 0) {
        const empty = document.createElement("p");
        empty.className = "output-empty";
        empty.textContent = "No file produced for this output.";
        group.appendChild(empty);
        return group;
    }
    const items = document.createElement("div");
    items.className = "output-items";
    for (const entry of entries) items.appendChild(buildOutputItem(entry));
    group.appendChild(items);
    return group;
}

function buildOutputItem(entry) {
    const item = document.createElement("div");
    item.className = "output-item";

    let preview;
    switch (entry.kind) {
        case "image":
            preview = document.createElement("img");
            preview.src = entry.url;
            preview.alt = entry.filename;
            break;
        case "video":
            preview = document.createElement("video");
            preview.controls = true;
            preview.src = entry.url;
            break;
        case "audio":
            preview = document.createElement("audio");
            preview.controls = true;
            preview.src = entry.url;
            break;
        case "3d": {
            const ext = (entry.filename.split(".").pop() || "").toLowerCase();
            if (ext === "glb" || ext === "gltf") {
                preview = document.createElement("model-viewer");
                preview.setAttribute("src", entry.url);
                preview.setAttribute("camera-controls", "");
                preview.setAttribute("auto-rotate", "");
                preview.setAttribute("ar", "");
            } else {
                preview = document.createElement("p");
                preview.className = "field-description";
                preview.textContent = "3D preview only supports .glb / .gltf in-browser. Download the file to view it locally.";
            }
            break;
        }
        default:
            preview = document.createElement("p");
            preview.className = "field-description";
            preview.textContent = "Binary file — preview unavailable.";
    }
    item.appendChild(preview);

    const meta = document.createElement("div");
    meta.className = "output-item-meta";
    const name = document.createElement("span");
    name.className = "output-item-name";
    name.textContent = entry.filename;
    const link = document.createElement("a");
    link.className = "output-item-download";
    link.href = entry.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.download = entry.filename;
    link.textContent = "Download";
    meta.append(name, link);
    item.appendChild(meta);
    return item;
}

function ensureModelViewer() {
    if (state.modelViewerLoaded || customElements.get("model-viewer")) {
        state.modelViewerLoaded = true;
        return;
    }
    state.modelViewerLoaded = true;
    const tag = document.createElement("script");
    tag.type = "module";
    tag.src = MODEL_VIEWER_SRC;
    tag.onerror = () => console.warn("Runflow Playground: model-viewer failed to load (offline?)");
    document.head.appendChild(tag);
}

// ── Bootstrap ─────────────────────────────────────────────────────────────

renderHeader();
renderForm();
renderIdle();
refreshRunButton();
$runBtn.addEventListener("click", onRun);
$resetBtn.addEventListener("click", onReset);
