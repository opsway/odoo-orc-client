/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useBus, useService } from "@web/core/utils/hooks";
import { session } from "@web/session";
import { OrcTaskListPopover } from "./orc_task_list_popover";
import { computeIsUnread, ORC_CHAT_UPDATE } from "./orc_chat_service";

const { Component } = owl;
const { useState } = owl.hooks;

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
    setup() {
        this.orcChat = useService("orc_chat");
        // Owl 1 — bump tick on every service update so the badge re-renders
        // when tasks / lastViewed mutate.
        this._render = useState({ tick: 0 });
        useBus(this.orcChat.bus, ORC_CHAT_UPDATE, () => {
            this._render.tick++;
        });
        this.ui = useState({ dropdownOpen: false });
        this._onDocClick = this._onDocClick.bind(this);
    }

    get enabled() {
        return Boolean(session.orc_enabled);
    }

    get unreadCount() {
        let n = 0;
        for (const t of this.orcChat.state.tasks) {
            if (computeIsUnread(t, this.orcChat.state.lastViewed)) n++;
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
            // view). Fire-and-forget — the service notifies via its bus.
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
OrcSystrayWithTasks.template = "orc_client_tasks.OrcSystrayWithTasks";
OrcSystrayWithTasks.components = { OrcTaskListPopover };

// `force: true` replaces the entry registered by orc_client_provisioning
// under the same key. Same sequence (100) so the icon position in the
// systray doesn't shift when the tasks addon is installed/uninstalled.
// `isDisplayed` matches the provisioning entry's gate so non-enrolled
// users see no icon at all (vs. an empty button shell).
registry.category("systray").add(
    "orc_client_provisioning.OrcSystray",
    {
        Component: OrcSystrayWithTasks,
        isDisplayed: () => Boolean(session.orc_enabled),
    },
    { force: true, sequence: 100 },
);
