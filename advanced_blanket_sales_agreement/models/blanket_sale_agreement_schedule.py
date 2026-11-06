# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, formatLang

# Agreement states in which the delivery plan can be changed.
SCHEDULE_EDITABLE_STATES = ("draft", "rejected", "approved", "issued")
# Changing these on a row that already has an order would contradict that order.
ORDER_BOUND_FIELDS = {"agreement_id", "line_id", "quantity", "scheduled_date"}
CONFIRMED_ORDER_STATES = ("sale",)


class BlanketSaleAgreementSchedule(models.Model):
    _name = "blanket.sale.agreement.schedule"
    _description = "Blanket Sales Agreement Delivery Schedule"
    _order = "agreement_id, scheduled_date, id"

    agreement_id = fields.Many2one(
        "blanket.sale.agreement", required=True, ondelete="cascade", index=True,
    )
    company_id = fields.Many2one(related="agreement_id.company_id", store=True, index=True)
    line_id = fields.Many2one(
        "blanket.sale.agreement.line", string="Agreement Line", required=True, ondelete="cascade",
        domain="[('agreement_id', '=', agreement_id)]",
    )
    product_id = fields.Many2one(related="line_id.product_id", store=True, string="Product")
    product_uom = fields.Many2one(related="line_id.product_uom", string="Unit of Measure")
    scheduled_date = fields.Date(string="Scheduled Date", required=True)
    quantity = fields.Float(digits="Product Unit", required=True, default=1.0)
    note = fields.Char(string="Delivery Notes")
    sale_order_id = fields.Many2one("sale.order", string="Quotation / Order", readonly=True, copy=False)
    state = fields.Selection(
        [("planned", "Planned"), ("quoted", "Quoted"), ("ordered", "Ordered"), ("delivered", "Delivered")],
        compute="_compute_state", string="Status",
    )

    _quantity_positive = models.Constraint("CHECK(quantity > 0)", "Scheduled quantity must be positive.")

    @api.depends("sale_order_id.state", "sale_order_id.order_line.qty_delivered")
    def _compute_state(self):
        for schedule in self:
            order = schedule.sudo().sale_order_id
            if not order or order.state == "cancel":
                schedule.state = "planned"
            elif order.state in ("draft", "sent"):
                schedule.state = "quoted"
            else:
                lines = order.order_line.filtered(lambda sl: sl.blanket_line_id == schedule.line_id)
                delivered = sum(lines.mapped("qty_delivered"))
                ordered = sum(lines.mapped("product_uom_qty"))
                schedule.state = "delivered" if ordered and float_compare(
                    delivered, ordered, precision_rounding=schedule.product_uom.rounding or 0.01) >= 0 \
                    else "ordered"

    @api.constrains("line_id", "agreement_id")
    def _check_line_agreement(self):
        for schedule in self:
            if schedule.line_id.agreement_id != schedule.agreement_id:
                raise ValidationError(_(
                    "The delivery schedule of %(agreement)s must use one of its own lines, not a line of %(other)s.",
                    agreement=schedule.agreement_id.name, other=schedule.line_id.agreement_id.name))

    @api.constrains("scheduled_date", "agreement_id")
    def _check_scheduled_date(self):
        for schedule in self:
            agreement = schedule.agreement_id
            if agreement.date_start and agreement.date_end and not (
                    agreement.date_start <= schedule.scheduled_date <= agreement.date_end):
                raise ValidationError(_("Delivery schedule dates must fall within the agreement period."))

    @api.constrains("quantity", "line_id")
    def _check_scheduled_quantity(self):
        for line in self.mapped("line_id"):
            scheduled = sum(line.schedule_ids.mapped("quantity"))
            if float_compare(scheduled, line.product_uom_qty,
                             precision_rounding=line.product_uom.rounding or 0.01) > 0:
                raise ValidationError(_(
                    "Scheduled quantities for %(product)s (%(scheduled)s) exceed the agreed quantity (%(agreed)s).",
                    product=line.product_id.display_name,
                    scheduled=formatLang(self.env, scheduled, dp="Product Unit"),
                    agreed=formatLang(self.env, line.product_uom_qty, dp="Product Unit")))

    # ------------------------------------------------------------------
    # Locks
    # ------------------------------------------------------------------
    def _check_agreement_allows_planning(self, agreements):
        for agreement in agreements:
            if agreement.state not in SCHEDULE_EDITABLE_STATES:
                raise UserError(_(
                    "The delivery schedule of agreement %(agreement)s cannot be changed while it is %(state)s.",
                    agreement=agreement.name,
                    state=dict(agreement._fields["state"]._description_selection(self.env))[agreement.state]))

    def _has_active_order(self):
        self.ensure_one()
        order = self.sudo().sale_order_id
        return bool(order) and order.state != "cancel"

    @api.model_create_multi
    def create(self, vals_list):
        agreement_ids = {vals.get("agreement_id") for vals in vals_list if vals.get("agreement_id")}
        self._check_agreement_allows_planning(self.env["blanket.sale.agreement"].browse(agreement_ids))
        return super().create(vals_list)

    def write(self, vals):
        if set(vals) - {"sale_order_id"}:
            self._check_agreement_allows_planning(self.agreement_id)
        if set(vals) & ORDER_BOUND_FIELDS:
            bound = self.filtered(lambda schedule: schedule._has_active_order())
            if bound:
                raise UserError(_(
                    "Delivery %(date)s of %(product)s already has quotation or order %(order)s. "
                    "Cancel it before changing the schedule.",
                    date=fields.Date.to_string(bound[0].scheduled_date), product=bound[0].product_id.display_name,
                    order=bound[0].sudo().sale_order_id.name))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_ordered(self):
        self._check_agreement_allows_planning(self.agreement_id)
        for schedule in self:
            order = schedule.sudo().sale_order_id
            if order.state in CONFIRMED_ORDER_STATES:
                raise UserError(_(
                    "Delivery %(date)s of %(product)s is on confirmed order %(order)s and cannot be deleted.",
                    date=fields.Date.to_string(schedule.scheduled_date), product=schedule.product_id.display_name,
                    order=order.name))

    def action_create_quotation(self):
        """Create one quotation per agreement for the selected delivery schedules."""
        orders = self.env["sale.order"]
        for agreement in self.mapped("agreement_id"):
            agreement._check_can_order()
            schedules = self.filtered(lambda s: s.agreement_id == agreement and s.state == "planned")
            if not schedules:
                raise UserError(_("The selected delivery schedules already have a quotation or order."))
            order_vals = agreement._prepare_sale_order_vals(commitment_date=min(schedules.mapped("scheduled_date")))
            order_vals["order_line"] = [
                (0, 0, schedule.line_id._prepare_sale_order_line_vals(schedule.quantity)) for schedule in schedules
            ]
            order = self.env["sale.order"].create(order_vals)
            schedules.write({"sale_order_id": order.id})
            orders |= order
        if len(orders) == 1:
            return self.mapped("agreement_id")._action_open_order(orders)
        return True
