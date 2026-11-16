frappe.ui.form.on('Vendor Report Import', {
    refresh(frm) {
        if (frm.is_new() || !frm.doc.report_file) {
            return;
        }

        frm.add_custom_button(__('Process & Import File'), () => {
            const run = () => {
                frappe.call({
                    method: 'process_file',
                    doc: frm.doc,
                    freeze: true,
                    freeze_message: __('Reading file and creating draft documents...'),
                    callback: () => frm.reload_doc(),
                    // Server errors: reload so the Failed status and message show.
                    error: () => frm.reload_doc(),
                });
            };

            const start = () => {
                if (frm.doc.import_status === 'Processing') {
                    frappe.confirm(
                        __('This import still shows as Processing (it may have been interrupted). Run it again?'),
                        run
                    );
                } else {
                    run();
                }
            };

            // Save unsaved edits first, so the server reads the file the user is looking at.
            if (frm.is_dirty()) {
                frm.save().then(start);
            } else {
                start();
            }
        }).addClass('btn-primary');

        if (frm.doc.import_status === 'Validated' && frm.doc.validation_errors) {
            frm.dashboard.add_comment(
                __('Some purchase orders were not created. Review Validation Errors below, map the missing items, and run the import again.'),
                'orange',
                true
            );
        }
        if (frm.doc.import_status === 'Completed') {
            frm.dashboard.add_comment(
                __('Import completed. Draft Sales Orders were created; review them before submitting.'),
                'green',
                true
            );
        }
    }
});
