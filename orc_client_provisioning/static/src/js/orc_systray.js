/** @odoo-module **/

import { registry } from "@web/core/registry";
import { session } from "@web/session";

const { Component } = owl;

/**
 * AI Workplace systray icon. Visible iff the current user has orc_enabled=True
 * (primed server-side via ir.http.session_info override).
 *
 * Clicking opens /orc/sso/start in a new tab; the server mints a
 * one-time nonce and returns an auto-submitting form that signs the
 * user in to AI Workplace with no second password prompt.
 */
export class OrcSystray extends Component {
    onClick() {
        window.open("/orc/sso/start", "_blank", "noopener");
    }
}
OrcSystray.template = "orc_client_provisioning.OrcSystray";

export const orcSystrayItem = {
    Component: OrcSystray,
    isDisplayed: () => Boolean(session.orc_enabled),
};

registry
    .category("systray")
    .add("orc_client_provisioning.OrcSystray", orcSystrayItem, { sequence: 100 });
