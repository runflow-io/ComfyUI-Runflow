import { app } from "../../scripts/app.js";

app.registerExtension({
    name: "Runflow.Settings",
    settings: [
        {
            id: "Runflow.ApiUrl",
            name: "API URL",
            category: ["Runflow", "Connection", "ApiUrl"],
            tooltip: "Runflow API base URL used when the deploy node's host field is empty.",
            type: "text",
            defaultValue: "https://api.runflow.io",
        },
        {
            id: "Runflow.ApiKey",
            name: "API Key",
            category: ["Runflow", "Connection", "ApiKey"],
            tooltip:
                "Runflow API key (rf_live_*) used when the deploy node's api_key field is empty. " +
                "Required scopes: comfyui-workflows:read, comfyui-workflows:create, comfyui-workflows:edit.",
            type: "text",
            defaultValue: "",
        },
    ],
});
