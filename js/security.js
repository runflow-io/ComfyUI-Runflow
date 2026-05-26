import { app } from "../../scripts/app.js";

// ---------------------------------------------------------------------------
// Setting IDs
// ---------------------------------------------------------------------------

const ID_ENABLED  = "Runflow.Auth.Enabled";
const ID_USERNAME = "Runflow.Auth.Username";
const ID_PASSWORD = "Runflow.Auth.Password";
const ID_SCAN     = "Runflow.Security.PortScan";

// ---------------------------------------------------------------------------
// Sync ComfyUI settings → backend security file on every change
// ---------------------------------------------------------------------------

let syncTimer = null;

async function syncSecuritySettings() {
    try {
        const enabled  = app.extensionManager.setting.get(ID_ENABLED);
        const username = app.extensionManager.setting.get(ID_USERNAME);
        const password = app.extensionManager.setting.get(ID_PASSWORD);

        await fetch("/runflow/security", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                enabled:  Boolean(enabled),
                username: username || "",
                password: password || "",
            }),
        });
    } catch (_) { /* best-effort */ }
}

function debouncedSync() {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(syncSecuritySettings, 500);
}

// ---------------------------------------------------------------------------
// Small DOM helper (mirrors js/auto_setup.js el())
// ---------------------------------------------------------------------------

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
// Port-exposure scanner UI (mounted as a custom-rendered setting)
// ---------------------------------------------------------------------------

function injectStyles() {
    if (document.getElementById("rf-scan-css")) return;
    const style = document.createElement("style");
    style.id = "rf-scan-css";
    style.textContent = `
        .rf-port-scan { width: 100%; font-size: 13px; color: var(--input-text, #ddd); }
        .rf-port-scan .rf-desc {
            margin: 0 0 12px; font-size: 13px; line-height: 1.5;
            color: var(--descrip-text, #aaa);
        }
        .rf-scan-btn {
            padding: 8px 20px; background: var(--p-primary-color, #4a9eff);
            color: #fff; border: none; border-radius: 4px; cursor: pointer;
            font-size: 13px; font-weight: 500;
        }
        .rf-scan-btn:disabled { opacity: 0.6; cursor: default; }
        .rf-scan-status { margin-top: 14px; line-height: 1.6; }
        .rf-scan-status p { margin: 0 0 4px; }
        .rf-scan-status table {
            width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px;
        }
        .rf-scan-status th {
            text-align: left; padding: 6px 10px;
            border-bottom: 1px solid var(--border-color, #444);
            color: var(--descrip-text, #aaa); font-weight: 500;
        }
        .rf-scan-status td {
            padding: 6px 10px; border-bottom: 1px solid var(--border-color, #333);
        }
        .rf-port-open   { color: #ff5555; font-weight: 600; }
        .rf-port-closed { color: #50c878; font-weight: 600; }
        .rf-bind-danger { color: #ff5555; font-weight: 600; }
        .rf-bind-ok     { color: #50c878; font-weight: 600; }
        .rf-scan-spinner {
            display: inline-block; width: 14px; height: 14px;
            border: 2px solid var(--border-color, #555);
            border-top-color: var(--p-primary-color, #4a9eff);
            border-radius: 50%; animation: rf-spin 0.8s linear infinite;
            vertical-align: middle; margin-right: 8px;
        }
        @keyframes rf-spin { to { transform: rotate(360deg); } }
        .rf-scan-summary {
            margin-top: 10px; padding: 8px 12px; border-radius: 4px;
            font-size: 12px; font-weight: 500; line-height: 1.5;
        }
        .rf-scan-summary.warn { background: rgba(255,85,85,0.12); color: #ff5555; }
        .rf-scan-summary.ok   { background: rgba(80,200,120,0.12); color: #50c878; }
        .rf-scan-summary.info { background: rgba(74,158,255,0.12); color: var(--input-text, #ddd); }
    `;
    document.head.appendChild(style);
}

function buildScanUI() {
    injectStyles();

    const status = el("div", { class: "rf-scan-status", style: { display: "none" } });
    const btn = el("button", { class: "rf-scan-btn" }, "Run security scan");
    btn.addEventListener("click", () => runPortScan(btn, status));

    return el("div", { class: "rf-port-scan" },
        el("p", { class: "rf-desc" },
            "ComfyUI has no authentication by default, and custom nodes can run " +
            "arbitrary code — so a ComfyUI port reachable from the public internet " +
            "is a serious risk. This asks a public service whether this machine's " +
            "ports are reachable from outside your network."),
        btn,
        status,
    );
}

