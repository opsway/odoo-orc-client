/** @odoo-module **/

import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { session } from "@web/session";

const { EventBus } = owl.core;

/**
 * Shared state + actions for the AI Workplace chat dock.
 *
 * Owns the task list (polled from /orc/tasks/list), the set of open
 * chat windows, and a last-viewed timestamp per room (persisted to
 * localStorage so fresh page loads keep the dock state and the
 * unread dots stay accurate).
 *
 * Components access via `useService("orc_chat")`. Owl 1 (Odoo v15) has
 * no `reactive()` primitive, so the service keeps plain state and
 * publishes an EventBus tick after every mutation. Components subscribe
 * with `useBus(orcChat.bus, "update", ...)` and bump their local
 * `useState` to trigger a re-render — getters read fresh values from
 * `orcChat.state` at template time.
 *
 * Deliberately not tied to Odoo's RPC layer — our controllers are
 * plain HTTP+JSON, not JSON-RPC.
 */

// localStorage is scoped to origin, which is the Odoo host — fine, the
// dock is per-Odoo-instance. Keys are stable and readable in DevTools.
const LS_OPEN_KEY = "orc_chat:open_windows";
const LS_LAST_VIEWED_KEY = "orc_chat:last_viewed";

// Max chat windows rendered side-by-side in the dock. Anything beyond
// is available from the systray popover; we don't show a "+N more"
// dock overflow in Phase 2a — YAGNI until users ask.
export const MAX_VISIBLE_WINDOWS = 3;

// Background poll cadence for the task list. The systray unread
// badge lags by at most this long; the shared-SSE rework in Phase 2b
// replaces this with push.
const POLL_MS = 60_000;

// Event name on the service's bus. Components subscribe with
// `useBus(orcChat.bus, ORC_CHAT_UPDATE, ...)`.
export const ORC_CHAT_UPDATE = "orc_chat:update";

function readJson(key, fallback) {
    try {
        const raw = window.localStorage.getItem(key);
        if (!raw) return fallback;
        const parsed = JSON.parse(raw);
        return parsed ?? fallback;
    } catch {
        return fallback;
    }
}

function writeJson(key, value) {
    try {
        window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
        // Quota exceeded or privacy-mode disables localStorage; dock
        // still works, just won't persist across reloads.
    }
}

function parseIso(s) {
    if (!s) return 0;
    const t = Date.parse(s);
    return Number.isFinite(t) ? t : 0;
}

/** Standalone unread predicate — takes the state fields explicitly so
 *  callers who read them through their own subscription stay reactive
 *  without depending on closure capture of the service's state object. */
export function computeIsUnread(task, lastViewed) {
    const activity = parseIso(task.last_activity);
    if (!activity) return false;
    const viewed = parseIso(lastViewed?.[task.room_id]);
    return activity > viewed;
}

