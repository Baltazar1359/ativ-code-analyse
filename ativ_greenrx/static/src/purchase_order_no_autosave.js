import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";
import { useBus } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";
import { session } from "@web/session";
import { FetchRecordError } from "@web/model/relational_model/errors";
import { FormController } from "@web/views/form/form_controller";
import { executeButtonCallback } from "@web/views/view_button/view_button_hook";

/**
 * The form view of this model never saves on its own: the user has to click Save.
 * Odoo saves a dirty form record behind the user's back in five places, all of
 * them patched below. Every other model keeps the standard behaviour.
 *
 * Switchable in Settings > ATIV ("Purchase orders: explicit save only"); the
 * server sends the setting as `session.ativ_purchase_no_autosave`. Only an
 * explicit `false` turns it off: a missing key keeps explicit save, the
 * behaviour this patch had before it became a setting.
 */
const NO_AUTOSAVE_MODEL = "purchase.order";

/**
 * @param {FormController} controller
 * @returns {boolean} whether this form is the one this module takes over
 */
function isNoAutosaveForm(controller) {
    return (
        session.ativ_purchase_no_autosave !== false &&
        controller.props.resModel === NO_AUTOSAVE_MODEL
    );
}

/**
 * "Are there changes that would be saved?" — the accurate answer.
 *
 * `record.dirty` is only a flag, and record.js warns that it may still be false
 * while a field holds a change: a text input keeps the typed value until it is
 * blurred, html and many2many_checkboxes commit lazily too. `isDirty()` asks the
 * fields first; it is what the stock `onPagerUpdate` uses.
 *
 * @param {FormController} controller
 * @returns {Promise<boolean>}
 */
function hasPendingChanges(controller) {
    return controller.model.root.isDirty();
}

/**
 * The same question, answered synchronously — for `beforeUnload` only, which may
 * not yield before calling `preventDefault()`. This mirrors what the form's own
 * save indicator shows (`FormStatusIndicator.indicatorMode`): the record's flag,
 * plus the `FIELD_IS_DIRTY` a field announces while the user is typing in it.
 *
 * @param {FormController} controller
 * @returns {boolean}
 */
function hasPendingChangesSync(controller) {
    return controller.model.root.dirty || controller.noAutosaveFieldIsDirty === true;
}

/**
 * Ask the user what to do with the changes of the current record.
 *
 * @param {FormController} controller
 * @param {Object} actions
 * @param {() => Promise<unknown>} actions.save run when the user picks Save
 * @param {() => Promise<unknown>} actions.discard run when the user picks Discard
 * @returns {Promise<boolean>} `true` to go ahead (the changes were saved or
 *      discarded), `false` to stay on the record — the value
 *      `clearUncommittedChanges` (`@web/webclient/actions/action_service`) reads
 *      to cancel a navigation.
 */
function askSaveOrDiscard(controller, { save, discard }) {
    return new Promise((resolve) => {
        let answer;
        controller.dialogService.add(
            ConfirmationDialog,
            {
                title: _t("Unsaved changes"),
                body: _t("This purchase order has changes that are not saved yet."),
                confirmLabel: _t("Save"),
                confirm: async () => {
                    // A save refused by the record (invalid fields) leaves the
                    // user on it, with the record's own notification.
                    answer = (await save()) !== false;
                },
                cancelLabel: _t("Discard"),
                cancel: async () => {
                    await discard();
                    answer = true;
                },
                // Dismissing the dialog (Escape, the X) is neither Save nor
                // Discard: keep the changes and stay where we are.
                dismiss: () => {
                    answer = false;
                },
            },
            // Resolve once the dialog is gone, whatever closed it — including a
            // callback above throwing, so that a navigation never ends up
            // waiting forever on this promise.
            { onClose: () => resolve(answer ?? false) }
        );
    });
}

