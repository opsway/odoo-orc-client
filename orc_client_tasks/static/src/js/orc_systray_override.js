/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { session } from "@web/session";
import { OrcTaskListPopover } from "./orc_task_list_popover";
import { computeIsUnread } from "./orc_chat_service";

/**
 * Replaces orc_client_provisioning's single-action "Open AI Workplace" systray
 * button with a task-list popover. The green icon stays — click now
 * opens a popover over the AI Workplace tasks for this user; a small "Open in
 * AI Workplace" action inside the popover still offers the Phase-1 new-tab
 * jump into the full dashboard.
 *
 * We do this via a force-override on the same registry key so the
 * orc_client_tasks module swaps behavior without shipping a second
 * icon. If orc_client_tasks isn't installed, the Phase-1 icon keeps
 * its original behavior.
 */
export class OrcSystrayWithTasks extends Component {
    static template = "orc_client_tasks.OrcSystrayWithTasks";
    static props = {};
    static components = { OrcTaskListPopover };

    setup() {
        this.orcChat = useService("orc_chat");
        // See orc_chat_dock.js — useState() subscribes this component
        // to the shared service reactive so the badge count updates
        // when tasks / last-viewed mutate.
        this.state = useState(this.orcChat.state);
        this.ui = useState({ dropdownOpen: false });
        this._onDocClick = this._onDocClick.bind(this);
    }

    get enabled() {
        return Boolean(session.orc_enabled);
    }

    get unreadCount() {
        // Reads go through `this.state` so the badge re-renders when
        // tasks/lastViewed mutate — the service's own isUnread() closes
        // over its original reactive and wouldn't notify this component.
        let n = 0;
        for (const t of this.state.tasks) {
            if (computeIsUnread(t, this.state.lastViewed)) n++;
        }
        return n;
    }

    get badgeLabel() {
        const n = this.unreadCount;
        if (n <= 0) return "";
        return n > 99 ? "99+" : String(n);
    }

    onToggleDropdown(ev) {
        if (ev) ev.stopPropagation();
        this.ui.dropdownOpen = !this.ui.dropdownOpen;
        if (this.ui.dropdownOpen) {
            // Refresh on open so the list is never stale (poll is 60s,
            // user clicking the icon is a strong cue they want a fresh
            // view). Fire-and-forget — the service updates reactive state.
            this.orcChat.refreshTasks();
            window.requestAnimationFrame(() => {
                document.addEventListener("click", this._onDocClick);
            });
        } else {
            document.removeEventListener("click", this._onDocClick);
        }
    }

    _onDocClick(ev) {
        if (!ev.target.closest(".o-orc-systray")) {
            this.ui.dropdownOpen = false;
            document.removeEventListener("click", this._onDocClick);
        }
    }

    onTaskPicked() {
        this.ui.dropdownOpen = false;
        document.removeEventListener("click", this._onDocClick);
    }
}

// `force: true` replaces the entry registered by orc_client_provisioning
// under the same key. Same sequence (100) so the icon position in the
// systray doesn't shift when the tasks addon is installed/uninstalled.
registry.category("systray").add(
    "orc_client_provisioning.OrcSystray",
    { Component: OrcSystrayWithTasks },
    { force: true, sequence: 100 },
);
