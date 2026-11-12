import { Component, markup, onWillStart, useRef, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { escape } from "@web/core/utils/strings";
import { patch } from "@web/core/utils/patch";
import { router } from "@web/core/browser/router";
import { KswHome } from "@KSW_backend_theme/home/home";

/** Plain text from the model -> safe HTML: escaped first, then **bold**
 * and line breaks. Nothing the model writes can inject markup. */
function renderAnswer(text) {
    const html = escape(text || "")
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/\n/g, "<br/>");
    return markup(html);
}

/**
 * Ask Odoo in plain words. Shared by the Home page box and the top-bar
 * dialog. Every table the assistant returns shows the filter it ran and
 * opens the same list in Odoo, so the reader can check the answer.
 */
export class KswAiChat extends Component {
    static template = "KSW_ai_assistant.Chat";
    static props = {
        compact: { type: Boolean, optional: true },
        close: { type: Function, optional: true },
        // The screen the user opened the assistant from: {model, res_id, title}.
        page: { type: [Object, { value: null }], optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.inputRef = useRef("input");
        this.state = useState({
            status: null,
            turns: [],
            conversationId: false,
            question: "",
            busy: false,
            page: this.props.page || null,
        });
        onWillStart(async () => {
            this.state.status = await this.orm.call("ksw.ai.conversation", "ksw_ai_status", []);
        });
    }

    get examples() {
        if (this.state.page?.res_id) {
            return [
                _t("Explain this record"),
                _t("How is this amount calculated?"),
                _t("What changed compared with last month?"),
            ];
        }
        return [
            _t("Show me all loans above 5,000 SAR"),
            _t("Who is on annual leave this week?"),
            _t("Total deductions per department this month"),
            _t("Employees absent more than 3 days last month"),
        ];
    }

    useExample(text) {
        this.state.question = text;
        this.ask();
    }

    onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.ask();
        }
    }

    async ask() {
        const question = this.state.question.trim();
        if (!question || this.state.busy) {
            return;
        }
        const turn = { question, answer: null, blocks: [], error: false };
        this.state.turns.push(turn);
        this.state.question = "";
        this.state.busy = true;
        try {
            const res = await this.orm.call("ksw.ai.conversation", "ksw_ai_ask", [
                question,
                this.state.conversationId,
                this.state.page,
            ]);
            this.state.conversationId = res.conversation_id;
            const last = this.state.turns[this.state.turns.length - 1];
            last.answer = renderAnswer(res.answer);
            last.blocks = res.blocks;
            last.error = res.error;
        } catch (error) {
            const last = this.state.turns[this.state.turns.length - 1];
            last.answer = renderAnswer(error.data?.message || _t("Something went wrong."));
            last.error = true;
        } finally {
            this.state.busy = false;
            this.inputRef.el?.focus();
        }
    }

    dropPage() {
        this.state.page = null;
    }

    newChat() {
        this.state.turns = [];
        this.state.conversationId = false;
        this.inputRef.el?.focus();
    }

    openList(block) {
        const views = block.kind === "summary"
            ? [[false, "list"], [false, "pivot"], [false, "form"]]
            : [[false, "list"], [false, "form"]];
        this.props.close?.();
        this.action.doAction({
            type: "ir.actions.act_window",
            name: block.title || block.model_label,
            res_model: block.model,
            domain: block.domain,
            views,
            context: block.kind === "summary" && block.group_by.length
                ? { group_by: block.group_by } : {},
        });
    }

    openRecord(block, index) {
        const id = block.ids?.[index];
        if (!id) {
            return;
        }
        this.props.close?.();
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: block.model,
            res_id: id,
            views: [[false, "form"]],
        });
    }

    sumEntries(block) {
        return Object.entries(block.sums || {});
    }

    formatCell(value) {
        if (typeof value === "number") {
            return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
        }
        return value;
    }
}

/** Top-bar entry: the same chat in a dialog, from any screen. */
export class KswAiDialog extends Component {
    static template = "KSW_ai_assistant.Dialog";
    static components = { Dialog, KswAiChat };
    static props = { close: Function, page: { type: [Object, { value: null }], optional: true } };
}

/** The record or list on screen, so "this payslip" needs no explaining. */
export function currentPage(actionService) {
    const ctrl = actionService.currentController;
    const model = ctrl?.props?.resModel || router.current.model;
    if (!model) {
        return null;
    }
    const resId = ctrl?.currentState?.resId || ctrl?.props?.resId || router.current.resId;
    return {
        model,
        res_id: typeof resId === "number" ? resId : false,
        title: ctrl?.displayName || ctrl?.action?.name || "",
        url: window.location.pathname,
    };
}

export class KswAiSystray extends Component {
    static template = "KSW_ai_assistant.Systray";
    static props = {};
    setup() {
        this.dialog = useService("dialog");
        this.action = useService("action");
    }
    open() {
        this.dialog.add(KswAiDialog, { page: currentPage(this.action) });
    }
}

registry.category("systray").add("KswAiAssistant", { Component: KswAiSystray }, { sequence: 90 });

// Home: the Ask box sits above "Waiting for you".
patch(KswHome, { components: { ...(KswHome.components || {}), KswAiChat } });
