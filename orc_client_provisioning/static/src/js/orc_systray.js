odoo.define('orc_client_provisioning.OrcSystray', function (require) {
"use strict";

var session = require('web.session');
var SystrayMenu = require('web.SystrayMenu');
var Widget = require('web.Widget');

/**
 * ORC systray icon. Clicking opens /orc/sso/start in a new tab; the
 * server mints a one-time nonce and auto-submits a sign-in form to ORC.
 *
 * Registered only for users with session.orc_enabled (primed by an
 * ir.http._get_session_info override) — the push is gated so non-ORC
 * users never instantiate the widget.
 */
var OrcSystray = Widget.extend({
    name: 'orc_systray',
    template: 'orc_client_provisioning.OrcSystray',
    events: {
        'click .o-orc-systray-btn': '_onClick',
    },

    _onClick: function (ev) {
        ev.preventDefault();
        window.open('/orc/sso/start', '_blank', 'noopener');
    },
});

// 100 keeps us left of the user menu (sequence 0).
OrcSystray.prototype.sequence = 100;

if (session.orc_enabled) {
    SystrayMenu.Items.push(OrcSystray);
}

return OrcSystray;
});
