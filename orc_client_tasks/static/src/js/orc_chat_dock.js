/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useBus, useService } from "@web/core/utils/hooks";
import { session } from "@web/session";
import { OrcChatWindow } from "./orc_chat_window";
import { MAX_VISIBLE_WINDOWS, ORC_CHAT_UPDATE } from "./orc_chat_service";

const { Component } = owl;
const { useState } = owl.hooks;

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
    setup() {
        this.orcChat = useService("orc_chat");
        // Owl 1 has no `reactive()`; bumping `_tick` on every service
        // update is what wires re-renders. Getters read fresh values
        // from `this.orcChat.state` at template time.
        this._render = useState({ tick: 0 });
        useBus(this.orcChat.bus, ORC_CHAT_UPDATE, () => {
            this._render.tick++;
        });
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
OrcChatDock.template = "orc_client_tasks.OrcChatDock";
OrcChatDock.components = { OrcChatWindow };

registry.category("main_components").add("orc_client_tasks.OrcChatDock", {
    Component: OrcChatDock,
});