patch(FormController.prototype, {
    setup() {
        super.setup(...arguments);
        if (isNoAutosaveForm(this)) {
            // `beforeUnload` cannot await the fields, so follow their "the user
            // is typing in me" signal to also know about a change the record has
            // not been told about yet.
            this.noAutosaveFieldIsDirty = false;
            useBus(this.model.bus, "FIELD_IS_DIRTY", (ev) => {
                this.noAutosaveFieldIsDirty = ev.detail;
            });
        }
    },

    /**
     * Leaving the record through the breadcrumb, a menu or any other action.
     * Stock saves silently; ask instead. Resolving `false` cancels the
     * navigation, per `clearUncommittedChanges`.
     */
    async beforeLeave(options) {
        if (!isNoAutosaveForm(this) || options?.forceLeave || !(await hasPendingChanges(this))) {
            return super.beforeLeave(...arguments);
        }
        return askSaveOrDiscard(this, {
            save: () =>
                this.save({
                    reload: false,
                    onError: (error, errorOptions) => this.onSaveError(error, errorOptions, true),
                }),
            discard: () => this.model.root.discard(),
        });
    },

    /**
     * The pager. It is not routed through `beforeLeave` — it has its own
     * dirty-check-and-save — so it needs the same dialog, and then the move to
     * the record at `offset` the stock method would have done.
     */
    async onPagerUpdate({ offset, resIds }) {
        if (!isNoAutosaveForm(this) || !(await hasPendingChanges(this))) {
            return super.onPagerUpdate(...arguments);
        }
        // Same recovery as the stock method when the record moved to is gone.
        const move = async (action) => {
            try {
                return await action();
            } catch (e) {
                if (e instanceof FetchRecordError) {
                    this.model.load({
                        resIds: this.model.config.resIds.filter((id) => !e.resIds.includes(id)),
                    });
                }
                throw e;
            }
        };
        await askSaveOrDiscard(this, {
            // `nextId` makes the save itself land on the next record, as stock does.
            save: () =>
                move(() =>
                    this.model.root.save({
                        onError: (error, errorOptions) =>
                            this.onSaveError(error, errorOptions, true),
                        nextId: resIds[offset],
                    })
                ),
            discard: () =>
                move(async () => {
                    await this.model.root.discard();
                    await this.model.load({ resId: resIds[offset] });
                }),
        });
    },

    /**
     * The "New" button. Leaving the record for a blank one is navigation just
     * like the breadcrumb, so it asks the same question instead of saving
     * silently; dismissing the dialog keeps the user on the record, as stock
     * never does — stock always ends up on the new record.
     */
    async create() {
        if (!isNoAutosaveForm(this) || !(await hasPendingChanges(this))) {
            return super.create(...arguments);
        }
        const canProceed = await askSaveOrDiscard(this, {
            save: () =>
                this.model.root.save({
                    onError: (error, errorOptions) => this.onSaveError(error, errorOptions, true),
                }),
            discard: () => this.model.root.discard(),
        });
        if (canProceed) {
            // What the stock method does once it may go ahead.
            await executeButtonCallback(this.ui.activeElement, () =>
                this.model.load({ resId: false })
            );
        }
    },

    /**
     * Switching to another browser tab. Stock saves the record here without even
     * looking at `dirty` — exactly the autosave this module removes. A hidden tab
     * cannot usefully ask anything, so the changes just stay pending until the
     * user comes back and acts on them.
     */
    beforeVisibilityChange() {
        if (!isNoAutosaveForm(this)) {
            return super.beforeVisibilityChange(...arguments);
        }
    },

    /**
     * Closing or reloading the tab. No `urgentSave()` — the browser shows its own
     * leave-page warning instead, and the user's answer there either keeps the
     * page (changes intact) or drops the changes. Nothing is ever written.
     */
    beforeUnload(ev) {
        if (!isNoAutosaveForm(this)) {
            return super.beforeUnload(...arguments);
        }
        if (hasPendingChangesSync(this)) {
            // Not translated, as in stock Odoo: browsers show their own wording.
            ev.preventDefault();
            ev.returnValue = "Unsaved changes";
        }
    },
});
