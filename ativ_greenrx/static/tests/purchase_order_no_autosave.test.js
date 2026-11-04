import { expect, test } from "@odoo/hoot";
import { press, unload } from "@odoo/hoot-dom";
import { animationFrame, mockSendBeacon } from "@odoo/hoot-mock";
import {
    contains,
    defineActions,
    defineModels,
    fields,
    getService,
    hideTab,
    models,
    mountView,
    mountWithCleanup,
    onRpc,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";

import { session } from "@web/session";
import { WebClient } from "@web/webclient/webclient";

/**
 * `ativ_greenrx` patches `FormController.prototype` so that a dirty
 * `purchase.order` form never saves behind the user's back. The patch ships in
 * `web.assets_backend`, which `web.assets_unit_tests_setup` includes, so it is
 * active for every test below — including the `res.partner` ones, which is
 * exactly why they are here: they prove the patch is scoped to one model and
 * left every other form on stock Odoo behaviour.
 *
 * The mocked session carries no `ativ_purchase_no_autosave` key, so every test
 * below except the last section also proves that a missing key means "on".
 */

onRpc("has_group", () => true);

const FORM_ARCH = /* xml */ `<form><field name="name"/></form>`;
const LIST_ARCH = /* xml */ `<list><field name="name"/></list>`;

class PurchaseOrder extends models.Model {
    _name = "purchase.order";

    name = fields.Char();

    _records = [
        { id: 1, name: "P00001" },
        { id: 2, name: "P00002" },
    ];
    _views = { form: FORM_ARCH, list: LIST_ARCH };
}

class ResPartner extends models.Model {
    _name = "res.partner";

    name = fields.Char();

    _records = [
        { id: 1, name: "Jean Dupont" },
        { id: 2, name: "Marie Durand" },
    ];
    _views = { form: FORM_ARCH, list: LIST_ARCH };
}

defineModels([PurchaseOrder, ResPartner]);

defineActions([
    {
        id: 1,
        name: "Purchase Orders",
        res_model: "purchase.order",
        views: [
            [false, "list"],
            [false, "form"],
        ],
    },
    {
        id: 2,
        name: "Contacts",
        res_model: "res.partner",
        views: [
            [false, "list"],
            [false, "form"],
        ],
    },
]);

/**
 * The dialog this module adds, as opposed to any other modal Odoo may open.
 */
function expectOurDialog() {
    expect(`.modal`).toHaveCount(1);
    expect(`.modal .modal-title`).toHaveText("Unsaved changes");
    expect(`.modal-footer button.btn-primary`).toHaveText("Save");
    expect(`.modal-footer button.btn-secondary`).toHaveText("Discard");
}

const SAVE_BUTTON = `.modal-footer button.btn-primary`;
const DISCARD_BUTTON = `.modal-footer button.btn-secondary`;
// The control panel's "New", scoped as the stock `save when create button
// clicked` test does: on a small screen the same button is also rendered inside
// the control panel's overflow dropdown, so every test using it is desktop-only.
const NEW_BUTTON = `.o_control_panel_main_buttons .o_form_button_create`;

// ---------------------------------------------------------------------------
// Leaving the record through the breadcrumb (FormController.beforeLeave)
// ---------------------------------------------------------------------------

test.tags("desktop");
test(`purchase.order: the breadcrumb asks instead of saving silently`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountWithCleanup(WebClient);
    await getService("action").doAction(1);
    await contains(`.o_data_row td.o_data_cell`).click();
    expect(`.o_breadcrumb`).toHaveText("Purchase Orders\nP00001");

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(`.breadcrumb-item.o_back_button`).click();

    // nothing saved, nothing left: the user is asked first
    expect.verifySteps([]);
    expectOurDialog();
    expect(`.modal-body`).toHaveText("This purchase order has changes that are not saved yet.");
    expect(`.o_form_view`).toHaveCount(1);
    expect(`.o_field_widget[name=name] input`).toHaveValue("edited");

    // Save -> saves, then leaves
    await contains(SAVE_BUTTON).click();
    await animationFrame();
    expect.verifySteps(["web_save"]);
    expect(`.modal`).toHaveCount(0);
    expect(`.o_breadcrumb`).toHaveText("Purchase Orders");
    expect(`.o_field_cell:eq(0)`).toHaveText("edited");
});

test.tags("desktop");
test(`purchase.order: Discard in the breadcrumb dialog drops the changes and leaves`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountWithCleanup(WebClient);
    await getService("action").doAction(1);
    await contains(`.o_data_row td.o_data_cell`).click();

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(`.breadcrumb-item.o_back_button`).click();
    expectOurDialog();

    await contains(DISCARD_BUTTON).click();
    await animationFrame();
    expect.verifySteps([]);
    expect(`.modal`).toHaveCount(0);
    expect(`.o_breadcrumb`).toHaveText("Purchase Orders");
    expect(`.o_field_cell:eq(0)`).toHaveText("P00001");
});

