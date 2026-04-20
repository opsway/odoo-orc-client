/** @odoo-module */

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { session } from "@web/session";

/**
 * ORC systray icon. Visible iff the current user has orc_enabled=True
 * (primed server-side via ir.http.session_info override).
 *
 * Clicking opens /orc/sso/start in a new tab; the server mints a
 * one-time nonce and returns an auto-submitting form that signs the
 * user in to ORC with no second password prompt.
 */
export class OrcSystray extends Component {
    static template = "orc_client_provisioning.OrcSystray";
    static props = {};

    get isEnabled() {
        return Boolean(session.orc_enabled);
    }

    onClick() {
        window.open("/orc/sso/start", "_blank", "noopener");
    }
}

export const orcSystrayItem = {
    Component: OrcSystray,
};

registry
    .category("systray")
    .add("orc_client_provisioning.OrcSystray", orcSystrayItem, { sequence: 100 });
