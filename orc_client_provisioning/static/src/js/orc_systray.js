odoo.define('orc_client_provisioning.OrcSystray', function (require) {
"use strict";

var SystrayMenu = require('web.SystrayMenu');
var Widget = require('web.Widget');
var session = require('web.session');

/**
 * ORC systray icon. Visible iff the current user has orc_enabled=True
 * (primed server-side via ir.http.session_info override).
 *
 * Clicking opens /orc/sso/start in a new tab; the server mints a
 * one-time nonce and returns an auto-submitting form that signs the
 * user in to ORC with no second password prompt.
 */
var OrcSystray = Widget.extend({
    template: 'orc_client_provisioning.OrcSystray',

    /**
     * Hide the widget for users who aren't ORC-enabled. The systray
     * manager has no native skip-me hook, so we mount empty.
     */
    start: function () {
        if (!session.orc_enabled) {
            this.$el.remove();
            return this._super.apply(this, arguments);
        }
        this.$('.o-orc-systray-btn').on('click', this._onClick.bind(this));
        return this._super.apply(this, arguments);
    },

    _onClick: function (ev) {
        ev.preventDefault();
        window.open('/orc/sso/start', '_blank', 'noopener');
    },
});

// 100 keeps us left of the user menu (sequence 0).
OrcSystray.prototype.sequence = 100;
SystrayMenu.Items.push(OrcSystray);

return OrcSystray;
});
