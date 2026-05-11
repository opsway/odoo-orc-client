/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { computeIsUnread } from "./orc_chat_service";

/**
 * List of the user's ORC tasks, grouped by status. Rendered inside
 * the systray's dropdown. Clicking a row opens a chat window via
 * the shared orc_chat service.
 *
 * Composing a new task is one click: the "+" button creates an
 * empty room server-side and immediately opens a chat window on
 * it. The user types their first message inside the chat iframe's
 * composer — same place every follow-up message goes. The ORC
 * server already supports the no-first-message creation path; see
 * `services/orc_client_tasks_ext.py::create_task`.
 *
 * Infrastructure is always the configured default in Phase 2a —
 * a picker is Phase 2c territory.
 */
export class OrcTaskListPopover extends Component {
    static template = "orc_client_tasks.OrcTaskListPopover";
    static props = {
        /** Called after a task is picked so the parent can close the dropdown. */
        onPicked: { type: Function, optional: true },
    };

    setup() {
        this.orcChat = useService("orc_chat");
        this.notification = useService("notification");
        // See orc_chat_dock.js — useState() on the service reactive is
        // what actually subscribes this component to re-renders when
        // the shared state (tasks list, unread markers) mutates.
        this.state = useState(this.orcChat.state);
        // Local-only component state: in-flight guard so a
        // double-click on "+" doesn't spawn two empty rooms.
        this.ui = useState({
            creatingTask: false,
        });
    }

    get tasks() {
        // Active first, then closed. Inside each group: most-recently-
        // active at the top.
        const items = [...this.state.tasks];
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
        // Read lastViewed via this.state (useState-subscribed) so dots
        // update without a service-call indirection bypassing the
        // component's reactive observer.
        return computeIsUnread(task, this.state.lastViewed);
    }

    onClickTask(task) {
        this.orcChat.openTask(task.room_id);
        if (this.props.onPicked) this.props.onPicked();
    }

    onClickOpenInOrc() {
        // Phase-1 fallback: jump to the full ORC dashboard in a new tab,
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