test.tags("desktop");
test(`purchase.order: dismissing the breadcrumb dialog stays on the record, changes intact`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountWithCleanup(WebClient);
    await getService("action").doAction(1);
    await contains(`.o_data_row td.o_data_cell`).click();

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(`.breadcrumb-item.o_back_button`).click();
    expectOurDialog();

    // Escape is neither Save nor Discard: cancel the navigation
    await press("escape");
    await animationFrame();
    expect.verifySteps([]);
    expect(`.modal`).toHaveCount(0);
    expect(`.o_breadcrumb`).toHaveText("Purchase Orders\nP00001");
    expect(`.o_form_view`).toHaveCount(1);
    expect(`.o_field_widget[name=name] input`).toHaveValue("edited");
    expect(`.o_form_editable`).toHaveClass("o_form_dirty");
});

test.tags("desktop");
test(`purchase.order: a clean record leaves through the breadcrumb without asking`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountWithCleanup(WebClient);
    await getService("action").doAction(1);
    await contains(`.o_data_row td.o_data_cell`).click();
    await contains(`.breadcrumb-item.o_back_button`).click();

    expect(`.modal`).toHaveCount(0);
    expect(`.o_breadcrumb`).toHaveText("Purchase Orders");
    expect.verifySteps([]);
});

// ---------------------------------------------------------------------------
// The pager (FormController.onPagerUpdate)
// ---------------------------------------------------------------------------

test(`purchase.order: the pager asks instead of saving silently, then saves`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "purchase.order",
        type: "form",
        arch: FORM_ARCH,
        resIds: [1, 2],
        resId: 1,
    });

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(`.o_pager_next`).click();

    expect.verifySteps([]);
    expectOurDialog();
    expect(`.o_field_widget[name=name] input`).toHaveValue("edited");

    await contains(SAVE_BUTTON).click();
    await animationFrame();
    expect.verifySteps(["web_save"]);
    expect(`.modal`).toHaveCount(0);
    expect(`.o_field_widget[name=name] input`).toHaveValue("P00002");
});

test(`purchase.order: Discard in the pager dialog drops the changes and moves on`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "purchase.order",
        type: "form",
        arch: FORM_ARCH,
        resIds: [1, 2],
        resId: 1,
    });

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(`.o_pager_next`).click();
    expectOurDialog();

    await contains(DISCARD_BUTTON).click();
    await animationFrame();
    expect.verifySteps([]);
    expect(`.modal`).toHaveCount(0);
    expect(`.o_field_widget[name=name] input`).toHaveValue("P00002");

    // back to the first record: the edit is gone, nothing was written
    await contains(`.o_pager_previous`).click();
    expect(`.modal`).toHaveCount(0);
    expect(`.o_field_widget[name=name] input`).toHaveValue("P00001");
    expect.verifySteps([]);
});

// desktop only: the dialog's `.btn-close` is replaced by a back arrow on mobile,
// where `Dialog` renders fullscreen. Escape, the other dismissal, is covered above.
test.tags("desktop");
test(`purchase.order: dismissing the pager dialog stays on the record, changes intact`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "purchase.order",
        type: "form",
        arch: FORM_ARCH,
        resIds: [1, 2],
        resId: 1,
    });

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(`.o_pager_next`).click();
    expectOurDialog();

    // the X of the dialog, the other way to dismiss it
    await contains(`.modal .btn-close`).click();
    await animationFrame();
    expect.verifySteps([]);
    expect(`.modal`).toHaveCount(0);
    expect(`.o_field_widget[name=name] input`).toHaveValue("edited");
    expect(`.o_form_editable`).toHaveClass("o_form_dirty");
});

test(`purchase.order: a clean record pages without asking`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "purchase.order",
        type: "form",
        arch: FORM_ARCH,
        resIds: [1, 2],
        resId: 1,
    });

    await contains(`.o_pager_next`).click();
    expect(`.modal`).toHaveCount(0);
    expect(`.o_field_widget[name=name] input`).toHaveValue("P00002");
    expect.verifySteps([]);
});

// ---------------------------------------------------------------------------
// The "New" record button (FormController.create)
// ---------------------------------------------------------------------------

