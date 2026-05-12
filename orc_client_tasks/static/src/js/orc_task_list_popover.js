/** @odoo-module **/

import { useBus, useService } from "@web/core/utils/hooks";
import { computeIsUnread, ORC_CHAT_UPDATE } from "./orc_chat_service";

const { Component } = owl;
const { useState } = owl.hooks;

/**
 * List of the user's AI Workplace tasks, grouped by status. Rendered inside
 * the systray's dropdown. Clicking a row opens a chat window via
 * the shared orc_chat service.
 *
 * Composing a new task is one click: the "+" button creates an
 * empty room server-side and immediately opens a chat window on
 * it. The user types their first message inside the chat iframe's
 * composer — same place every follow-up message goes. The AI Workplace
 * server already supports the no-first-message creation path; see
 * `services/orc_client_tasks_ext.py::create_task`.
 *
 * Infrastructure is always the configured default in Phase 2a —
 * a picker is Phase 2c territory.
 */
export class OrcTaskListPopover extends Component {
    setup() {
        this.orcChat = useService("orc_chat");
        this.notification = useService("notification");
        // Owl 1 — bump tick on every service update so the tasks getter
        // re-runs and the unread predicate re-evaluates against fresh
        // `orcChat.state.lastViewed`.
        this._render = useState({ tick: 0 });
        useBus(this.orcChat.bus, ORC_CHAT_UPDATE, () => {
            this._render.tick++;
        });
        // Local-only component state: in-flight guard so a
        // double-click on "+" doesn't spawn two empty rooms.
        this.ui = useState({
            creatingTask: false,
        });
    }

    get tasks() {
        // Active first, then closed. Inside each group: most-recently-
        // active at the top.
        const items = [...this.orcChat.state.tasks];
        items.sort((a, b) => {
            const sa = (a.status === "closed") ? 1 : 0;
            const sb = (b.status === "closed") ? 1 : 0;
            if (sa !== sb) return sa - sb;
            return (b.last_activity || "").localeCompare(a.last_activity || "");
        });
        return items;
    }

    taskLabel(task) {
        if (task.name) return task.name;
        // Fallback: first 24 chars of the room id, with the ! stripped.
        const raw = String(task.room_id || "").replace(/^!/, "");
        return raw.slice(0, 24) + (raw.length > 24 ? "…" : "");
    }

    taskSubtitle(task) {
        const parts = [];
        if (task.infrastructure_name) parts.push(task.infrastructure_name);
        if (task.last_activity) {
            try {
                parts.push(new Date(task.last_activity).toLocaleString());
            } catch {
                /* ignore */
            }
        }
        return parts.join(" · ");
    }

    isUnread(task) {
        return computeIsUnread(task, this.orcChat.state.lastViewed);
    }

    onClickTask(task) {
        this.orcChat.openTask(task.room_id);
        if (this.props.onPicked) this.props.onPicked();
    }

    onClickOpenInOrc() {
        // Phase-1 fallback: jump to the full AI Workplace dashboard in a new tab,
        // signed in via the same SSO start endpoint orc_client_provisioning ships.
        window.open("/orc/sso/start", "_blank", "noopener");
        if (this.props.onPicked) this.props.onPicked();
    }

    async onClickNewTask() {
        // Single-click flow: create the room with no first message,
        // immediately open a chat window on it (the service's
        // `createTask` already calls `openTask` on success), close
        // the popover. The user types their first message inside
        // the chat iframe.
        if (this.ui.creatingTask) return;
        this.ui.creatingTask = true;
        try {
            await this.orcChat.createTask({});
            if (this.props.onPicked) this.props.onPicked();
        } catch {
            // notification already surfaced by the service
        } finally {
            this.ui.creatingTask = false;
        }
    }
}
OrcTaskListPopover.template = "orc_client_tasks.OrcTaskListPopover";
