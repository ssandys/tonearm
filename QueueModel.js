// Pure presentation for the pushed daemon snapshot. No backend or row actions.
function available(state) {
    return !!(state && state.queue && state.status === "ok" && state.selection
              && state.selection.availability === "available" && state.zone
              && state.zone.id === state.selection.zone_id
              && state.queue.zone_id === state.zone.id)
}
function header(state) {
    var time = available(state) ? state.zone.queue_time_remaining : null
    if (typeof time !== "number" || !isFinite(time) || time < 0 || time > 2147483648)
        return "Queue"
    var seconds = Math.floor(time)
    var minutes = Math.floor(seconds / 60)
    return "Queue (" + minutes + ":" + (seconds % 60 < 10 ? "0" : "") + seconds % 60 + ")"
}
function status(state) {
    if (!state || !state.queue) return "unavailable"
    if (state.status !== "ok") return "disconnected"
    if (state.queue.status === "disconnected") return "disconnected"
    if (!state.selection || state.selection.availability === "unknown") return "unknown"
    if (state.selection.availability !== "available") return "unavailable"
    if (!available(state)) return "unknown"
    return ["loading", "ready", "error", "unknown", "unavailable"].indexOf(state.queue.status) >= 0
            ? state.queue.status : "error"
}
function rows(state) {
    return status(state) === "ready" && Array.isArray(state.queue.items)
            ? state.queue.items.slice(0, 100) : []
}
function message(state) {
    var word = status(state)
    if (word === "ready") return rows(state).length ? "" : "Queue is empty"
    if (word === "loading") return "Loading queue…"
    if (word === "unknown") return "Waiting for the selected zone"
    if (word === "disconnected") return "Queue disconnected"
    if (word === "error") return "Queue unavailable after a subscription error"
    return "Queue view unavailable"
}
function cursor(current, delta, count) {
    return count ? Math.max(0, Math.min(count - 1, current + delta)) : -1
}

if (typeof module !== "undefined") module.exports = {header, status, rows, message, cursor}
