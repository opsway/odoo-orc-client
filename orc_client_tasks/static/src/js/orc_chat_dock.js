/** @odoo-module **/

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { session } from "@web/session";
import { OrcChatWindow } from "./orc_chat_window";
import { MAX_VISIBLE_WINDOWS } from "./orc_chat_service";

/**
 * Page-level dock holding the open chat windows.
 *
 * Mounted once via the `main_components` registry so it's independent
 * of whatever route the user is on — navigating between modules keeps
 * the dock (and its iframes) alive.
 *
 * Windows stack bottom-right, newest on the right. We render at most
 * MAX_VISIBLE_WINDOWS; any overflow past the cap stays in the service
 * state (restore on next page load, accessible from the systray
 * popover) but is invisible in the dock. Phase 2a UX decision: we
 * chose not to add a "+N more" pill on the dock itself since the
 * popover already surfaces all tasks.
 */
export class OrcChatDock extends Component {
    static template = "orc_client_tasks.OrcChatDock";
    static props = {};
    static components = { OrcChatWindow };

    setup() {
        this.orcChat = useService("orc_chat");
    }

    get enabled() {
        return Boolean(session.orc_enabled);
    }

    get visibleWindows() {
        // Most-recently-opened on the right. openTask appends to the
        // tail, so the natural order already does the right thing;
        // we just trim to the cap.
        const all = this.orcChat.state.openWindows;
        if (all.length <= MAX_VISIBLE_WINDOWS) return all;
        return all.slice(all.length - MAX_VISIBLE_WINDOWS);
    }
}

registry.category("main_components").add("orc_client_tasks.OrcChatDock", {
    Component: OrcChatDock,
});