test.tags("desktop");
test(`purchase.order: the New button asks instead of saving silently, then saves`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountWithCleanup(WebClient);
    await getService("action").doAction(1);
    await contains(`.o_data_row td.o_data_cell`).click();

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(NEW_BUTTON).click();

    // nothing saved, no blank record yet: the user is asked first
    expect.verifySteps([]);
    expectOurDialog();
    expect(`.o_breadcrumb`).toHaveText("Purchase Orders\nP00001");
    expect(`.o_field_widget[name=name] input`).toHaveValue("edited");

    // Save -> saves, then opens the blank record
    await contains(SAVE_BUTTON).click();
    await animationFrame();
    expect.verifySteps(["web_save"]);
    expect(`.modal`).toHaveCount(0);
    expect(`.o_breadcrumb .active`).toHaveText("New");
    expect(`.o_field_widget[name=name] input`).toHaveValue("");
});

test.tags("desktop");
test(`purchase.order: Discard in the New dialog drops the changes and opens the blank record`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountWithCleanup(WebClient);
    await getService("action").doAction(1);
    await contains(`.o_data_row td.o_data_cell`).click();

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(NEW_BUTTON).click();
    expectOurDialog();

    await contains(DISCARD_BUTTON).click();
    await animationFrame();
    expect.verifySteps([]);
    expect(`.modal`).toHaveCount(0);
    expect(`.o_breadcrumb .active`).toHaveText("New");
    expect(`.o_field_widget[name=name] input`).toHaveValue("");

    // back to the list: the edit is gone, nothing was written
    await contains(`.breadcrumb-item.o_back_button`).click();
    expect(`.modal`).toHaveCount(0);
    expect(`.o_field_cell:eq(0)`).toHaveText("P00001");
    expect.verifySteps([]);
});

test.tags("desktop");
test(`purchase.order: dismissing the New dialog stays on the record, changes intact`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountWithCleanup(WebClient);
    await getService("action").doAction(1);
    await contains(`.o_data_row td.o_data_cell`).click();

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(NEW_BUTTON).click();
    expectOurDialog();

    // Escape is neither Save nor Discard: no new record, stay on this one
    await press("escape");
    await animationFrame();
    expect.verifySteps([]);
    expect(`.modal`).toHaveCount(0);
    expect(`.o_breadcrumb`).toHaveText("Purchase Orders\nP00001");
    expect(`.o_form_view`).toHaveCount(1);
    expect(`.o_field_widget[name=name] input`).toHaveValue("edited");
    expect(`.o_form_editable`).toHaveClass("o_form_dirty");
});

test.tags("desktop");
test(`purchase.order: a clean record opens the New record without asking`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountWithCleanup(WebClient);
    await getService("action").doAction(1);
    await contains(`.o_data_row td.o_data_cell`).click();
    await contains(NEW_BUTTON).click();

    expect(`.modal`).toHaveCount(0);
    expect(`.o_breadcrumb .active`).toHaveText("New");
    expect(`.o_field_widget[name=name] input`).toHaveValue("");
    expect.verifySteps([]);
});

// ---------------------------------------------------------------------------
// Closing or reloading the tab (FormController.beforeUnload)
// ---------------------------------------------------------------------------

test(`purchase.order: closing the tab warns and never saves`, async () => {
    mockSendBeacon(() => expect.step("sendBeacon"));
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "purchase.order",
        type: "form",
        arch: FORM_ARCH,
        resId: 1,
    });

    await contains(`.o_field_widget[name=name] input`).edit("edited");

    const [event] = await unload();
    await animationFrame();

    // the browser's own leave-page warning, and nothing written
    expect(event.defaultPrevented).toBe(true);
    expect.verifySteps([]);
    expect(`.modal`).toHaveCount(0);
    // the changes are still there, pending, for when the user comes back
    expect(`.o_field_widget[name=name] input`).toHaveValue("edited");
    expect(`.o_form_status_indicator_buttons:not(.invisible)`).toHaveCount(1);
});

test(`purchase.order: closing the tab warns about a change the record was not told about yet`, async () => {
    mockSendBeacon(() => expect.step("sendBeacon"));
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "purchase.order",
        type: "form",
        arch: FORM_ARCH,
        resId: 1,
    });

    // typed, not focused out: the record's `dirty` flag is still false, only
    // the field's FIELD_IS_DIRTY signal knows about it
    await contains(`.o_field_widget[name=name] input`).edit("edited", { confirm: false });

    const [event] = await unload();
    await animationFrame();
    expect(event.defaultPrevented).toBe(true);
    expect.verifySteps([]);
});

test(`purchase.order: closing the tab on a clean record does not warn`, async () => {
    mockSendBeacon(() => expect.step("sendBeacon"));
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "purchase.order",
        type: "form",
        arch: FORM_ARCH,
        resId: 1,
    });

    const [event] = await unload();
    await animationFrame();
    expect(event.defaultPrevented).toBe(false);
    expect.verifySteps([]);
});

