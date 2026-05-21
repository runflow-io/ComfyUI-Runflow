import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ---------------------------------------------------------------------------
// Styles — mirror the visual language already used by security.js's
// port scanner so the modal blends with the Runflow settings UI.
// ---------------------------------------------------------------------------

function injectStyles() {
    if (document.getElementById("rf-autosetup-css")) return;
    const style = document.createElement("style");
    style.id = "rf-autosetup-css";
    style.textContent = `
        .rf-as-backdrop {
            position: fixed; inset: 0;
            background: rgba(0,0,0,0.55);
            z-index: 9999;
            display: flex; align-items: center; justify-content: center;
        }
        .rf-as-modal {
            background: var(--comfy-menu-bg, #202020);
            color: var(--input-text, #ddd);
            border: 1px solid var(--border-color, #444);
            border-radius: 6px;
            width: min(560px, 92vw);
            max-height: 86vh;
            display: flex; flex-direction: column;
            box-shadow: 0 12px 40px rgba(0,0,0,0.5);
            font-family: inherit;
        }
        .rf-as-modal header {
            padding: 14px 18px;
            border-bottom: 1px solid var(--border-color, #444);
            font-size: 15px; font-weight: 600;
        }
        .rf-as-body { padding: 14px 18px; overflow: auto; }
        .rf-as-body p.rf-as-info {
            margin: 0 0 12px;
            font-size: 13px; line-height: 1.55;
            color: var(--descrip-text, #aaa);
        }
        .rf-as-body label.rf-as-check {
            display: flex; align-items: center; gap: 8px;
            margin: 6px 0;
            font-size: 13px;
            cursor: pointer;
        }
        .rf-as-body label.rf-as-check input { margin: 0; }
        .rf-as-section {
            margin-top: 14px;
            padding-top: 12px;
            border-top: 1px solid var(--border-color, #333);
        }
        .rf-as-section h5 {
            margin: 0 0 6px;
            font-size: 13px; font-weight: 600;
            color: var(--input-text, #ddd);
        }
        .rf-as-section .rf-as-summary {
            font-size: 12px;
            color: var(--descrip-text, #aaa);
            margin: 0 0 6px;
            min-height: 16px;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .rf-as-progress {
            height: 8px;
            background: rgba(255,255,255,0.06);
            border-radius: 4px;
            overflow: hidden;
        }
        .rf-as-progress > div {
            height: 100%;
            background: var(--p-primary-color, #4a9eff);
            width: 0;
            transition: width 0.15s linear;
        }
        .rf-as-log {
            margin-top: 8px;
            max-height: 96px;
            overflow: auto;
            background: rgba(0,0,0,0.25);
            border: 1px solid var(--border-color, #333);
            border-radius: 4px;
            padding: 6px 8px;
            font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 11px;
            color: var(--descrip-text, #aaa);
            line-height: 1.4;
            white-space: pre-wrap;
            word-break: break-word;
        }
        .rf-as-unresolved {
            margin-top: 12px;
            padding: 8px 12px;
            background: rgba(255,165,0,0.08);
            border: 1px solid rgba(255,165,0,0.3);
            border-radius: 4px;
            font-size: 12px;
            color: var(--input-text, #ddd);
        }
        .rf-as-unresolved h5 { margin: 0 0 4px; font-size: 12px; font-weight: 600; }
        .rf-as-unresolved ul { margin: 0; padding-left: 18px; }
        .rf-as-errors {
            margin-top: 12px;
            padding: 8px 12px;
            background: rgba(255,85,85,0.10);
            border: 1px solid rgba(255,85,85,0.35);
            border-radius: 4px;
            font-size: 12px;
        }
        .rf-as-errors h5 { margin: 0 0 4px; font-size: 12px; font-weight: 600; color: #ff5555; }
        .rf-as-errors ul { margin: 0; padding-left: 18px; }
        .rf-as-footer {
            padding: 12px 18px;
            border-top: 1px solid var(--border-color, #444);
            display: flex; justify-content: flex-end; gap: 8px;
        }
        .rf-as-btn {
            padding: 7px 16px;
            border: 1px solid var(--border-color, #555);
            background: transparent;
            color: var(--input-text, #ddd);
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
        }
        .rf-as-btn:hover { background: rgba(255,255,255,0.06); }
        .rf-as-btn-primary {
            background: var(--p-primary-color, #4a9eff);
            color: #fff;
            border-color: var(--p-primary-color, #4a9eff);
        }
        .rf-as-btn-primary:hover { filter: brightness(1.1); }
        .rf-as-btn:disabled { opacity: 0.55; cursor: default; filter: none; }
        .rf-as-spinner {
            display: inline-block; width: 14px; height: 14px;
            border: 2px solid var(--border-color, #555);
            border-top-color: var(--p-primary-color, #4a9eff);
            border-radius: 50%;
            animation: rf-as-spin 0.8s linear infinite;
            vertical-align: middle; margin-right: 6px;
        }
        @keyframes rf-as-spin { to { transform: rotate(360deg); } }
        .rf-as-pill {
            display: inline-block;
            padding: 1px 8px;
            border-radius: 9px;
            font-size: 11px;
            background: rgba(255,255,255,0.07);
            color: var(--descrip-text, #aaa);
            margin-left: 6px;
        }
    `;
    document.head.appendChild(style);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtBytes(n) {
    if (!n || n < 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") node.className = v;
        else if (k === "style") Object.assign(node.style, v);
        else if (k.startsWith("on") && typeof v === "function") {
            node.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (v !== false && v != null) {
            node.setAttribute(k, v);
        }
    }
    for (const c of children) {
        if (c == null || c === false) continue;
        node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
}

// ---------------------------------------------------------------------------
// Modal lifecycle
// ---------------------------------------------------------------------------

let _eventHandler = null;

function attachEventHandler(handler) {
    detachEventHandler();
    _eventHandler = handler;
    api.addEventListener("runflow.auto_setup", handler);
}

function detachEventHandler() {
    if (_eventHandler) {
        api.removeEventListener("runflow.auto_setup", _eventHandler);
        _eventHandler = null;
    }
}

export async function openAutoSetupModal() {
    injectStyles();

    // State for the run
    const state = {
        jobId: null,
        phase: "configuring", // configuring | running | done | cancelled | error
        plan: null,
        cancelRequested: false,
        currentModelTotalBytes: 0,
        currentModelBytes: 0,
    };

    // Build DOM
    const backdrop = el("div", { class: "rf-as-backdrop" });
    const modal = el("div", { class: "rf-as-modal" });
    backdrop.appendChild(modal);

    const header = el("header", {}, "Auto setup");
    const body = el("div", { class: "rf-as-body" });
    const footer = el("div", { class: "rf-as-footer" });
    modal.append(header, body, footer);

    // Configuring view
    const infoP = el(
        "p",
        { class: "rf-as-info" },
        "This will check your local setup and attempt to automatically install all " +
        "missing custom nodes and download missing models for the active workflow."
    );
    const chkNodes = el("input", { type: "checkbox", checked: "checked" });
    const chkModels = el("input", { type: "checkbox", checked: "checked" });
    const labelNodes = el("label", { class: "rf-as-check" }, chkNodes, "Install missing custom nodes");
    const labelModels = el("label", { class: "rf-as-check" }, chkModels, "Download missing models");

    const planSummary = el("p", { class: "rf-as-info", style: { marginTop: "10px" } });

    body.append(infoP, labelNodes, labelModels, planSummary);

    // Progress sections (added once running)
    let modelsSection, modelsTitle, modelsSummary, modelsBar;
    let nodesSection, nodesTitle, nodesSummary, nodesBar, nodesLog;
    let unresolvedSection, errorsSection;

    function buildProgressSections(plan) {
        const modelsTotal = plan.missing_models.length;
        const nodesTotal = plan.missing_custom_nodes.length;

        if (chkModels.checked && modelsTotal > 0) {
            modelsTitle = el("h5", {}, "Downloading models", el("span", { class: "rf-as-pill" }, `0 of ${modelsTotal}`));
            modelsSummary = el("p", { class: "rf-as-summary" }, "Preparing…");
            const inner = el("div");
            modelsBar = el("div", { class: "rf-as-progress" }, inner);
            modelsSection = el("div", { class: "rf-as-section" }, modelsTitle, modelsSummary, modelsBar);
            body.appendChild(modelsSection);
        }

        if (chkNodes.checked && nodesTotal > 0) {
            nodesTitle = el("h5", {}, "Installing custom nodes", el("span", { class: "rf-as-pill" }, `0 of ${nodesTotal}`));
            nodesSummary = el("p", { class: "rf-as-summary" }, "Preparing…");
            const inner = el("div");
            nodesBar = el("div", { class: "rf-as-progress" }, inner);
            nodesLog = el("div", { class: "rf-as-log", style: { display: "none" } });
            nodesSection = el("div", { class: "rf-as-section" }, nodesTitle, nodesSummary, nodesBar, nodesLog);
            body.appendChild(nodesSection);
        }

        if (plan.unresolved?.length) {
            const ul = el("ul");
            for (const u of plan.unresolved) {
                if (u.kind === "custom_node") {
                    ul.appendChild(el("li", {}, `Custom node: ${u.class_type}`));
                } else if (u.kind === "model") {
                    ul.appendChild(el("li", {}, `Model: ${u.filename || u.rel_path}`));
                }
            }
            unresolvedSection = el(
                "div",
                { class: "rf-as-unresolved" },
                el("h5", {}, "Could not auto-resolve"),
                ul,
            );
            body.appendChild(unresolvedSection);
        }
    }

    function setBar(barEl, frac) {
        const clamped = Math.max(0, Math.min(1, frac));
        barEl.firstElementChild.style.width = `${(clamped * 100).toFixed(1)}%`;
    }

    function setPill(titleEl, n, total) {
        const pill = titleEl.querySelector(".rf-as-pill");
        if (pill) pill.textContent = `${n} of ${total}`;
    }

    function appendLog(line) {
        if (!nodesLog) return;
        nodesLog.style.display = "block";
        nodesLog.appendChild(document.createTextNode(line + "\n"));
        // Keep log size bounded
        while (nodesLog.childNodes.length > 200) {
            nodesLog.removeChild(nodesLog.firstChild);
        }
        nodesLog.scrollTop = nodesLog.scrollHeight;
    }

    // Footer buttons (rebuilt as phase changes)
    function renderFooter() {
        footer.innerHTML = "";
        if (state.phase === "configuring") {
            const cancel = el("button", { class: "rf-as-btn", onclick: closeModal }, "Cancel");
            const start = el(
                "button",
                { class: "rf-as-btn rf-as-btn-primary", onclick: onStart },
                "Start",
            );
            footer.append(cancel, start);
        } else if (state.phase === "running") {
            const cancel = el(
                "button",
                { class: "rf-as-btn", onclick: onCancel, disabled: state.cancelRequested },
                state.cancelRequested ? "Cancelling…" : "Cancel",
            );
            footer.appendChild(cancel);
        } else if (state.phase === "done" || state.phase === "cancelled" || state.phase === "error") {
            const close = el("button", { class: "rf-as-btn", onclick: closeModal }, "Close");
            // Only offer Restart if something was actually installed — a no-op
            // setup run shouldn't suggest restarting ComfyUI.
            const installedSomething =
                (state.plan?.missing_models?.length ?? 0) > 0 ||
                (state.plan?.missing_custom_nodes?.length ?? 0) > 0;
            if (installedSomething && state.phase !== "error") {
                const restart = el(
                    "button",
                    { class: "rf-as-btn rf-as-btn-primary", onclick: onRestart },
                    "Restart ComfyUI",
                );
                footer.append(close, restart);
            } else {
                footer.append(close);
            }
        }
    }

    function closeModal() {
        if (state.phase === "running") {
            if (!confirm("An install is in progress. Cancel it and close?")) return;
            // Best-effort cancel before closing.
            if (state.jobId) {
                fetch("/runflow/auto-setup/cancel", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ job_id: state.jobId }),
                }).catch(() => {});
            }
        }
        detachEventHandler();
        backdrop.remove();
    }

    async function onStart() {
        // Lock options
        chkNodes.disabled = true;
        chkModels.disabled = true;
        planSummary.textContent = "Planning…";

        let plan;
        try {
            const graphJson = await app.graphToPrompt();
            const planResp = await fetch("/runflow/auto-setup/plan", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ graph: graphJson.workflow }),
            });
            if (!planResp.ok) throw new Error(`plan failed (${planResp.status})`);
            plan = await planResp.json();
        } catch (err) {
            state.phase = "error";
            planSummary.innerHTML = `<span style="color:#ff5555">Plan failed: ${err.message || err}</span>`;
            renderFooter();
            return;
        }

        state.plan = plan;
        const mc = plan.missing_models.length;
        const nc = plan.missing_custom_nodes.length;
        if (mc === 0 && nc === 0 && (!plan.unresolved || plan.unresolved.length === 0)) {
            state.phase = "done";
            planSummary.textContent = "Nothing to install — your setup already has everything this workflow needs.";
            renderFooter();
            return;
        }
        planSummary.innerHTML =
            `Found <strong>${mc}</strong> missing model${mc === 1 ? "" : "s"}` +
            ` and <strong>${nc}</strong> missing custom node${nc === 1 ? "" : "s"}.`;

        buildProgressSections(plan);
        state.phase = "running";
        renderFooter();

        // Subscribe before kicking off, so we don't miss the first event.
        attachEventHandler(onEvent);

        try {
            const startResp = await fetch("/runflow/auto-setup/start", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    plan,
                    options: {
                        download_models: chkModels.checked,
                        install_custom_nodes: chkNodes.checked,
                    },
                }),
            });
            if (!startResp.ok) throw new Error(`start failed (${startResp.status})`);
            const { job_id } = await startResp.json();
            state.jobId = job_id;
        } catch (err) {
            state.phase = "error";
            planSummary.innerHTML = `<span style="color:#ff5555">Start failed: ${err.message || err}</span>`;
            detachEventHandler();
            renderFooter();
        }
    }

    async function onCancel() {
        if (!state.jobId) return;
        state.cancelRequested = true;
        renderFooter();
        try {
            await fetch("/runflow/auto-setup/cancel", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ job_id: state.jobId }),
            });
        } catch (_) { /* server-side will end the job and emit cancelled */ }
    }

    async function onRestart() {
        // Replace footer with a single spinner button.
        footer.innerHTML = "";
        const spinner = el("button", { class: "rf-as-btn", disabled: true },
            el("span", { class: "rf-as-spinner" }), "Restarting ComfyUI…");
        footer.appendChild(spinner);

        try {
            await fetch("/runflow/restart", { method: "POST" });
        } catch (_) { /* connection may drop mid-flight, that's fine */ }

        await waitForComfyUp();
        // Full reload — clears in-memory state and re-fetches every JS module.
        window.location.reload();
    }

    function onEvent(ev) {
        const data = ev.detail || ev;
        if (!data || !data.type) return;
        // Only react to events for our own job (server broadcasts to all clients).
        if (state.jobId && data.job_id && data.job_id !== state.jobId) return;

        switch (data.type) {
            case "model_start": {
                state.currentModelBytes = 0;
                state.currentModelTotalBytes = 0;
                setPill(modelsTitle, data.index, data.total);
                modelsSummary.textContent = `Downloading ${data.filename} (model ${data.index + 1} of ${data.total})`;
                setBar(modelsBar, 0);
                break;
            }
            case "model_progress": {
                state.currentModelBytes = data.bytes || 0;
                state.currentModelTotalBytes = data.total_bytes || 0;
                const frac = state.currentModelTotalBytes
                    ? state.currentModelBytes / state.currentModelTotalBytes
                    : 0;
                setBar(modelsBar, frac);
                const tail = state.currentModelTotalBytes
                    ? `${fmtBytes(state.currentModelBytes)} / ${fmtBytes(state.currentModelTotalBytes)}`
                    : fmtBytes(state.currentModelBytes);
                // Preserve the filename in the summary
                const cur = modelsSummary.textContent.split(" — ")[0];
                modelsSummary.textContent = `${cur} — ${tail}`;
                break;
            }
            case "model_done": {
                setBar(modelsBar, 1);
                setPill(modelsTitle, data.index + 1, data.total ?? (state.plan?.missing_models.length ?? 0));
                break;
            }
            case "model_error": {
                addError("model", data.index, data.error);
                break;
            }
            case "custom_node_start": {
                setPill(nodesTitle, data.index, data.total);
                nodesSummary.textContent = `Installing ${data.name} (custom node ${data.index + 1} of ${data.total})`;
                // Tick the bar to the index (count-progress, not byte-progress).
                setBar(nodesBar, data.total ? data.index / data.total : 0);
                if (nodesLog) nodesLog.innerHTML = "";
                break;
            }
            case "custom_node_log": {
                appendLog(data.line || "");
                break;
            }
            case "custom_node_done": {
                const total = state.plan?.missing_custom_nodes.length ?? 0;
                setBar(nodesBar, total ? (data.index + 1) / total : 1);
                setPill(nodesTitle, data.index + 1, total);
                break;
            }
            case "custom_node_error": {
                addError("custom_node", data.index, data.error);
                break;
            }
            case "job_done": {
                state.phase = "done";
                if (data.had_errors && Array.isArray(data.errors)) {
                    for (const e of data.errors) {
                        if (!errorsSection) buildErrorsSection();
                        errorsSection.querySelector("ul").appendChild(
                            el("li", {}, `${e.kind === "model" ? "Model" : "Custom node"} ${e.name}: ${e.error}`),
                        );
                    }
                }
                renderFooter();
                detachEventHandler();
                break;
            }
            case "job_cancelled": {
                state.phase = "cancelled";
                planSummary.innerHTML += " <span style=\"color:#ffb86b\">— cancelled</span>";
                renderFooter();
                detachEventHandler();
                break;
            }
        }
    }

    function addError(kind, index, error) {
        if (!errorsSection) buildErrorsSection();
        let name = "?";
        if (kind === "model") {
            name = state.plan?.missing_models[index]?.filename ?? `model #${index}`;
        } else if (kind === "custom_node") {
            name = state.plan?.missing_custom_nodes[index]?.name ?? `node #${index}`;
        }
        errorsSection.querySelector("ul").appendChild(
            el("li", {}, `${kind === "model" ? "Model" : "Custom node"} ${name}: ${error}`),
        );
    }

    function buildErrorsSection() {
        errorsSection = el(
            "div",
            { class: "rf-as-errors" },
            el("h5", {}, "Errors"),
            el("ul"),
        );
        body.appendChild(errorsSection);
    }

    backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) closeModal();
    });
    document.addEventListener("keydown", function onKey(e) {
        if (e.key === "Escape" && document.body.contains(backdrop)) {
            closeModal();
            document.removeEventListener("keydown", onKey);
        }
    });

    renderFooter();
    document.body.appendChild(backdrop);
}

// ---------------------------------------------------------------------------
// Poll /system_stats until ComfyUI is back up after a restart.
// ---------------------------------------------------------------------------

async function waitForComfyUp() {
    // Brief delay to let the current process tear down before we start probing.
    await new Promise(r => setTimeout(r, 1500));
    const deadline = Date.now() + 5 * 60 * 1000; // 5 minutes
    while (Date.now() < deadline) {
        try {
            const resp = await fetch("/system_stats", { cache: "no-store" });
            if (resp.ok) return true;
        } catch (_) { /* connection refused while restarting */ }
        await new Promise(r => setTimeout(r, 1000));
    }
    return false;
}