const orcChatService = {
    dependencies: ["notification"],

    start(env, { notification }) {
        // `state` is read by components through `orcChat.state`. After
        // every mutation we `bus.trigger(ORC_CHAT_UPDATE)`; subscribers
        // bump a local `useState` to re-render. Owl 1 has no reactive()
        // proxy so this is the established v15 pattern.
        const state = {
            tasks: [],                                           // [{room_id, name, last_activity, ...}]
            openWindows: readJson(LS_OPEN_KEY, []),              // [{room_id, folded}]
            lastViewed: readJson(LS_LAST_VIEWED_KEY, {}),        // {room_id: isoTimestamp}
            loading: false,
            lastError: null,
        };
        const bus = new EventBus();
        const notify = () => bus.trigger(ORC_CHAT_UPDATE);

        let pollHandle = null;

        // Callers are expected to be gated by session.orc_enabled — if
        // the user isn't enrolled we skip the fetch entirely (would 403).
        async function refreshTasks() {
            if (!session.orc_enabled) return;
            state.loading = true;
            notify();
            try {
                const res = await fetch("/orc/tasks/list", {
                    credentials: "same-origin",
                });
                const data = await res.json();
                if (data.ok) {
                    state.tasks = Array.isArray(data.tasks) ? data.tasks : [];
                    state.lastError = null;
                } else {
                    state.lastError = data.error || "Unknown error";
                }
            } catch (err) {
                state.lastError = String(err);
            } finally {
                state.loading = false;
                notify();
            }
        }

        function startPolling() {
            if (pollHandle !== null) return;
            refreshTasks();
            pollHandle = window.setInterval(refreshTasks, POLL_MS);
        }

        function stopPolling() {
            if (pollHandle === null) return;
            window.clearInterval(pollHandle);
            pollHandle = null;
        }

        // Opening a room: either unfold an existing window or add one.
        // Always marks the window as viewed right away — the user is
        // about to look at it.
        function openTask(roomId) {
            const existing = state.openWindows.find((w) => w.room_id === roomId);
            if (existing) {
                existing.folded = false;
            } else {
                state.openWindows = [
                    ...state.openWindows,
                    { room_id: roomId, folded: false },
                ];
            }
            markViewed(roomId);
            writeJson(LS_OPEN_KEY, state.openWindows);
            notify();
        }

        function closeWindow(roomId) {
            state.openWindows = state.openWindows.filter((w) => w.room_id !== roomId);
            writeJson(LS_OPEN_KEY, state.openWindows);
            notify();
        }

        function toggleFold(roomId) {
            const w = state.openWindows.find((x) => x.room_id === roomId);
            if (!w) return;
            w.folded = !w.folded;
            // Unfolding counts as "looking at it again".
            if (!w.folded) markViewed(roomId);
            writeJson(LS_OPEN_KEY, state.openWindows);
            notify();
        }

        function markViewed(roomId) {
            state.lastViewed = {
                ...state.lastViewed,
                [roomId]: new Date().toISOString(),
            };
            writeJson(LS_LAST_VIEWED_KEY, state.lastViewed);
            notify();
        }

        // Unread heuristic: task.last_activity > lastViewed[room_id].
        // Rooms never viewed count as unread if they have any activity.
        function isUnread(task) {
            const activity = parseIso(task.last_activity);
            if (!activity) return false;
            const viewed = parseIso(state.lastViewed[task.room_id]);
            return activity > viewed;
        }

        function unreadCount() {
            return state.tasks.reduce((n, t) => (isUnread(t) ? n + 1 : n), 0);
        }

        // Ask the Odoo backend to mint a one-time AI Workplace SSO nonce for
        // this task. Returns {url, nonce} — the caller (chat window)
        // form-POSTs these into its iframe.
        async function openHandshake(roomId) {
            const res = await fetch("/orc/tasks/open", {
                method: "POST",
                credentials: "same-origin",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ room_id: roomId }),
            });
            const data = await res.json();
            if (!data.ok) {
                notification.add(data.error || _t("Failed to open AI Workplace chat"), {
                    type: "warning",
                });
                throw new Error(data.error || "handshake failed");
            }
            return { url: data.url, nonce: data.nonce };
        }

        // Create a fresh task and open a window on it. Returns room_id
        // on success, throws on failure (caller decides what to show).
        async function createTask({ message, infrastructure_id = null }) {
            const body = { message };
            if (infrastructure_id) body.infrastructure_id = infrastructure_id;
            const res = await fetch("/orc/tasks/create", {
                method: "POST",
                credentials: "same-origin",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!data.ok || !data.room_id) {
                notification.add(data.error || _t("Failed to create AI Workplace task"), {
                    type: "danger",
                });
                throw new Error(data.error || "create failed");
            }
            // Give the new task a stub row in our local list so the
            // dock + systray can render it before the next poll lands.
            if (!state.tasks.some((t) => t.room_id === data.room_id)) {
                state.tasks = [
                    {
                        room_id: data.room_id,
                        name: null,
                        status: "active",
                        last_activity: new Date().toISOString(),
                    },
                    ...state.tasks,
                ];
            }
            openTask(data.room_id);
            // Re-fetch soon after to pick up the authoritative row
            // (infrastructure_name, org_name, …).
            window.setTimeout(refreshTasks, 2000);
            return data.room_id;
        }

        if (session.orc_enabled) {
            startPolling();
            window.addEventListener("beforeunload", stopPolling);
        }

        return {
            state,
            bus,
            // queries
            isUnread,
            unreadCount,
            // mutations
            openTask,
            closeWindow,
            toggleFold,
            markViewed,
            // backend
            refreshTasks,
            openHandshake,
            createTask,
        };
    },
};

registry.category("services").add("orc_chat", orcChatService);
