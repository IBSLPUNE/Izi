frappe.ui.form.on('Shipment', {
    refresh(frm) {
        if (frm.is_new() || frm.doc.docstatus === 2) {
            return;
        }
        frappe.call({
            method: 'izi.izi.courier.bluedart.shipment_service.get_booking',
            args: { shipment: frm.doc.name },
            callback: (r) => show_blue_dart(frm, r.message || { status: 'Not Booked' }),
        });
    }
});

function show_blue_dart(frm, booking) {
    const group = __('Blue Dart');
    const status = booking.status;
    const run = (method, args, message) => {
        frappe.call({
            method: `izi.izi.courier.bluedart.shipment_service.${method}`,
            args: args,
            freeze: true,
            freeze_message: message,
            callback: () => frm.reload_doc(),
            error: () => frm.reload_doc(),
        });
    };

    frm.page.set_indicator(__('Blue Dart: {0}', [status]), status === 'Booked' ? 'green' : 'orange');
    if (booking.message) {
        frm.dashboard.add_comment(booking.message, 'orange', true);
    }

    if (status === 'Not Booked') {
        frm.add_custom_button(__('Book Waybill'), () => {
            frappe.confirm(
                __('Book a Blue Dart waybill for this shipment? This creates a real shipment.'),
                () => run('book_shipment', { shipment: frm.doc.name }, __('Booking with Blue Dart...'))
            );
        }, group);
    }

    if (status === 'Booked') {
        frm.add_custom_button(__('Cancel Waybill'), () => {
            frappe.confirm(
                __('Cancel AWB {0} with Blue Dart?', [booking.awb]),
                () => run('cancel_shipment', { shipment: frm.doc.name }, __('Cancelling waybill...'))
            );
        }, group);
    }

    if (booking.label) {
        frm.add_custom_button(__('Open Label'), () => window.open(booking.label), group);
    }

    if ((status === 'Booking Pending' || status === 'Booking Unknown') && frappe.user_roles.includes('System Manager')) {
        frm.add_custom_button(__('Resolve Unknown Booking'), () => {
            frappe.prompt(
                [{
                    fieldname: 'awb',
                    label: __('AWB number (leave blank if Blue Dart has no waybill for this attempt)'),
                    fieldtype: 'Data',
                }],
                (values) => run('resolve_unknown_booking', { shipment: frm.doc.name, awb: values.awb || '' }, __('Saving...')),
                __('Resolve unknown booking'),
                __('Save')
            );
        }, group);
    }
}