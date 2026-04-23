/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { session } from "@web/session";
import { OrcTaskListPopover } from "./orc_task_list_popover";

/**
 * Systray entry: an "ORC Chats" button with an unread badge, opening
 * a dropdown populated with the user's tasks.
 *
 * Visible iff session.orc_enabled is True — same primed-server-side
 * gate as the Phase 1 systray. Everyone else sees nothing so the
 * shipped addon costs zero pixels for non-enrolled users.
 */
export class OrcChatSystray extends Component {
    static template = "orc_client_tasks.OrcChatSystray";
    static props = {};
    static components = { OrcTaskListPopover };

    setup() {
        this.orcChat = useService("orc_chat");
        // Local UI state — dropdown open/closed. Dropdown itself is a
        // plain DOM construct so we don't depend on Odoo's internal
        // Dropdown widget (whose API shifts between 18.x point releases).
        this.ui = useState({ dropdownOpen: false });
        this._onDocClick = this._onDocClick.bind(this);
    }

    get enabled() {
        return Boolean(session.orc_enabled);
    }

    get unreadCount() {
        return this.orcChat.unreadCount();
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
            // A single document-level listener closes the dropdown on
            // outside-click. Added lazily so we don't hold references
            // when the dropdown isn't shown.
            window.requestAnimationFrame(() => {
                document.addEventListener("click", this._onDocClick);
            });
        } else {
            document.removeEventListener("click", this._onDocClick);
        }
    }

    _onDocClick(ev) {
        // Close on any click outside the systray entry. Works for all
        // systray instances on the page — if the user happened to open
        // two we'd close both, which is fine (there's only one).
        if (!ev.target.closest(".o_orc_chat_systray")) {
            this.ui.dropdownOpen = false;
            document.removeEventListener("click", this._onDocClick);
        }
    }

    onTaskPicked() {
        this.ui.dropdownOpen = false;
        document.removeEventListener("click", this._onDocClick);
    }
}

export const orcChatSystrayItem = {
    Component: OrcChatSystray,
};

registry
    .category("systray")
    .add("orc_client_tasks.OrcChatSystray", orcChatSystrayItem, { sequence: 95 });
