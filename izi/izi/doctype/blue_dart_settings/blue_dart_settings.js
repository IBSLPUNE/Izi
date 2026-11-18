frappe.ui.form.on('Blue Dart Settings', {
    refresh(frm) {
        frm.add_custom_button(__('Test connection'), () => {
            frappe.call({
                method: 'izi.izi.doctype.blue_dart_settings.blue_dart_settings.test_connection',
                freeze: true,
                freeze_message: __('Contacting Blue Dart...'),
                callback: (r) => frappe.msgprint(r.message),
            });
        });
    }
});