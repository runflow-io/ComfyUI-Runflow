import { app } from "../../scripts/app.js";

// ---------------------------------------------------------------------------
// Setting IDs
// ---------------------------------------------------------------------------

const ID_ENABLED  = "Runflow.Auth.Enabled";
const ID_USERNAME = "Runflow.Auth.Username";
const ID_PASSWORD = "Runflow.Auth.Password";

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
// Port Scanner — injected into the settings panel via DOM manipulation
// ---------------------------------------------------------------------------

function injectStyles() {
    if (document.getElementById("rf-scan-css")) return;
    const style = document.createElement("style");
    style.id = "rf-scan-css";
    style.textContent = `
        #rf-port-scan {
            margin-top: 20px;
            padding: 16px;
            border-top: 1px solid var(--border-color, #444);
        }
        #rf-port-scan h4 {
            margin: 0 0 6px;
            font-size: 14px;
            color: var(--input-text, #ddd);
        }
        #rf-port-scan .rf-desc {
            margin: 0 0 12px;
            font-size: 13px;
            color: var(--descrip-text, #aaa);
        }
        #rf-scan-btn {
            padding: 8px 20px;
            background: var(--p-primary-color, #4a9eff);
            color: #fff;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
        }
        #rf-scan-btn:disabled {
            opacity: 0.6;
            cursor: default;
        }
        #rf-scan-status {
            margin-top: 14px;
            font-size: 13px;
            color: var(--input-text, #ddd);
            line-height: 1.6;
        }
        #rf-scan-status table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 8px;
            font-size: 13px;
        }
        #rf-scan-status th {
            text-align: left;
            padding: 6px 10px;
            border-bottom: 1px solid var(--border-color, #444);
            color: var(--descrip-text, #aaa);
            font-weight: 500;
        }
        #rf-scan-status td {
            padding: 6px 10px;
            border-bottom: 1px solid var(--border-color, #333);
        }
        .rf-port-open   { color: #ff5555; font-weight: 600; }
        .rf-port-closed { color: #50c878; font-weight: 600; }
        .rf-scan-spinner {
            display: inline-block;
            width: 14px;
            height: 14px;
            border: 2px solid var(--border-color, #555);
            border-top-color: var(--p-primary-color, #4a9eff);
            border-radius: 50%;
            animation: rf-spin 0.8s linear infinite;
            vertical-align: middle;
            margin-right: 8px;
        }
        @keyframes rf-spin { to { transform: rotate(360deg); } }
        .rf-scan-summary {
            margin-top: 10px;
            padding: 8px 12px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 500;
        }
        .rf-scan-summary.warn  { background: rgba(255,85,85,0.12); color: #ff5555; }
        .rf-scan-summary.ok    { background: rgba(80,200,120,0.12); color: #50c878; }
    `;
    document.head.appendChild(style);
}

function createScanUI() {
    injectStyles();

    const div = document.createElement("div");
    div.id = "rf-port-scan";
    div.innerHTML = `
        <h4>Port Scanner</h4>
        <p class="rf-desc">
            Check if common ports on this machine are reachable from the public internet.
        </p>
        <button id="rf-scan-btn">Security Scan</button>
        <div id="rf-scan-status" style="display:none"></div>
    `;
    div.querySelector("#rf-scan-btn").addEventListener("click", runPortScan);
    return div;
}

async function runPortScan() {
    const btn    = document.getElementById("rf-scan-btn");
    const status = document.getElementById("rf-scan-status");
    if (!btn || !status) return;

    btn.disabled = true;
    btn.textContent = "Scanning\u2026";
    status.style.display = "block";
    status.innerHTML =
        '<span class="rf-scan-spinner"></span> Detecting public IP address\u2026';

    try {
        // Step 1 — get server's public IP
        const ipResp = await fetch("/runflow/public-ip");
        if (!ipResp.ok) throw new Error("Failed to detect public IP");
        const { ip } = await ipResp.json();

        status.innerHTML =
            `<span class="rf-scan-spinner"></span> Public IP: <strong>${ip}</strong> — checking ports\u2026`;

        // Step 2 — scan ports from the internet via portchecker.io
        const scanResp = await fetch("/runflow/port-scan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ip }),
        });
        if (!scanResp.ok) throw new Error("Port scan request failed");
        const data = await scanResp.json();

        // Step 3 — render results
        let html = `<p style="margin:0 0 4px">Public IP: <strong>${data.ip}</strong></p>`;
        html += "<table><tr><th>Port</th><th>Status</th></tr>";
        let anyOpen = false;
        for (const p of data.ports) {
            const open = p.status;
            if (open) anyOpen = true;
            const cls   = open ? "rf-port-open" : "rf-port-closed";
            const label = open ? "OPEN" : "CLOSED";
            html += `<tr><td>${p.port}</td><td class="${cls}">${label}</td></tr>`;
        }
        html += "</table>";

        if (anyOpen) {
            html += '<div class="rf-scan-summary warn">Open ports detected — consider enabling authentication or restricting access.</div>';
        } else {
            html += '<div class="rf-scan-summary ok">No open ports detected from the internet.</div>';
        }
        status.innerHTML = html;
    } catch (e) {
        status.innerHTML = `<span style="color:#ff5555">Error: ${e.message}</span>`;
    }

    btn.disabled = false;
    btn.textContent = "Security Scan";
}

// Inject the port scanner section next to our authentication settings.
// We locate our password input (type="password") and walk up to the section
// container, then append the scanner at the bottom.

function injectPortScanner() {
    if (document.getElementById("rf-port-scan")) return true;

    const pwInput = document.querySelector('input[type="password"]');
    if (!pwInput) return false;

    // Walk up until we find a container holding all our settings
    let container = pwInput;
    for (let i = 0; i < 15; i++) {
        if (!container.parentElement) return false;
        container = container.parentElement;
        const controls = container.querySelectorAll(
            'input, [role="switch"], [role="checkbox"]'
        );
        if (controls.length >= 3) break;
    }

    container.appendChild(createScanUI());
    return true;
}

// ---------------------------------------------------------------------------
// Register settings under Settings → Runflow / Authentication
// (reverse order — settings are prepended so last-registered appears first)
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
            onChange: (newVal, oldVal) => { debouncedSync(); },
        },
        {
            id: ID_USERNAME,
            name: "Username",
            type: "text",
            defaultValue: "",
            category: ["Runflow", "Authentication", "Username"],
            onChange: (newVal, oldVal) => { debouncedSync(); },
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
            onChange: (newVal, oldVal) => { debouncedSync(); },
        },
    ],

    async setup() {
        // Watch for the settings panel to appear and inject the port scanner
        if (injectPortScanner()) return;
        const observer = new MutationObserver(() => {
            if (injectPortScanner()) observer.disconnect();
        });
        observer.observe(document.body, { childList: true, subtree: true });
        setTimeout(() => observer.disconnect(), 60_000);
    },
});