async function runPortScan(btn, status) {
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Scanning…";
    status.style.display = "block";
    status.replaceChildren(
        el("span", {},
            el("span", { class: "rf-scan-spinner" }),
            "Contacting port-check service…"),
    );

    try {
        const resp = await fetch("/runflow/port-scan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
        });
        const data = await resp.json();
        status.replaceChildren(renderScanResult(data));
    } catch (e) {
        status.replaceChildren(
            el("div", { class: "rf-scan-summary warn" },
                `Scan failed: ${e && e.message ? e.message : e}`),
        );
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

function renderScanResult(data) {
    const wrap = el("div");

    // Listen binding — known locally, always reported.
    const bindCls = data.bound_all_interfaces ? "rf-bind-danger" : "rf-bind-ok";
    const bindText = data.bound_all_interfaces
        ? `all interfaces (${data.listen}) — reachable from the network`
        : `${data.listen} — local only`;
    wrap.appendChild(
        el("p", {}, "Listening on port ",
            el("strong", {}, String(data.port)), ", bound to ",
            el("span", { class: bindCls }, bindText)),
    );

    if (data.ip) {
        wrap.appendChild(el("p", {}, "Public IP: ", el("strong", {}, data.ip)));
    }

    // Per-port reachability table (present only when the external scan ran).
    let anyOpen = false;
    if (Array.isArray(data.ports) && data.ports.length) {
        const rows = [el("tr", {}, el("th", {}, "Port"), el("th", {}, "Reachable from internet"))];
        for (const p of data.ports) {
            const open = Boolean(p.status);
            if (open) anyOpen = true;
            rows.push(el("tr", {},
                el("td", {}, String(p.port)),
                el("td", { class: open ? "rf-port-open" : "rf-port-closed" },
                    open ? "OPEN" : "closed")));
        }
        wrap.appendChild(el("table", {}, ...rows));
    }

    // Summary banner.
    if (data.error) {
        const cls = data.bound_all_interfaces ? "warn" : "info";
        let msg = data.error;
        if (data.bound_all_interfaces) {
            msg += " ComfyUI is bound to all network interfaces, so it may be " +
                "exposed — enable authentication below or restrict access with a firewall.";
        }
        wrap.appendChild(el("div", { class: `rf-scan-summary ${cls}` }, msg));
    } else if (anyOpen) {
        wrap.appendChild(el("div", { class: "rf-scan-summary warn" },
            "Open ports are reachable from the public internet. Enable password " +
            "authentication below, restrict access with a firewall, or bind ComfyUI " +
            "to 127.0.0.1 (remove --listen)."));
    } else {
        wrap.appendChild(el("div", { class: "rf-scan-summary ok" },
            "None of the scanned ports are reachable from the public internet."));
    }

    return wrap;
}

// ---------------------------------------------------------------------------
// Register settings under Settings → Runflow
// (auth controls + the custom-rendered port-exposure scanner)
// ---------------------------------------------------------------------------

app.registerExtension({
    name: "Runflow.Security",

    settings: [
        {
            id: ID_PASSWORD,
            name: "Password",
            type: "text",
            defaultValue: "",
            category: ["Runflow", "Authentication", "Password"],
            attrs: { type: "password" },
            onChange: () => { debouncedSync(); },
        },
        {
            id: ID_USERNAME,
            name: "Username",
            type: "text",
            defaultValue: "",
            category: ["Runflow", "Authentication", "Username"],
            onChange: () => { debouncedSync(); },
        },
        {
            id: ID_ENABLED,
            name: "Enable password authentication",
            type: "boolean",
            defaultValue: false,
            category: ["Runflow", "Authentication", "Enable password authentication"],
            tooltip:
                "Require HTTP Basic authentication for all requests. " +
                "Both username and password must be set for this to take effect.",
            onChange: () => { debouncedSync(); },
        },
        {
            // Custom-rendered "setting": ComfyUI calls the render function and
            // mounts the returned element directly in the settings panel — the
            // supported replacement for the old, brittle DOM-injection.
            id: ID_SCAN,
            name: "Port exposure scan",
            type: () => buildScanUI(),
            defaultValue: "",
            category: ["Runflow", "Authentication", "Port exposure scan"],
            tooltip:
                "Check whether this machine's ports are reachable from the " +
                "public internet via a third-party port-check service.",
        },
    ],
});
