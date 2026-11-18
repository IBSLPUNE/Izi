frappe.ui.form.on('Delivery Note', {
    refresh(frm) {
        if (frm.doc.docstatus !== 1 || frm.doc.is_return) {
            return;
        }
        frm.add_custom_button(__('Blue Dart Shipment'), () => {
            frappe.call({
                method: 'izi.izi.courier.bluedart.shipment_service.create_shipment_from_delivery_note',
                args: { delivery_note: frm.doc.name },
                freeze: true,
                freeze_message: __('Creating shipment...'),
                callback: (r) => {
                    if (r.message) {
                        frappe.set_route('Form', 'Shipment', r.message);
                    }
                },
            });
        }, __('Create'));
    }
});