// ---------------------------------------------------------------------------
// Switching browser tab (FormController.beforeVisibilityChange)
// ---------------------------------------------------------------------------

test(`purchase.order: hiding the tab is a no-op`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "purchase.order",
        type: "form",
        arch: FORM_ARCH,
        resId: 1,
    });

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await hideTab();
    await animationFrame();

    expect.verifySteps([]);
    expect(`.modal`).toHaveCount(0);
    expect(`.o_field_widget[name=name] input`).toHaveValue("edited");
    expect(`.o_form_status_indicator_buttons:not(.invisible)`).toHaveCount(1);

    // and the explicit Save still works
    await contains(`.o_form_button_save`).click();
    expect.verifySteps(["web_save"]);
});

// ---------------------------------------------------------------------------
// Every other model keeps stock Odoo's autosave — the scoping of the patch
// ---------------------------------------------------------------------------

test.tags("desktop");
test(`res.partner: the breadcrumb still autosaves, no dialog`, async () => {
    onRpc("web_save", ({ args }) => {
        expect.step("web_save");
        expect(args).toEqual([[1], { name: "edited" }]);
    });

    await mountWithCleanup(WebClient);
    await getService("action").doAction(2);
    await contains(`.o_data_row td.o_data_cell`).click();
    expect(`.o_breadcrumb`).toHaveText("Contacts\nJean Dupont");

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(`.breadcrumb-item.o_back_button`).click();

    expect(`.modal`).toHaveCount(0);
    expect.verifySteps(["web_save"]);
    expect(`.o_breadcrumb`).toHaveText("Contacts");
    expect(`.o_field_cell:eq(0)`).toHaveText("edited");
});

test(`res.partner: the pager still autosaves, no dialog`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "res.partner",
        type: "form",
        arch: FORM_ARCH,
        resIds: [1, 2],
        resId: 1,
    });

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(`.o_pager_next`).click();

    expect(`.modal`).toHaveCount(0);
    expect.verifySteps(["web_save"]);
    expect(`.o_field_widget[name=name] input`).toHaveValue("Marie Durand");
});

test.tags("desktop");
test(`res.partner: the New button still autosaves, no dialog`, async () => {
    onRpc("web_save", ({ args }) => {
        expect.step("web_save");
        expect(args).toEqual([[1], { name: "edited" }]);
    });

    await mountWithCleanup(WebClient);
    await getService("action").doAction(2);
    await contains(`.o_data_row td.o_data_cell`).click();

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(NEW_BUTTON).click();

    expect(`.modal`).toHaveCount(0);
    expect.verifySteps(["web_save"]);
    expect(`.o_breadcrumb .active`).toHaveText("New");
    expect(`.o_field_widget[name=name] input`).toHaveValue("");
});

test(`res.partner: hiding the tab still autosaves`, async () => {
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "res.partner",
        type: "form",
        arch: FORM_ARCH,
        resId: 1,
    });

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await hideTab();

    expect(`.modal`).toHaveCount(0);
    expect.verifySteps(["web_save"]);
});

test(`res.partner: closing the tab still saves urgently`, async () => {
    mockSendBeacon(() => {
        expect.step("sendBeacon");
        return true;
    });

    await mountView({
        resModel: "res.partner",
        type: "form",
        arch: FORM_ARCH,
        resId: 1,
    });

    await contains(`.o_field_widget[name=name] input`).edit("edited");

    const [event] = await unload();
    await animationFrame();

    expect.verifySteps(["sendBeacon"]);
    expect(event.defaultPrevented).toBe(false);
});

// ---------------------------------------------------------------------------
// The setting "Purchase orders: explicit save only" switched off
// ---------------------------------------------------------------------------

test(`purchase.order, setting off: the pager autosaves like stock, no dialog`, async () => {
    patchWithCleanup(session, { ativ_purchase_no_autosave: false });
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "purchase.order",
        type: "form",
        arch: FORM_ARCH,
        resIds: [1, 2],
        resId: 1,
    });

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await contains(`.o_pager_next`).click();

    expect(`.modal`).toHaveCount(0);
    expect.verifySteps(["web_save"]);
    expect(`.o_field_widget[name=name] input`).toHaveValue("P00002");
});

test(`purchase.order, setting off: hiding the tab autosaves like stock`, async () => {
    patchWithCleanup(session, { ativ_purchase_no_autosave: false });
    onRpc("web_save", () => expect.step("web_save"));

    await mountView({
        resModel: "purchase.order",
        type: "form",
        arch: FORM_ARCH,
        resId: 1,
    });

    await contains(`.o_field_widget[name=name] input`).edit("edited");
    await hideTab();

    expect(`.modal`).toHaveCount(0);
    expect.verifySteps(["web_save"]);
});
