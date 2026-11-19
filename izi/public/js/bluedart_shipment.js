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
        frm.add_custom_button(__('Book Waybill'), () => open_booking_dialog(booking, (options) => {
            frappe.confirm(
                __('Book a {0} Blue Dart waybill for this shipment? This creates a real shipment.', [options.payment_mode]),
                () => run('book_shipment', {
                    shipment: frm.doc.name,
                    options: JSON.stringify(options),
                }, __('Booking with Blue Dart...'))
            );
        }), group);
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
        frm.add_custom_button(__('Download Label'), () => window.open(booking.label), group);
    }

    if (booking.invoice) {
        frm.add_custom_button(__('Download Invoice'), () => window.open(invoice_pdf_url(booking.invoice)), group);
    } else if (status === 'Booked') {
        frm.dashboard.add_comment(
            __('No submitted Sales Invoice is linked to this Delivery Note yet. Create the invoice, then reload this page.'),
            'blue',
            true
        );
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

function open_booking_dialog(booking, on_submit) {
    const o = booking.options || {};
    frappe.prompt(
        [
            {
                fieldname: 'payment_mode',
                label: __('Payment Mode'),
                fieldtype: 'Select',
                options: 'Prepaid\nCOD',
                default: o.payment_mode || 'Prepaid',
                reqd: 1,
            },
            {
                fieldname: 'product_code',
                label: __('Product (A = Air, E = Ground, D = Domestic Priority)'),
                fieldtype: 'Select',
                options: 'A\nE\nD',
                default: o.product_code || 'E',
                reqd: 1,
                description: __('COD is available for Air (A) and Ground (E) only.'),
            },
            {
                fieldname: 'pickup_time',
                label: __('Pickup Time (HHMM)'),
                fieldtype: 'Data',
                default: o.pickup_time || '1700',
                reqd: 1,
            },
            {
                fieldname: 'commodity',
                label: __('Commodity Description'),
                fieldtype: 'Data',
                default: o.commodity || '',
                reqd: 1,
            },
            {
                fieldname: 'collectable_amount',
                label: __('Amount to Collect on Delivery (COD)'),
                fieldtype: 'Currency',
                default: o.collectable_amount || booking.amount || 0,
                depends_on: "eval:doc.payment_mode=='COD'",
                mandatory_depends_on: "eval:doc.payment_mode=='COD'",
            },
        ],
        on_submit,
        __('Blue Dart booking'),
        __('Continue')
    );
}

function invoice_pdf_url(invoice_name) {
    return '/api/method/frappe.utils.print_format.download_pdf'
        + '?doctype=' + encodeURIComponent('Sales Invoice')
        + '&name=' + encodeURIComponent(invoice_name)
        + '&format=Standard&no_letterhead=0';
}