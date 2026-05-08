/** @odoo-module **/

import { Component, onMounted, useEffect, useRef, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { computeIsUnread } from "./orc_chat_service";

/**
 * Single chat window: header (title + fold + close) + iframe body.
 *
 * The iframe is kept around even when folded so a returning user
 * doesn't pay the full SSO handshake + Next.js boot again. That also
 * means each open window holds one SSE connection to ORC — acceptable
 * at N≤3 (the dock's cap); Phase 2b replaces with a shared SSE via
 * the React SDK extraction.
 */
export class OrcChatWindow extends Component {
    static template = "orc_client_tasks.OrcChatWindow";
    static props = {
        roomId: { type: String },
        folded: { type: Boolean },
    };

    setup() {
        this.orcChat = useService("orc_chat");
        // Subscribe to the shared service reactive so the title/unread
        // reflect live task-list updates (see orc_chat_dock.js).
        this.state = useState(this.orcChat.state);
        this.iframeRef = useRef("iframe");
        this.formRef = useRef("form");
        this.ui = useState({
            loading: true,
            error: null,
            /** "form" name that both the iframe and the hidden form
             *  reference so the form submit lands inside the iframe,
             *  not the parent. Unique per window to avoid collisions
             *  when multiple windows open. */
            frameName: `orc-chat-${this._stableId(this.props.roomId)}`,
            /** Nonce handshake — populated after we fetch from
             *  /orc/tasks/open; the form below then submits them. */
            ssoUrl: null,
            ssoNonce: null,
        });

        this._submitted = false;
        // OWL patches the DOM asynchronously after a reactive write.
        // Submitting the form from an rAF scheduled inside _startHandshake
        // raced the patch: the form only renders once ui.ssoUrl + ssoNonce
        // are set, and formRef.el was null when rAF fired, so form.submit()
        // silently no-oped and the iframe stayed on about:blank forever.
        // useEffect runs *after* every patch — by the time this fires with
        // both values set, the form is guaranteed in the DOM.
        useEffect(
            (ssoUrl, ssoNonce) => {
                if (!ssoUrl || !ssoNonce || this._submitted) return;
                const form = this.formRef.el;
                if (!form) return;
                this._submitted = true;
                form.submit();
            },
            () => [this.ui.ssoUrl, this.ui.ssoNonce]
        );
        onMounted(() => this._startHandshake());
    }

    _stableId(roomId) {
        // Slugify the matrix room id into something that's a legal
        // form target name: alphanumeric + dash.
        return String(roomId || "").replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    }

    async _startHandshake() {
        try {
            const { url, nonce } = await this.orcChat.openHandshake(this.props.roomId);
            // The useEffect above watches these two and submits the
            // hidden form after OWL patches it into the DOM.
            this.ui.ssoUrl = url;
            this.ui.ssoNonce = nonce;
        } catch (err) {
            this.ui.loading = false;
            this.ui.error = String(err?.message || err);
        }
    }

    onIframeLoad() {
        // The form submit fires the iframe's load once, plus any
        // subsequent in-iframe navigations. We only care about
        // hiding the "loading" overlay — the first non-about:blank
        // load counts.
        try {
            const href = this.iframeRef.el?.contentWindow?.location?.href || "";
            if (href && !href.startsWith("about:")) {
                this.ui.loading = false;
            }
        } catch {
            // Cross-origin once the iframe lands on ORC — which means
            // it loaded successfully. Flip the flag unconditionally.
            this.ui.loading = false;
        }
    }

    onToggleFold() {
        this.orcChat.toggleFold(this.props.roomId);
    }

    onClose() {
        this.orcChat.closeWindow(this.props.roomId);
    }

    onOpenInOrc() {
        const url = `/orc/tasks/open-in-orc?room_id=${encodeURIComponent(this.props.roomId)}`;
        window.open(url, "_blank", "noopener");
    }

    get task() {
        return this.state.tasks.find((t) => t.room_id === this.props.roomId);
    }

    get title() {
        const t = this.task;
        if (t && t.name) return t.name;
        const raw = String(this.props.roomId || "").replace(/^!/, "");
        return raw.slice(0, 20) + (raw.length > 20 ? "…" : "");
    }

    get unread() {
        const t = this.task;
        return t ? computeIsUnread(t, this.state.lastViewed) : false;
    }
}
