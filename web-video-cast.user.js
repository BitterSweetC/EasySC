// ==UserScript==
// @name         Web Video Cast to TV
// @namespace    local.screen.casting
// @version      0.5.6
// @description  Detect web video sources and send them to a local bridge for DLNA casting
// @match        *://*/*
// @grant        GM_addStyle
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @noframes
// @run-at       document-start
// ==/UserScript==

(function () {
    "use strict";

    const TEXT = {
        toggle: "\u6295\u5c4f",
        title: "\u6295\u5c4f\u5230\u7535\u89c6",
        description: "\u5148\u9009\u7535\u89c6\uff0c\u518d\u628a\u5f53\u524d\u7f51\u9875\u89c6\u9891\u6295\u51fa\u53bb",
        statusLabel: "\u5f53\u524d\u72b6\u6001",
        bridgeLabel: "\u672c\u5730 Bridge",
        bridgeHint: "\u5982\u679c\u8fde\u63a5\u5931\u8d25\uff0c\u5148\u5728\u7535\u8111\u4e0a\u8fd0\u884c python run_bridge.py",
        ping: "\u68c0\u6d4b",
        debugLabel: "\u8c03\u8bd5\u5bfc\u51fa",
        debugExport: "\u5bfc\u51fa\u8c03\u8bd5 JSON",
        debugHint: "\u9047\u5230\u4e0d\u652f\u6301\u7684\u7ad9\u70b9\u65f6\uff0c\u53ef\u5bfc\u51fa\u5f53\u524d\u9875\u9762\u7684\u5019\u9009\u89c6\u9891\u6e90\u4fe1\u606f\u7ed9\u6211\u7ee7\u7eed\u9002\u914d\u3002",
        deviceLabel: "\u7535\u89c6",
        deviceHint: "\u7535\u8111\u548c\u7535\u89c6\u9700\u5728\u540c\u4e00\u5c40\u57df\u7f51",
        scan: "\u626b\u63cf\u7535\u89c6",
        sourceLabel: "\u89c6\u9891\u6e90",
        sourceHint: "\u4f18\u5148\u4f7f\u7528\u5f53\u524d\u9875\u9762\u89e3\u6790\u6216\u81ea\u52a8\u8bc6\u522b\u5230\u7684\u76f4\u94fe",
        qualityLabel: "\u753b\u8d28",
        qualityHint: "\u53ea\u5bf9\u652f\u6301\u591a\u6863\u753b\u8d28\u7684\u64ad\u653e\u9875\u751f\u6548\uff0c\u624b\u52a8\u76f4\u94fe\u901a\u5e38\u4e0d\u4f1a\u6539\u753b\u8d28",
        qualityBest: "\u6700\u9ad8\u53ef\u7528",
        quality1080: "1080p",
        quality720: "720p",
        modeLabel: "\u6295\u5c4f\u6a21\u5f0f",
        modeHint: "\u539f\u6837\u542f\u52a8\u6700\u5feb\uff1b\u753b\u8d28\u4f18\u5148\u548c\u6d41\u7545 60fps \u4f1a\u5148\u5728\u672c\u5730\u91cd\u7f16\u7801\uff0c\u5f00\u59cb\u66f4\u6162",
        modeStandard: "\u539f\u6837\u4f18\u5148",
        modeQuality: "\u753b\u8d28\u4f18\u5148",
        modeSmooth: "\u6d41\u7545 60fps",
        refresh: "\u91cd\u65b0\u8bc6\u522b",
        manualLabel: "\u624b\u52a8\u94fe\u63a5",
        manualPlaceholder: "\u7c98\u8d34\u89c6\u9891\u76f4\u94fe\u6216\u64ad\u653e\u9875\u94fe\u63a5",
        manualHint: "\u53ea\u5728\u81ea\u52a8\u8bc6\u522b\u4e0d\u51c6\u65f6\u518d\u624b\u52a8\u586b\u5199",
        start: "\u5f00\u59cb\u6295\u5c4f",
        stop: "\u505c\u6b62\u6295\u5c4f",
        advanced: "\u9ad8\u7ea7\u8bbe\u7f6e",
        close: "\u6536\u8d77",
        waiting: "\u51c6\u5907\u5c31\u7eea\uff0c\u5148\u626b\u63cf\u7535\u89c6\u3002",
        noDevices: "\u8fd8\u6ca1\u6709\u626b\u5230\u7535\u89c6",
    };

    const STORAGE_KEY = "screenCastingBridgeBaseUrl";
    const QUALITY_STORAGE_KEY = "screenCastingPreferredQuality";
    const MODE_STORAGE_KEY = "screenCastingTranscodeProfile";
    const DEFAULT_BRIDGE_URL = "http://127.0.0.1:9527";
    const DEFAULT_QUALITY = "max";
    const DEFAULT_MODE = "standard";
    const UI_TOGGLE_ID = "tm-cast-toggle";
    const UI_PANEL_ID = "tm-cast-panel";
    const MEDIA_EXTENSIONS = [".m3u8", ".mp4", ".m4v", ".mov", ".webm", ".mpd", ".flv", ".mkv", ".avi", ".wmv", ".ts"];
    const MEDIA_URL_PATTERN = /https?:\/\/[^"'`\s<>()\\]+?(?:\.m3u8|\.mp4|\.m4v|\.mov|\.webm|\.mpd|\.flv|\.mkv|\.avi|\.wmv|\.ts)(?:\?[^"'`\s<>()\\]*)?/ig;
    const SPECIAL_GLOBALS = {
        bilibili: ["__playinfo__", "__INITIAL_STATE__"],
        tencent: ["__PLAYER_CONFIG__", "__PLAYER__CONFIG__", "VIDEO_INFO", "COVER_INFO", "__NEXT_DATA__"],
    };

    const STATE = {
        bridgeBaseUrl: normalizeBridgeBaseUrl(localStorage.getItem(STORAGE_KEY) || DEFAULT_BRIDGE_URL),
        qualityPreference: normalizeQualityPreference(localStorage.getItem(QUALITY_STORAGE_KEY) || DEFAULT_QUALITY),
        transcodeProfile: normalizeTranscodeProfile(localStorage.getItem(MODE_STORAGE_KEY) || DEFAULT_MODE),
        devices: [],
        candidateMap: new Map(),
        refreshTimer: 0,
        uiReady: false,
        observer: null,
        networkCandidates: new Map(),
    };

    let elements = null;

    function isTopLevelPage() {
        try {
            return window.top === window.self;
        } catch (error) {
            return false;
        }
    }

    function normalizeBridgeBaseUrl(value) {
        const text = String(value || "").trim();
        return (text || DEFAULT_BRIDGE_URL).replace(/\/+$/, "");
    }

    function normalizeQualityPreference(value) {
        const text = String(value || "").trim().toLowerCase();
        if (text === "1080" || text === "1080p") {
            return "1080p";
        }
        if (text === "720" || text === "720p") {
            return "720p";
        }
        return "max";
    }

    function normalizeTranscodeProfile(value) {
        const text = String(value || "").trim().toLowerCase();
        if (text === "quality" || text === "hq") {
            return "quality";
        }
        if (text === "smooth" || text === "60fps" || text === "smooth60") {
            return "smooth";
        }
        return "standard";
    }

    function getQualityLabel(value) {
        const quality = normalizeQualityPreference(value);
        if (quality === "1080p") {
            return TEXT.quality1080;
        }
        if (quality === "720p") {
            return TEXT.quality720;
        }
        return TEXT.qualityBest;
    }

    function getModeLabel(value) {
        const profile = normalizeTranscodeProfile(value);
        if (profile === "quality") {
            return TEXT.modeQuality;
        }
        if (profile === "smooth") {
            return TEXT.modeSmooth;
        }
        return TEXT.modeStandard;
    }

    function isHttpUrl(value) {
        return /^https?:\/\//i.test(String(value || "").trim());
    }

    function getPlayableUrlKind(url) {
        if (!isHttpUrl(url)) {
            return "";
        }

        let pathname = "";
        try {
            pathname = new URL(url, location.href).pathname.toLowerCase();
        } catch (error) {
            pathname = String(url || "").trim().toLowerCase().split("?")[0];
        }

        if (pathname.endsWith(".m3u8")) {
            return "hls";
        }
        if (pathname.endsWith(".mpd")) {
            return "dash";
        }
        if (pathname.endsWith(".mp4") || pathname.endsWith(".m4v") || pathname.endsWith(".mov") || pathname.endsWith(".webm")) {
            return "file";
        }
        if (pathname.endsWith(".flv") || pathname.endsWith(".mkv") || pathname.endsWith(".avi") || pathname.endsWith(".wmv")) {
            return "container";
        }
        if (pathname.endsWith(".ts")) {
            return "segment";
        }
        return "";
    }

    function looksLikePlayableMediaUrl(url) {
        return Boolean(getPlayableUrlKind(url));
    }

    function isBlobUrl(value) {
        return /^blob:/i.test(String(value || "").trim());
    }

    function toAbsoluteUrl(value) {
        const text = String(value || "").trim();
        if (!text) {
            return "";
        }
        try {
            return new URL(text, location.href).href;
        } catch (error) {
            return "";
        }
    }

    function getReadableName(url) {
        try {
            const parsed = new URL(url, location.href);
            const parts = parsed.pathname.split("/").filter(Boolean);
            return decodeURIComponent(parts[parts.length - 1] || parsed.hostname || url);
        } catch (error) {
            return url;
        }
    }

    function shortenLabel(text, maxLength) {
        const value = String(text || "").trim();
        if (value.length <= maxLength) {
            return value;
        }
        return `${value.slice(0, Math.max(0, maxLength - 1))}\u2026`;
    }

    function formatCandidateLabel(candidate) {
        if (!candidate) {
            return "";
        }
        if (candidate.sourceType === "page") {
            if (pageParsePreferredHost(location.hostname)) {
                return "\u5f53\u524d\u9875\u9762\u89e3\u6790\uff08\u53d7\u652f\u6301\u7ad9\u70b9\u63a8\u8350\uff09";
            }
            return "\u5f53\u524d\u9875\u9762\u89e3\u6790\uff08\u4ec5\u53d7\u652f\u6301\u7ad9\u70b9\uff09";
        }

        const sourceNames = {
            video: "\u9875\u9762\u64ad\u653e\u5668",
            "video-source": "\u64ad\u653e\u5668\u5907\u7528\u6e90",
            ldjson: "\u9875\u9762\u6570\u636e",
            resource: "\u9875\u9762\u8d44\u6e90",
            fetch: "\u7f51\u9875\u8bf7\u6c42",
            xhr: "\u7f51\u9875\u8bf7\u6c42",
            "inline-script": "\u9875\u9762\u811a\u672c",
            "bilibili-global": "Bilibili \u64ad\u653e\u6570\u636e",
            "tencent-global": "\u817e\u8baf\u64ad\u653e\u6570\u636e",
        };

        const prefix = sourceNames[candidate.sourceType] || "\u81ea\u52a8\u8bc6\u522b";
        const suffix = shortenLabel(getReadableName(candidate.url) || candidate.displayName || candidate.label, 34);
        const kind = getPlayableUrlKind(candidate.url);
        const kindLabel = {
            hls: "HLS",
            dash: "DASH",
            file: "MP4/WebM",
            container: "\u5a92\u4f53\u6587\u4ef6",
            segment: "TS \u5206\u7247",
        }[kind] || "\u89c6\u9891\u6e90";
        return `${prefix} \u00b7 ${suffix} \u00b7 ${kindLabel}`;
    }

    function rememberBridgeUrl(value) {
        STATE.bridgeBaseUrl = normalizeBridgeBaseUrl(value);
        localStorage.setItem(STORAGE_KEY, STATE.bridgeBaseUrl);
        if (elements) {
            elements.bridgeInput.value = STATE.bridgeBaseUrl;
        }
    }

    function rememberQualityPreference(value) {
        STATE.qualityPreference = normalizeQualityPreference(value);
        localStorage.setItem(QUALITY_STORAGE_KEY, STATE.qualityPreference);
        if (elements) {
            elements.qualitySelect.value = STATE.qualityPreference;
        }
    }

    function rememberTranscodeProfile(value) {
        STATE.transcodeProfile = normalizeTranscodeProfile(value);
        localStorage.setItem(MODE_STORAGE_KEY, STATE.transcodeProfile);
        if (elements) {
            elements.modeSelect.value = STATE.transcodeProfile;
        }
    }

    function buildForwardHeaders() {
        const headers = {
            Referer: location.href,
            Origin: location.origin,
            "User-Agent": navigator.userAgent,
        };
        if (document.cookie) {
            headers.Cookie = document.cookie;
        }
        return headers;
    }

    function delay(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }

    function requestBridge(method, path, payload, timeoutMs) {
        const url = `${STATE.bridgeBaseUrl}${path}`;
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method,
                url,
                timeout: timeoutMs || 15000,
                headers: payload ? { "Content-Type": "application/json" } : {},
                data: payload ? JSON.stringify(payload) : undefined,
                onload(response) {
                    let data = {};
                    try {
                        data = response.responseText ? JSON.parse(response.responseText) : {};
                    } catch (error) {
                        reject(new Error("Bridge \u8fd4\u56de\u4e86\u65e0\u6cd5\u89e3\u6790\u7684\u6570\u636e\u3002"));
                        return;
                    }
                    if (response.status >= 200 && response.status < 300 && data.ok !== false) {
                        resolve(data);
                        return;
                    }
                    reject(new Error(data.error || `Bridge \u8bf7\u6c42\u5931\u8d25\uff0cHTTP ${response.status}\u3002`));
                },
                onerror() {
                    reject(new Error(`\u65e0\u6cd5\u8fde\u63a5\u672c\u5730 Bridge\uff1a${url}`));
                },
                ontimeout() {
                    reject(new Error(`\u672c\u5730 Bridge \u54cd\u5e94\u8d85\u65f6\uff1a${url}`));
                },
            });
        });
    }

    async function pollTask(taskId) {
        const deadline = Date.now() + 20 * 60 * 1000;
        while (Date.now() < deadline) {
            const payload = await requestBridge("GET", `/api/tasks/${encodeURIComponent(taskId)}`, null, 20000);
            if (payload.status === "completed") {
                return payload.result || {};
            }
            if (payload.status === "failed") {
                throw new Error(payload.error || payload.message || "\u6295\u5c4f\u4efb\u52a1\u5931\u8d25\u3002");
            }
            setStatus(payload.message || formatTaskStatus(payload.status), "neutral");
            await delay(2000);
        }
        throw new Error("\u6295\u5c4f\u5904\u7406\u65f6\u95f4\u8fc7\u957f\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002");
    }

    function formatTaskStatus(status) {
        const value = String(status || "").trim().toLowerCase();
        if (value === "queued") {
            return "\u6295\u5c4f\u4efb\u52a1\u5df2\u63d0\u4ea4\uff0c\u6b63\u5728\u6392\u961f...";
        }
        if (value === "running" || value === "resolving") {
            return "\u6b63\u5728\u89e3\u6790\u5f53\u524d\u9875\u9762\u89c6\u9891...";
        }
        if (value === "scanning") {
            return "\u6b63\u5728\u5b9a\u4f4d\u7535\u89c6...";
        }
        if (value === "muxing") {
            return "\u6b63\u5728\u51c6\u5907\u53ef\u64ad\u653e\u7684\u89c6\u9891\u6587\u4ef6...";
        }
        if (value === "transcoding") {
            return "\u6b63\u5728\u505a\u672c\u5730\u89c6\u9891\u4f18\u5316...";
        }
        if (value === "serving") {
            return "\u6b63\u5728\u51c6\u5907\u6295\u5c4f\u94fe\u63a5...";
        }
        if (value === "casting") {
            return "\u6b63\u5728\u628a\u64ad\u653e\u6307\u4ee4\u53d1\u9001\u5230\u7535\u89c6...";
        }
        if (value === "completed") {
            return "\u6295\u5c4f\u6307\u4ee4\u5df2\u53d1\u9001\u3002";
        }
        if (value === "failed") {
            return "\u6295\u5c4f\u5931\u8d25\u3002";
        }
        return TEXT.waiting;
    }

    function formatStatusMessage(message) {
        const text = String(message || "").trim();
        if (!text) {
            return TEXT.waiting;
        }

        const directMap = {
            "Cast task queued.": "\u6295\u5c4f\u4efb\u52a1\u5df2\u63d0\u4ea4\uff0c\u6b63\u5728\u6392\u961f...",
            "Cast task accepted.": "\u5df2\u63d0\u4ea4\u6295\u5c4f\u4efb\u52a1\uff0c\u6b63\u5728\u5904\u7406\u89c6\u9891...",
            "Cast task started.": "\u6b63\u5728\u5f00\u59cb\u5904\u7406\u6295\u5c4f\u4efb\u52a1...",
            "Cast task failed.": "\u6295\u5c4f\u4efb\u52a1\u5931\u8d25\u3002",
            "Cast task completed.": "\u6295\u5c4f\u6307\u4ee4\u5df2\u53d1\u9001\u3002",
            "Resolving page source...": "\u6b63\u5728\u89e3\u6790\u5f53\u524d\u9875\u9762\u89c6\u9891...",
            "Locating target TV...": "\u6b63\u5728\u5b9a\u4f4d\u7535\u89c6...",
            "Downloading streams and preparing a compatible MP4...": "\u6b63\u5728\u51c6\u5907\u53ef\u64ad\u653e\u7684\u89c6\u9891\u6587\u4ef6...",
            "Downloading streams and applying local quality optimization...": "\u6b63\u5728\u4e0b\u8f7d\u89c6\u9891\u5e76\u505a\u672c\u5730\u753b\u8d28\u4f18\u5316...",
            "Downloading streams and rendering smoother 60fps playback...": "\u6b63\u5728\u4e0b\u8f7d\u89c6\u9891\u5e76\u751f\u6210\u66f4\u6d41\u7545\u7684 60fps \u7248\u672c...",
            "Downloading streams and rendering smoother 60fps playback with hardware encoding...": "\u6b63\u5728\u4e0b\u8f7d\u89c6\u9891\uff0c\u4f7f\u7528\u786c\u4ef6\u7f16\u7801\u751f\u6210 60fps \u7248\u672c...",
            "60fps hardware encoding unavailable. Falling back to local quality optimization...": "60fps \u786c\u4ef6\u7f16\u7801\u4e0d\u53ef\u7528\uff0c\u5df2\u81ea\u52a8\u964d\u7ea7\u4e3a\u753b\u8d28\u4f18\u5316\u3002",
            "Preparing media URL for the TV...": "\u6b63\u5728\u51c6\u5907\u6295\u5c4f\u94fe\u63a5...",
            "Applying local quality optimization...": "\u6b63\u5728\u505a\u672c\u5730\u753b\u8d28\u4f18\u5316...",
            "Rendering smoother 60fps playback...": "\u6b63\u5728\u751f\u6210\u66f4\u6d41\u7545\u7684 60fps \u64ad\u653e\u7248\u672c...",
            "Rendering smoother 60fps playback with hardware encoding...": "\u6b63\u5728\u4f7f\u7528\u786c\u4ef6\u7f16\u7801\u751f\u6210 60fps \u64ad\u653e\u7248\u672c...",
            "Sending playback command to the TV...": "\u6b63\u5728\u628a\u64ad\u653e\u6307\u4ee4\u53d1\u9001\u5230\u7535\u89c6...",
            "Task not found": "\u672a\u627e\u5230\u6295\u5c4f\u4efb\u52a1\u3002",
            "Not found": "\u8bf7\u6c42\u63a5\u53e3\u4e0d\u5b58\u5728\u3002",
        };
        if (directMap[text]) {
            return directMap[text];
        }

        let match = text.match(/^Found (\d+) DLNA devices\.$/i);
        if (match) {
            return `\u5df2\u627e\u5230 ${match[1]} \u53f0\u53ef\u6295\u5c4f\u8bbe\u5907\u3002`;
        }

        match = text.match(/^Cast sent to (.+)\.$/i);
        if (match) {
            return `\u5df2\u53d1\u9001\u5230 ${match[1]}\u3002`;
        }

        match = text.match(/^Task status:\s*(.+)$/i);
        if (match) {
            return formatTaskStatus(match[1]);
        }

        return text;
    }

    function setStatus(message, tone) {
        if (!elements) {
            return;
        }
        elements.status.textContent = formatStatusMessage(message);
        elements.status.dataset.tone = tone || "neutral";
    }

    function setPanelOpen(opened) {
        if (!elements) {
            return;
        }
        const value = opened ? "true" : "false";
        elements.panel.setAttribute("data-open", value);
        elements.toggle.setAttribute("data-open", value);
        elements.toggle.setAttribute("aria-expanded", String(opened));
    }

    function createCandidate(url, label, sourceType, displayName, priority) {
        return {
            url,
            label,
            sourceType,
            displayName: displayName || document.title.trim() || getReadableName(url),
            priority,
        };
    }

    function addCandidate(url, label, sourceType, displayName, priority) {
        const absoluteUrl = toAbsoluteUrl(url);
        if (!looksLikePlayableMediaUrl(absoluteUrl)) {
            return;
        }
        const nextCandidate = createCandidate(absoluteUrl, label, sourceType, displayName, priority);
        const existing = STATE.candidateMap.get(absoluteUrl);
        if (!existing || nextCandidate.priority < existing.priority) {
            STATE.candidateMap.set(absoluteUrl, nextCandidate);
        }
    }

    function addPageResolveCandidate() {
        const title = document.title.trim() || location.hostname;
        STATE.candidateMap.set(
            location.href,
            {
                url: location.href,
                label: `Page parse: ${title}`,
                sourceType: "page",
                displayName: title,
                priority: 0,
            }
        );
    }

    function sortCandidates(candidates) {
        return candidates.slice().sort((left, right) => {
            if (left.priority !== right.priority) {
                return left.priority - right.priority;
            }
            return left.label.localeCompare(right.label);
        });
    }

    function pageParsePreferredHost(hostname) {
        const host = String(hostname || "").toLowerCase();
        return host.endsWith("bilibili.com") || host.endsWith("qq.com") || host.endsWith("v.qq.com");
    }

    function isDirectPlayableCandidate(candidate) {
        if (!candidate || candidate.sourceType === "page") {
            return false;
        }
        const kind = getPlayableUrlKind(candidate.url);
        return kind === "hls" || kind === "dash" || kind === "file";
    }

    function hasBlobVideoElement() {
        return Array.from(document.querySelectorAll("video")).some((video) => {
            return isBlobUrl(video.currentSrc) || isBlobUrl(video.src);
        });
    }

    function pickDefaultCandidate(candidates) {
        if (!candidates.length) {
            return null;
        }

        const directManifestOrFile = candidates.find((candidate) => isDirectPlayableCandidate(candidate));
        const pageCandidate = candidates.find((candidate) => candidate.sourceType === "page") || null;

        if (!pageParsePreferredHost(location.hostname) && directManifestOrFile) {
            return directManifestOrFile;
        }
        return pageCandidate || directManifestOrFile || candidates[0];
    }

    function extractUrlsFromObject(root, labelPrefix, sourceType, priority) {
        if (!root || (typeof root !== "object" && !Array.isArray(root))) {
            return;
        }
        const queue = [root];
        const visited = new WeakSet();
        let scanBudget = 0;

        while (queue.length && scanBudget < 300) {
            scanBudget += 1;
            const current = queue.shift();
            if (!current || typeof current !== "object") {
                continue;
            }
            if (visited.has(current)) {
                continue;
            }
            visited.add(current);

            const values = Array.isArray(current) ? current : Object.values(current);
            values.forEach((value) => {
                if (typeof value === "string") {
                    if (looksLikePlayableMediaUrl(value)) {
                        addCandidate(value, `${labelPrefix}: ${getReadableName(value)}`, sourceType, document.title.trim(), priority);
                    }
                    return;
                }
                if (value && typeof value === "object") {
                    queue.push(value);
                }
            });
        }
    }

    function decodeInlineScriptText(text) {
        return String(text || "")
            .replace(/\\u002F/gi, "/")
            .replace(/\\u003A/gi, ":")
            .replace(/\\\//g, "/")
            .replace(/&amp;/g, "&");
    }

    function extractInlineScriptCandidates(labelPrefix, priority) {
        const seen = new Set();
        document.querySelectorAll("script").forEach((script) => {
            const text = decodeInlineScriptText(script.textContent);
            if (!text) {
                return;
            }
            const matches = text.match(MEDIA_URL_PATTERN) || [];
            matches.forEach((url) => {
                if (seen.has(url)) {
                    return;
                }
                seen.add(url);
                addCandidate(url, `${labelPrefix}: ${getReadableName(url)}`, "inline-script", document.title.trim(), priority);
            });
        });
    }

    function extractLdJsonCandidates() {
        document.querySelectorAll('script[type="application/ld+json"]').forEach((script) => {
            try {
                const data = JSON.parse(script.textContent || "{}");
                extractUrlsFromObject(data, "ld+json", "ldjson", 40);
            } catch (error) {
                // Ignore broken ld+json payloads.
            }
        });
    }

    function extractVideoElementCandidates() {
        document.querySelectorAll("video").forEach((video, index) => {
            [video.currentSrc, video.src].forEach((url) => {
                addCandidate(url, `video ${index + 1}: ${getReadableName(url)}`, "video", document.title.trim(), 30);
            });
            video.querySelectorAll("source").forEach((source, sourceIndex) => {
                addCandidate(
                    source.src,
                    `source ${index + 1}.${sourceIndex + 1}: ${getReadableName(source.src)}`,
                    "video-source",
                    document.title.trim(),
                    32
                );
            });
        });
    }

    function extractPerformanceCandidates() {
        performance.getEntriesByType("resource").forEach((entry) => {
            addCandidate(entry.name, `resource: ${getReadableName(entry.name)}`, "resource", document.title.trim(), 55);
        });
    }

    function extractNetworkCandidates() {
        Array.from(STATE.networkCandidates.values()).forEach((candidate) => {
            addCandidate(candidate.url, candidate.label, candidate.sourceType, document.title.trim(), candidate.priority);
        });
    }

    function safeGlobal(name) {
        try {
            return window[name];
        } catch (error) {
            return undefined;
        }
    }

    function extractBilibiliCandidates() {
        SPECIAL_GLOBALS.bilibili.forEach((globalName) => {
            extractUrlsFromObject(safeGlobal(globalName), `bilibili ${globalName}`, "bilibili-global", 12);
        });
        extractInlineScriptCandidates("bilibili script", 18);
    }

    function extractTencentCandidates() {
        SPECIAL_GLOBALS.tencent.forEach((globalName) => {
            extractUrlsFromObject(safeGlobal(globalName), `tencent ${globalName}`, "tencent-global", 14);
        });
        extractInlineScriptCandidates("tencent script", 20);
    }

    function extractSiteSpecificCandidates() {
        const host = location.hostname.toLowerCase();
        if (host.endsWith("bilibili.com")) {
            extractBilibiliCandidates();
            return;
        }
        if (host.endsWith("qq.com") || host.endsWith("v.qq.com")) {
            extractTencentCandidates();
            return;
        }
        extractInlineScriptCandidates("page script", 45);
    }

    function renderDeviceOptions(selectedLocation) {
        if (!elements) {
            return;
        }
        elements.deviceSelect.innerHTML = "";
        if (!STATE.devices.length) {
            const option = document.createElement("option");
            option.value = "";
            option.textContent = TEXT.noDevices;
            elements.deviceSelect.appendChild(option);
            updateActionState();
            return;
        }
        STATE.devices.forEach((device, index) => {
            const option = document.createElement("option");
            option.value = device.location;
            option.textContent = device.display_name;
            if (device.location === selectedLocation || (!selectedLocation && index === 0)) {
                option.selected = true;
            }
            elements.deviceSelect.appendChild(option);
        });
        updateActionState();
    }

    function renderSourceOptions(previousValue) {
        if (!elements) {
            return;
        }
        const candidates = sortCandidates(Array.from(STATE.candidateMap.values()));
        const directCount = candidates.filter((candidate) => candidate.sourceType !== "page").length;
        const directPlayableCount = candidates.filter((candidate) => isDirectPlayableCandidate(candidate)).length;
        const defaultCandidate = previousValue ? null : pickDefaultCandidate(candidates);
        const blobVideoOnly = hasBlobVideoElement();
        elements.sourceSelect.innerHTML = "";
        candidates.forEach((candidate, index) => {
            const option = document.createElement("option");
            option.value = candidate.url;
            option.textContent = formatCandidateLabel(candidate);
            if (candidate.url === previousValue || (!previousValue && defaultCandidate && candidate.url === defaultCandidate.url) || (!previousValue && !defaultCandidate && index === 0)) {
                option.selected = true;
            }
            elements.sourceSelect.appendChild(option);
        });
        if (!candidates.length) {
            elements.sourceHint.textContent = "\u8fd8\u6ca1\u8bc6\u522b\u5230\u76f4\u63a5\u89c6\u9891\u6e90\uff0c\u53ef\u5148\u5c1d\u8bd5\u9875\u9762\u89e3\u6790\uff0c\u6216\u5728\u9ad8\u7ea7\u8bbe\u7f6e\u91cc\u624b\u52a8\u7c98\u8d34\u94fe\u63a5\u3002";
        } else if (!directCount && candidates[0].sourceType === "page") {
            if (blobVideoOnly) {
                elements.sourceHint.textContent = "\u5f53\u524d\u9875\u9762\u64ad\u653e\u5668\u663e\u793a\u4e3a blob \u5730\u5740\uff0c\u8bf4\u660e\u771f\u5b9e\u89c6\u9891\u6d41\u8fd8\u6ca1\u88ab\u6293\u5230\u3002\u201c\u5f53\u524d\u9875\u9762\u89e3\u6790\u201d\u53ea\u9002\u7528\u4e8e\u5c11\u6570\u53d7\u652f\u6301\u7ad9\u70b9\uff0c\u8fd9\u79cd\u60c5\u51b5\u5f80\u5f80\u9700\u8981\u4e13\u9879\u9002\u914d\u3002";
            } else {
                elements.sourceHint.textContent = "\u5f53\u524d\u53ea\u5269\u201c\u5f53\u524d\u9875\u9762\u89e3\u6790\u201d\u53ef\u7528\u3002\u5b83\u53ea\u9002\u7528\u4e8e\u53d7\u652f\u6301\u7684\u64ad\u653e\u9875\uff0c\u5982\u679c\u5931\u8d25\uff0c\u8bf7\u6539\u7528\u76f4\u94fe\u6216\u624b\u52a8\u94fe\u63a5\u3002";
            }
        } else if (defaultCandidate && defaultCandidate.sourceType !== "page" && !pageParsePreferredHost(location.hostname)) {
            elements.sourceHint.textContent = "\u5df2\u8bc6\u522b\u5230\u53ef\u76f4\u63a5\u6295\u5c4f\u7684\u89c6\u9891\u6d41\uff0c\u5df2\u81ea\u52a8\u4f18\u5148\u9009\u62e9\u5b83\u3002\u201c\u5f53\u524d\u9875\u9762\u89e3\u6790\u201d\u53ea\u9002\u7528\u4e8e\u5c11\u6570\u53d7\u652f\u6301\u7ad9\u70b9\u3002";
        } else if (directPlayableCount > 0) {
            elements.sourceHint.textContent = `\u5df2\u8bc6\u522b\u5230 ${directPlayableCount} \u4e2a\u53ef\u76f4\u63a5\u6295\u5c4f\u7684\u89c6\u9891\u6e90\u3002\u4f18\u5148\u9009 HLS / DASH / MP4 \u76f4\u94fe\uff0c\u201c\u5f53\u524d\u9875\u9762\u89e3\u6790\u201d\u53ea\u9002\u7528\u4e8e\u53d7\u652f\u6301\u7ad9\u70b9\u3002`;
        } else {
            elements.sourceHint.textContent = `\u989d\u5916\u8bc6\u522b\u5230 ${directCount} \u4e2a\u89c6\u9891\u6e90\uff0c\u4f18\u5148\u4f7f\u7528\u201c\u5f53\u524d\u9875\u9762\u89e3\u6790\uff08\u63a8\u8350\uff09\u201d\u3002`;
        }
        updateActionState();
    }

    function collectCandidates() {
        const previousValue = elements ? elements.sourceSelect.value : "";
        STATE.candidateMap.clear();
        addPageResolveCandidate();
        extractSiteSpecificCandidates();
        extractVideoElementCandidates();
        extractLdJsonCandidates();
        extractPerformanceCandidates();
        extractNetworkCandidates();
        renderSourceOptions(previousValue);
    }

    function scheduleCollectCandidates() {
        if (STATE.refreshTimer) {
            clearTimeout(STATE.refreshTimer);
        }
        STATE.refreshTimer = window.setTimeout(collectCandidates, 250);
    }

    function listVideoElements() {
        return Array.from(document.querySelectorAll("video")).map((video, index) => ({
            index: index + 1,
            currentSrc: String(video.currentSrc || "").trim(),
            src: String(video.src || "").trim(),
            poster: String(video.poster || "").trim(),
            sourceUrls: Array.from(video.querySelectorAll("source"))
                .map((source) => String(source.src || "").trim())
                .filter(Boolean),
        }));
    }

    function buildDebugPayload() {
        const candidates = sortCandidates(Array.from(STATE.candidateMap.values())).map((candidate) => ({
            url: candidate.url,
            label: candidate.label,
            sourceType: candidate.sourceType,
            displayName: candidate.displayName,
            priority: candidate.priority,
        }));
        const networkCandidates = sortCandidates(Array.from(STATE.networkCandidates.values())).map((candidate) => ({
            url: candidate.url,
            label: candidate.label,
            sourceType: candidate.sourceType,
            priority: candidate.priority,
        }));
        return {
            exportedAt: new Date().toISOString(),
            page: {
                url: location.href,
                title: document.title,
                host: location.hostname,
                referrer: document.referrer || "",
            },
            bridge: {
                baseUrl: STATE.bridgeBaseUrl,
                qualityPreference: STATE.qualityPreference,
                transcodeProfile: STATE.transcodeProfile,
            },
            selection: {
                selectedSourceUrl: elements ? elements.sourceSelect.value : "",
                selectedDeviceLocation: elements ? elements.deviceSelect.value : "",
                manualInput: elements ? elements.manualInput.value.trim() : "",
            },
            counts: {
                candidates: candidates.length,
                networkCandidates: networkCandidates.length,
                videoElements: document.querySelectorAll("video").length,
            },
            candidates,
            networkCandidates,
            videoElements: listVideoElements(),
            userAgent: navigator.userAgent,
        };
    }

    function downloadTextFile(filename, content) {
        const blob = new Blob([content], { type: "application/json;charset=utf-8" });
        const objectUrl = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = objectUrl;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    }

    function exportDebugData() {
        collectCandidates();
        const payload = buildDebugPayload();
        const content = JSON.stringify(payload, null, 2);
        const safeHost = location.hostname.replace(/[^a-z0-9.-]+/gi, "_") || "page";
        const fileName = `screen-casting-debug-${safeHost}-${Date.now()}.json`;
        console.log("[Screen Casting Debug]", payload);
        downloadTextFile(fileName, content);
        setStatus(`\u8c03\u8bd5\u4fe1\u606f\u5df2\u5bfc\u51fa\uff1a${fileName}`, "success");
    }

    async function checkBridge() {
        rememberBridgeUrl(elements.bridgeInput.value);
        setStatus("\u6b63\u5728\u68c0\u6d4b\u672c\u5730 Bridge...", "neutral");
        try {
            const health = await requestBridge("GET", "/api/health");
            const extractorHint = health.extractor_available
                ? "\u9875\u9762\u89e3\u6790\u80fd\u529b\u53ef\u7528\u3002"
                : "Bridge \u5df2\u8fde\u63a5\uff0c\u4f46\u672a\u68c0\u6d4b\u5230 yt-dlp\uff0c\u90e8\u5206\u9875\u9762\u89e3\u6790\u53ef\u80fd\u5931\u8d25\u3002";
            setStatus(`\u672c\u5730 Bridge \u5df2\u8fde\u63a5\u3002${extractorHint}`, "success");
        } catch (error) {
            setStatus(`${error.message}\n${TEXT.bridgeHint}`, "error");
        }
    }

    async function scanDevices() {
        rememberBridgeUrl(elements.bridgeInput.value);
        const selectedLocation = elements.deviceSelect.value;
        setStatus("\u6b63\u5728\u626b\u63cf\u5c40\u57df\u7f51\u4e2d\u7684\u7535\u89c6...", "neutral");
        try {
            const payload = await requestBridge("GET", "/api/devices?timeout=5.5", null, 22000);
            STATE.devices = Array.isArray(payload.devices) ? payload.devices : [];
            renderDeviceOptions(selectedLocation);
            if (STATE.devices.length) {
                setStatus(`\u5df2\u627e\u5230 ${STATE.devices.length} \u53f0\u53ef\u6295\u5c4f\u8bbe\u5907\u3002`, "success");
            } else {
                setStatus("\u8fd8\u6ca1\u6709\u53d1\u73b0\u7535\u89c6\uff0c\u8bf7\u786e\u8ba4\u7535\u8111\u548c\u7535\u89c6\u5728\u540c\u4e00\u5c40\u57df\u7f51\u3002", "warning");
            }
        } catch (error) {
            setStatus(error.message, "error");
        }
    }

    function getSelectedSource() {
        const manualUrl = elements.manualInput.value.trim();
        if (manualUrl) {
            return {
                url: manualUrl,
                displayName: document.title.trim() || getReadableName(manualUrl),
            };
        }
        const selectedUrl = elements.sourceSelect.value;
        return STATE.candidateMap.get(selectedUrl) || null;
    }

    async function castSelectedSource() {
        rememberBridgeUrl(elements.bridgeInput.value);
        rememberQualityPreference(elements.qualitySelect.value);
        rememberTranscodeProfile(elements.modeSelect.value);
        const deviceLocation = elements.deviceSelect.value;
        const source = getSelectedSource();

        if (!deviceLocation) {
            setStatus("\u8bf7\u5148\u9009\u62e9\u7535\u89c6\u3002", "warning");
            return;
        }
        if (!source || !source.url) {
            setStatus("\u6ca1\u6709\u53ef\u7528\u89c6\u9891\u6e90\uff0c\u8bf7\u91cd\u65b0\u8bc6\u522b\u6216\u624b\u52a8\u7c98\u8d34\u94fe\u63a5\u3002", "warning");
            return;
        }

        setStatus(
            `\u6b63\u5728\u53d1\u9001\u6295\u5c4f\u4efb\u52a1\uff08${getQualityLabel(STATE.qualityPreference)} / ${getModeLabel(STATE.transcodeProfile)}\uff09...`,
            "neutral"
        );
        try {
            const accepted = await requestBridge("POST", "/api/cast", {
                device_location: deviceLocation,
                source_url: source.url,
                display_name: source.displayName || document.title.trim(),
                headers: buildForwardHeaders(),
                quality: STATE.qualityPreference,
                transcode_profile: STATE.transcodeProfile,
                speed: "1",
                async: true,
            }, 20000);
            if (accepted.task_id) {
                setStatus(accepted.message || "\u5df2\u63d0\u4ea4\u6295\u5c4f\u4efb\u52a1\uff0c\u6b63\u5728\u5904\u7406\u89c6\u9891...", "neutral");
            }
            const payload = accepted.task_id ? await pollTask(accepted.task_id) : accepted;
            if (payload.player_url) {
                setStatus(`\u5df2\u53d1\u9001\u5230\u7535\u89c6\u3002\u5982\u679c\u76f4\u94fe\u65e0\u6cd5\u64ad\u653e\uff0c\u53ef\u7528\u5907\u7528\u64ad\u653e\u9875\uff1a${payload.player_url}`, "success");
            } else {
                setStatus(`\u5df2\u53d1\u9001\u5230 ${payload.device.display_name}\u3002`, "success");
            }
        } catch (error) {
            setStatus(error.message, "error");
        }
    }

    async function stopCasting() {
        rememberBridgeUrl(elements.bridgeInput.value);
        setStatus("\u6b63\u5728\u505c\u6b62\u6295\u5c4f...", "neutral");
        try {
            await requestBridge("POST", "/api/stop", {});
            setStatus("\u6295\u5c4f\u5df2\u505c\u6b62\u3002", "success");
        } catch (error) {
            setStatus(error.message, "error");
        }
    }

    function updateActionState() {
        if (!elements) {
            return;
        }
        const hasDevice = Boolean(elements.deviceSelect.value);
        const hasSource = Boolean(elements.manualInput.value.trim() || elements.sourceSelect.value);
        elements.startButton.disabled = !(hasDevice && hasSource);
    }

    function captureNetworkCandidate(rawUrl, sourceType, priority) {
        const url = toAbsoluteUrl(rawUrl);
        if (!looksLikePlayableMediaUrl(url)) {
            return;
        }
        STATE.networkCandidates.set(url, {
            url,
            label: `${sourceType}: ${getReadableName(url)}`,
            sourceType,
            priority,
        });
        scheduleCollectCandidates();
    }

    function installRequestHooks() {
        const originalFetch = window.fetch;
        window.fetch = function (...args) {
            const input = args[0];
            const url = typeof input === "string" ? input : input && input.url;
            captureNetworkCandidate(url, "fetch", 52);
            return originalFetch.apply(this, args);
        };

        const originalOpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function (method, url, ...rest) {
            captureNetworkCandidate(url, "xhr", 50);
            return originalOpen.call(this, method, url, ...rest);
        };
    }

    function createUi() {
        if (STATE.uiReady || !document.body) {
            return;
        }
        if (document.getElementById(UI_TOGGLE_ID) || document.getElementById(UI_PANEL_ID)) {
            STATE.uiReady = true;
            return;
        }
        STATE.uiReady = true;

        GM_addStyle(`
            #tm-cast-toggle {
                position: fixed;
                top: 50%;
                right: 0;
                transform: translateY(-50%);
                z-index: 2147483645;
                border: 0;
                border-radius: 18px 0 0 18px;
                padding: 16px 10px 16px 12px;
                background: linear-gradient(180deg, #f97316, #ea580c);
                color: #fff7ed;
                font: 700 13px/1.05 "Segoe UI", "Microsoft YaHei UI", sans-serif;
                letter-spacing: 2px;
                writing-mode: vertical-rl;
                text-orientation: mixed;
                box-shadow: 0 18px 40px rgba(194, 65, 12, 0.28);
                cursor: pointer;
                transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
            }
            #tm-cast-toggle:hover {
                transform: translateY(-50%) translateX(-2px);
                box-shadow: 0 20px 44px rgba(194, 65, 12, 0.34);
            }
            #tm-cast-toggle[data-open="true"] {
                background: linear-gradient(180deg, #fb923c, #f97316);
            }
            #tm-cast-panel {
                position: fixed;
                top: 50%;
                right: 58px;
                transform: translateY(-50%);
                width: min(360px, calc(100vw - 86px));
                max-height: min(78vh, 720px);
                overflow: auto;
                z-index: 2147483646;
                border: 1px solid rgba(251, 146, 60, 0.18);
                border-radius: 24px;
                background:
                    radial-gradient(circle at top right, rgba(251, 146, 60, 0.12), transparent 32%),
                    linear-gradient(180deg, rgba(255, 255, 255, 0.98), rgba(255, 251, 235, 0.96));
                color: #172033;
                padding: 18px;
                box-shadow: 0 28px 64px rgba(15, 23, 42, 0.18);
                backdrop-filter: blur(18px);
                overscroll-behavior: contain;
                display: none;
            }
            #tm-cast-panel[data-open="true"] {
                display: block;
            }
            #tm-cast-panel .tm-header {
                display: flex;
                align-items: flex-start;
                justify-content: space-between;
                gap: 12px;
            }
            #tm-cast-panel .tm-kicker {
                display: inline-flex;
                align-items: center;
                padding: 4px 10px;
                border-radius: 999px;
                background: rgba(255, 237, 213, 0.95);
                color: #c2410c;
                font: 700 11px/1 "Segoe UI", "Microsoft YaHei UI", sans-serif;
                letter-spacing: 0.08em;
            }
            #tm-cast-panel h2 {
                margin: 10px 0 4px;
                font: 700 22px/1.25 "Segoe UI", "Microsoft YaHei UI", sans-serif;
            }
            #tm-cast-panel p,
            #tm-cast-panel label,
            #tm-cast-panel summary,
            #tm-cast-panel button,
            #tm-cast-panel input,
            #tm-cast-panel select {
                font: 14px/1.5 "Segoe UI", "Microsoft YaHei UI", sans-serif;
            }
            #tm-cast-panel p {
                margin: 0;
                color: #475569;
            }
            #tm-cast-panel .tm-section {
                margin-top: 14px;
                padding: 14px;
                border-radius: 18px;
                background: rgba(255, 255, 255, 0.92);
                border: 1px solid rgba(226, 232, 240, 0.95);
            }
            #tm-cast-panel .tm-section-head {
                display: flex;
                align-items: flex-start;
                gap: 10px;
            }
            #tm-cast-panel .tm-step {
                flex: 0 0 auto;
                width: 24px;
                height: 24px;
                border-radius: 999px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                background: rgba(255, 237, 213, 0.95);
                color: #c2410c;
                font: 700 13px/1 "Segoe UI", "Microsoft YaHei UI", sans-serif;
            }
            #tm-cast-panel .tm-section-title {
                color: #0f172a;
                font: 700 15px/1.3 "Segoe UI", "Microsoft YaHei UI", sans-serif;
            }
            #tm-cast-panel .tm-section-note {
                margin-top: 2px;
                color: #64748b;
                font: 12px/1.45 "Segoe UI", "Microsoft YaHei UI", sans-serif;
            }
            #tm-cast-panel .tm-row {
                display: flex;
                align-items: center;
                gap: 8px;
                margin-top: 10px;
            }
            #tm-cast-panel .tm-row > * {
                flex: 1;
            }
            #tm-cast-panel input,
            #tm-cast-panel select,
            #tm-cast-panel button {
                width: 100%;
                box-sizing: border-box;
                min-height: 42px;
                border-radius: 13px;
                border: 1px solid #d8e1ea;
                background: #ffffff;
                color: #172033;
                padding: 10px 12px;
            }
            #tm-cast-panel button {
                cursor: pointer;
                font-weight: 700;
                transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease, background 0.16s ease;
            }
            #tm-cast-panel button:hover:not(:disabled) {
                transform: translateY(-1px);
            }
            #tm-cast-panel button:disabled {
                cursor: not-allowed;
                opacity: 0.56;
                box-shadow: none;
            }
            #tm-cast-panel .tm-row button {
                flex: 0 0 auto;
                width: auto;
                min-width: 92px;
                white-space: nowrap;
            }
            #tm-cast-panel button.tm-primary {
                background: linear-gradient(135deg, #f97316, #ea580c);
                color: #fff7ed;
                border-color: transparent;
                box-shadow: 0 14px 24px rgba(249, 115, 22, 0.22);
            }
            #tm-cast-panel button.tm-muted {
                background: #fff7ed;
                color: #9a3412;
                border-color: rgba(251, 146, 60, 0.22);
            }
            #tm-cast-panel .tm-close {
                width: auto;
                min-height: 36px;
                padding: 8px 12px;
                border-radius: 999px;
                background: #fff7ed;
                color: #9a3412;
                border: 1px solid rgba(251, 146, 60, 0.22);
                flex: 0 0 auto;
            }
            #tm-cast-panel .tm-status-card {
                margin-top: 14px;
                padding: 14px 15px;
                border-radius: 18px;
                background: linear-gradient(135deg, rgba(255, 247, 237, 0.98), rgba(255, 251, 235, 0.94));
                border: 1px solid rgba(251, 146, 60, 0.22);
            }
            #tm-cast-panel .tm-status-label {
                color: #9a3412;
                font: 700 12px/1 "Segoe UI", "Microsoft YaHei UI", sans-serif;
                letter-spacing: 0.04em;
            }
            #tm-cast-panel .tm-status {
                margin-top: 8px;
                color: #172033;
                white-space: pre-wrap;
                word-break: break-word;
            }
            #tm-cast-panel .tm-status[data-tone="success"] {
                color: #047857;
            }
            #tm-cast-panel .tm-status[data-tone="error"] {
                color: #b91c1c;
            }
            #tm-cast-panel .tm-status[data-tone="warning"] {
                color: #b45309;
            }
            #tm-cast-panel .tm-subtle {
                color: #64748b;
                font-size: 12px;
                margin-top: 8px;
            }
            #tm-cast-panel .tm-mini-label {
                display: block;
                margin-top: 12px;
                color: #334155;
                font-size: 12px;
                font-weight: 700;
            }
            #tm-cast-panel .tm-actions {
                display: grid;
                grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr);
                gap: 10px;
                margin-top: 16px;
            }
            #tm-cast-panel .tm-actions button {
                min-height: 46px;
            }
            #tm-cast-panel .tm-advanced {
                margin-top: 14px;
                border-radius: 18px;
                border: 1px dashed rgba(148, 163, 184, 0.55);
                background: rgba(248, 250, 252, 0.85);
                padding: 0 14px 14px;
            }
            #tm-cast-panel .tm-advanced summary {
                list-style: none;
                display: flex;
                align-items: center;
                justify-content: space-between;
                cursor: pointer;
                padding: 14px 0 2px;
                color: #334155;
                font-weight: 700;
            }
            #tm-cast-panel .tm-advanced summary::-webkit-details-marker {
                display: none;
            }
            #tm-cast-panel .tm-advanced summary::after {
                content: "+";
                font-size: 18px;
                line-height: 1;
                color: #94a3b8;
            }
            #tm-cast-panel .tm-advanced[open] summary::after {
                content: "-";
            }
            #tm-cast-panel .tm-advanced-body {
                display: grid;
                gap: 12px;
                margin-top: 10px;
            }
            #tm-cast-panel .tm-advanced-item label {
                display: block;
                color: #334155;
                font-weight: 700;
            }
            #tm-cast-panel code {
                font: 12px/1.4 Consolas, "Courier New", monospace;
            }
            @media (max-width: 640px) {
                #tm-cast-toggle {
                    right: 8px;
                    border-radius: 16px;
                    padding: 10px 12px;
                    writing-mode: horizontal-tb;
                    letter-spacing: 0;
                }
                #tm-cast-toggle:hover {
                    transform: translateY(-50%);
                }
                #tm-cast-panel {
                    right: 12px;
                    width: min(360px, calc(100vw - 24px));
                    max-height: calc(100vh - 34px);
                }
                #tm-cast-panel .tm-actions {
                    grid-template-columns: 1fr;
                }
                #tm-cast-panel .tm-row {
                    flex-wrap: wrap;
                }
                #tm-cast-panel .tm-row button {
                    width: 100%;
                }
            }
        `);

        const toggle = document.createElement("button");
        toggle.id = UI_TOGGLE_ID;
        toggle.type = "button";
        toggle.textContent = TEXT.toggle;
        toggle.setAttribute("aria-label", TEXT.title);

        const panel = document.createElement("aside");
        panel.id = UI_PANEL_ID;
        panel.setAttribute("data-open", "false");
        panel.setAttribute("role", "dialog");
        panel.setAttribute("aria-label", TEXT.title);
        panel.innerHTML = `
            <div class="tm-header">
                <div>
                    <div class="tm-kicker">\u7f51\u9875\u89c6\u9891</div>
                    <h2>${TEXT.title}</h2>
                    <p>${TEXT.description}</p>
                </div>
                <button id="tm-cast-close" class="tm-close" type="button">${TEXT.close}</button>
            </div>
            <div class="tm-status-card">
                <div class="tm-status-label">${TEXT.statusLabel}</div>
                <div id="tm-cast-status" class="tm-status" data-tone="neutral">${TEXT.waiting}</div>
            </div>
            <div class="tm-section">
                <div class="tm-section-head">
                    <span class="tm-step">1</span>
                    <div>
                        <div class="tm-section-title">${TEXT.deviceLabel}</div>
                        <div class="tm-section-note">${TEXT.deviceHint}</div>
                    </div>
                </div>
                <div class="tm-row">
                    <select id="tm-cast-device"></select>
                    <button id="tm-cast-scan" class="tm-muted" type="button">${TEXT.scan}</button>
                </div>
            </div>
            <div class="tm-section">
                <div class="tm-section-head">
                    <span class="tm-step">2</span>
                    <div>
                        <div class="tm-section-title">${TEXT.sourceLabel}</div>
                        <div class="tm-section-note">${TEXT.sourceHint}</div>
                    </div>
                </div>
                <div class="tm-row">
                    <select id="tm-cast-source"></select>
                    <button id="tm-cast-refresh" class="tm-muted" type="button">${TEXT.refresh}</button>
                </div>
                <div class="tm-subtle" id="tm-cast-source-hint"></div>
                <label class="tm-mini-label" for="tm-cast-quality">${TEXT.qualityLabel}</label>
                <div class="tm-row">
                    <select id="tm-cast-quality">
                        <option value="max">${TEXT.qualityBest}</option>
                        <option value="1080p">${TEXT.quality1080}</option>
                        <option value="720p">${TEXT.quality720}</option>
                    </select>
                </div>
                <div class="tm-subtle">${TEXT.qualityHint}</div>
                <label class="tm-mini-label" for="tm-cast-mode">${TEXT.modeLabel}</label>
                <div class="tm-row">
                    <select id="tm-cast-mode">
                        <option value="standard">${TEXT.modeStandard}</option>
                        <option value="quality">${TEXT.modeQuality}</option>
                        <option value="smooth">${TEXT.modeSmooth}</option>
                    </select>
                </div>
                <div class="tm-subtle">${TEXT.modeHint}</div>
            </div>
            <div class="tm-actions">
                <button id="tm-cast-start" class="tm-primary" type="button">${TEXT.start}</button>
                <button id="tm-cast-stop" class="tm-muted" type="button">${TEXT.stop}</button>
            </div>
            <details class="tm-advanced">
                <summary>${TEXT.advanced}</summary>
                <div class="tm-advanced-body">
                    <div class="tm-advanced-item">
                        <label>${TEXT.bridgeLabel}</label>
                        <div class="tm-row">
                            <input id="tm-cast-bridge" type="text" />
                            <button id="tm-cast-health" class="tm-muted" type="button">${TEXT.ping}</button>
                        </div>
                        <div class="tm-subtle">${TEXT.bridgeHint}</div>
                    </div>
                    <div class="tm-advanced-item">
                        <label>${TEXT.manualLabel}</label>
                        <div class="tm-row">
                            <input id="tm-cast-manual" type="text" placeholder="${TEXT.manualPlaceholder}" />
                        </div>
                        <div class="tm-subtle">${TEXT.manualHint}</div>
                    </div>
                    <div class="tm-advanced-item">
                        <label>${TEXT.debugLabel}</label>
                        <div class="tm-row">
                            <button id="tm-cast-export-debug" class="tm-muted" type="button">${TEXT.debugExport}</button>
                        </div>
                        <div class="tm-subtle">${TEXT.debugHint}</div>
                    </div>
                </div>
            </details>
        `;

        document.body.appendChild(toggle);
        document.body.appendChild(panel);

        elements = {
            toggle,
            panel,
            bridgeInput: panel.querySelector("#tm-cast-bridge"),
            deviceSelect: panel.querySelector("#tm-cast-device"),
            sourceSelect: panel.querySelector("#tm-cast-source"),
            qualitySelect: panel.querySelector("#tm-cast-quality"),
            modeSelect: panel.querySelector("#tm-cast-mode"),
            sourceHint: panel.querySelector("#tm-cast-source-hint"),
            manualInput: panel.querySelector("#tm-cast-manual"),
            status: panel.querySelector("#tm-cast-status"),
            closeButton: panel.querySelector("#tm-cast-close"),
            healthButton: panel.querySelector("#tm-cast-health"),
            scanButton: panel.querySelector("#tm-cast-scan"),
            refreshButton: panel.querySelector("#tm-cast-refresh"),
            startButton: panel.querySelector("#tm-cast-start"),
            stopButton: panel.querySelector("#tm-cast-stop"),
            exportDebugButton: panel.querySelector("#tm-cast-export-debug"),
        };

        elements.bridgeInput.value = STATE.bridgeBaseUrl;
        elements.qualitySelect.value = STATE.qualityPreference;
        elements.modeSelect.value = STATE.transcodeProfile;
        renderDeviceOptions("");
        collectCandidates();
        updateActionState();
        setPanelOpen(false);

        toggle.addEventListener("click", () => {
            const opened = panel.getAttribute("data-open") === "true";
            setPanelOpen(!opened);
        });
        elements.closeButton.addEventListener("click", () => setPanelOpen(false));
        elements.healthButton.addEventListener("click", checkBridge);
        elements.scanButton.addEventListener("click", scanDevices);
        elements.refreshButton.addEventListener("click", collectCandidates);
        elements.startButton.addEventListener("click", castSelectedSource);
        elements.stopButton.addEventListener("click", stopCasting);
        elements.exportDebugButton.addEventListener("click", exportDebugData);
        elements.bridgeInput.addEventListener("change", () => rememberBridgeUrl(elements.bridgeInput.value));
        elements.qualitySelect.addEventListener("change", () => rememberQualityPreference(elements.qualitySelect.value));
        elements.modeSelect.addEventListener("change", () => rememberTranscodeProfile(elements.modeSelect.value));
        elements.deviceSelect.addEventListener("change", updateActionState);
        elements.sourceSelect.addEventListener("change", updateActionState);
        elements.manualInput.addEventListener("input", updateActionState);

        checkBridge();
    }

    function installObserver() {
        if (STATE.observer || !document.documentElement) {
            return;
        }
        STATE.observer = new MutationObserver(() => {
            scheduleCollectCandidates();
            if (!STATE.uiReady && document.body) {
                createUi();
            }
        });
        STATE.observer.observe(document.documentElement, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ["src"],
        });
    }

    if (!isTopLevelPage()) {
        return;
    }

    installRequestHooks();
    installObserver();
    document.addEventListener("DOMContentLoaded", createUi, { once: true });
    window.addEventListener("load", scheduleCollectCandidates, { once: true });
})();
