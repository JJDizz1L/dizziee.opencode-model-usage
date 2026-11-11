import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: root
    visible: false

    property string providerId: "opencode"
    property string providerName: "OpenCode"
    property bool ready: false
    property bool refreshing: false
    property string usageStatusText: ""
    property string authHelpText: ""

    // A failed scan keeps the last good numbers on screen and says so. The
    // retry timer in Main.qml asks again sooner than the normal interval.
    // The raw error text stays data; the widget supplies the localized
    // framing around it.
    property bool stale: false
    property bool retryAdvised: false
    property bool scanFailed: false
    property string scanErrorKind: ""
    property string errorText: ""
    property double scannedAtMs: 0

    property int todaySessions: 0
    property int todayTotalTokens: 0
    property var todayTokensByModel: ({})

    property var recentDays: []
    property int totalSessions: 0
    property var modelUsage: ({})

    // Online Go-plan meters merged by the scanner from the packaged agents
    // collector's secret-free record. The plugin never sees credentials:
    // only these display fields reach the cache.
    property var limits: []
    property string tierLabel: ""
    property bool limitsStale: false
    property double limitsFetchedAtMs: 0

    property bool hasLocalStats: true

    property string dbPath: "~/.local/share/opencode/opencode.db"

    readonly property string scannerScriptPath: pathFromUrl(Qt.resolvedUrl("../scripts/opencode_usage_scanner.py"))

    // The cache is only the aggregate snapshot the panel draws (a few KB),
    // never messages or a copy of the database. It paints the bar instantly
    // at shell start, and every bar instance watches it so one scan reaches
    // every monitor. The cap is a hard ceiling, not an expected size.
    readonly property int cacheMaxBytes: 131072

    property string lastCacheText: ""
    property bool pendingRefresh: false

    readonly property string cachePath: {
        var base = Quickshell.env("XDG_CACHE_HOME")
        if (!base) base = (Quickshell.env("HOME") ?? "/home") + "/.cache"
        return base + "/omarchy/dizziee.opencode-model-usage.json"
    }

    function pathFromUrl(url) {
        var value = String(url || "")
        if (value.indexOf("file://") === 0)
            return decodeURIComponent(value.substring(7))
        return value
    }

    function resolvePath(p) {
        if (p && p.startsWith("~"))
            return (Quickshell.env("HOME") ?? "/home") + p.substring(1)
        return p
    }

    Process {
        id: scanner
        running: false
        command: []

        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: root.applyUsage(text)
        }

        stderr: StdioCollector {
            waitForEnd: true
            onStreamFinished: function(text) {
                if (text && text.trim() !== "")
                    console.warn("model-usage/opencode", text.trim())
            }
        }

        onExited: {
            root.refreshing = false
            if (root.pendingLimitsRefresh) {
                root.pendingLimitsRefresh = false
                root.refreshLimits(true)
            } else if (root.pendingRefresh) {
                root.pendingRefresh = false
                root.refresh(true)
            }
        }
    }

    FileView {
        id: usageCacheFile
        path: root.cachePath
        watchChanges: true
        atomicWrites: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: {
            var content = String(text() || "")
            if (content.length > root.cacheMaxBytes) {
                root.fail("", false, "cache")
                return
            }
            root.lastCacheText = content
            root.applyUsage(content)
        }
    }

    function fail(message, retry, kind) {
        var text = String(message || "")
        if (root.errorText !== text)
            console.warn("model-usage/opencode", text)
        root.errorText = text
        root.scanFailed = true
        root.scanErrorKind = String(kind || "read")
        root.usageStatusText = ""
        root.authHelpText = ""
        root.stale = root.ready
        root.retryAdvised = retry !== false
    }

    function applyUsage(content) {
        var text = String(content || "")
        if (text.length > root.cacheMaxBytes) {
            root.fail("", false, "cache")
            return
        }

        try {
            var data = JSON.parse(text || "{}")
            if (!data || typeof data !== "object")
                throw new Error("empty record")

            if (data.ready !== true) {
                root.fail(String(data.error || ""), data.retryAdvised === true, "read")
                return
            }

            // Only a successful scan writes the cache; loading it is a read.
            // The text guard also makes a write->load echo a no-op.
            if (text !== "" && text !== root.lastCacheText) {
                root.lastCacheText = text
                try { usageCacheFile.setText(text) } catch (e) {}
            }

            root.ready = true
            root.stale = false
            root.scanFailed = false
            root.scanErrorKind = ""
            root.retryAdvised = false
            root.errorText = ""
            root.hasLocalStats = data.hasLocalStats !== false
            root.scannedAtMs = Number(data.scannedAt || 0) * 1000
            root.todaySessions = Math.max(0, Number(data.todaySessions || 0))
            root.todayTotalTokens = Math.max(0, Number(data.todayTotalTokens || 0))
            root.todayTokensByModel = data.todayTokensByModel || ({})
            root.recentDays = data.recentDays || []
            root.modelUsage = data.modelUsage || ({})
            root.totalSessions = Math.max(0, Number(data.totalSessions || 0))
            // Online meters: validated display data only, no credentials.
            var cleanLimits = []
            if (data.limits && data.limits.length !== undefined) {
                for (var i = 0; i < Math.min(data.limits.length, 10); i++) {
                    var w = data.limits[i] || {}
                    var pct = Number(w.percent)
                    if (!(pct >= 0 && pct <= 1)) continue
                    var label = String(w.label || "").slice(0, 64)
                    if (label === "") continue
                    cleanLimits.push({
                        label: label,
                        percent: pct,
                        resetsAt: String(w.resetsAt || "").slice(0, 64)
                    })
                }
            }
            root.limits = cleanLimits
            root.tierLabel = String(data.tierLabel || "").slice(0, 32)
            root.limitsStale = data.limitsStale === true && cleanLimits.length > 0
            root.limitsFetchedAtMs = Math.max(0, Number(data.limitsFetchedAt || 0))
            root.usageStatusText = String(data.usageStatusText || "").slice(0, 256)
            root.authHelpText = String(data.authHelpText || "").slice(0, 512)
        } catch (e) {
            root.fail(String(e), true, "parse")
        }
    }

    function isFresh(maxAgeSec) {
        return root.ready && root.scannedAtMs > 0
            && (Date.now() - root.scannedAtMs) < Math.max(0, Number(maxAgeSec || 0)) * 1000
    }

    // The online meters ride the packaged collector's own cache, so they
    // refresh on a faster lane than the sqlite scan. limitsFetchedAt arrives
    // in milliseconds (unlike scannedAt, which is seconds).
    function isLimitsFresh(maxAgeSec) {
        return root.limitsFetchedAtMs > 0
            && (Date.now() - root.limitsFetchedAtMs) < Math.max(0, Number(maxAgeSec || 0)) * 1000
    }

    property bool pendingLimitsRefresh: false

    function refreshLimits(force, maxAgeSec) {
        if (scanner.running) {
            root.pendingLimitsRefresh = true
            return
        }
        if (force !== true && maxAgeSec !== undefined && root.isLimitsFresh(maxAgeSec))
            return

        root.refreshing = true
        pendingLimitsRefresh = false
        scanner.command = ["/usr/bin/python3", root.scannerScriptPath,
            root.resolvePath(root.dbPath), "--limits-only", "--cache", root.cachePath]
        scanner.running = true
    }

    function refreshLimitsIfStale(maxAgeSec) {
        root.refreshLimits(false, maxAgeSec)
    }

    function refresh(force, maxAgeSec) {
        if (scanner.running) {
            root.pendingRefresh = true
            return
        }
        if (force !== true && maxAgeSec !== undefined && root.isFresh(maxAgeSec))
            return

        root.refreshing = true
        scanner.command = ["/usr/bin/python3", root.scannerScriptPath, root.resolvePath(root.dbPath)]
        scanner.running = true
    }

    function refreshIfStale(maxAgeSec) {
        root.refresh(false, maxAgeSec)
    }
}
