# -*- coding: utf-8 -*-
from collections import defaultdict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_compare, formatLang

CONFIRMED_ORDER_STATES = ("sale", "done")
# Order line fields whose change on a confirmed order is re-checked against the agreement.
BLANKET_CHECKED_FIELDS = {"product_uom_qty", "product_uom", "price_unit", "discount", "blanket_line_id", "product_id"}


class SaleOrder(models.Model):
    _inherit = "sale.order"

    blanket_agreement_id = fields.Many2one(
        "blanket.sale.agreement", string="Blanket Agreement", index=True, copy=False,
        domain="[('state', '=', 'issued'), ('partner_id', 'child_of', commercial_partner_id),"
               " ('company_id', '=', company_id)]",
        states={"sale": [("readonly", True)], "done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    commercial_partner_id = fields.Many2one(related="partner_id.commercial_partner_id")

    @api.constrains("blanket_agreement_id")
    def _check_blanket_lines_match_agreement(self):
        self.order_line._check_blanket_line_order()

    def action_confirm(self):
        self._check_blanket_agreement_consumption()
        return super().action_confirm()

    def _check_blanket_agreement_consumption(self):
        today = fields.Date.context_today(self)
        consumed = defaultdict(float)
        for order in self.filtered("blanket_agreement_id"):
            agreement = order.blanket_agreement_id.sudo()
            if agreement.state != "issued":
                raise ValidationError(_(
                    "Order %(order)s is linked to agreement %(agreement)s which is not issued.",
                    order=order.name, agreement=agreement.name))
            if not agreement.date_start <= today <= agreement.date_end:
                raise ValidationError(_(
                    "Agreement %(agreement)s is only valid from %(start)s to %(end)s.",
                    agreement=agreement.name, start=agreement.date_start, end=agreement.date_end))
            if order.partner_id.commercial_partner_id != agreement.partner_id.commercial_partner_id:
                raise ValidationError(_("Order %s customer does not match its blanket agreement.", order.name))
            if order.currency_id != agreement.currency_id:
                raise ValidationError(_(
                    "Order %(order)s is in %(order_currency)s but agreement %(agreement)s is in %(currency)s. "
                    "Use a pricelist in %(currency)s.",
                    order=order.name, order_currency=order.currency_id.name, agreement=agreement.name,
                    currency=agreement.currency_id.name))
            for sale_line in order.order_line.filtered("blanket_line_id"):
                blanket_line = sale_line.blanket_line_id.sudo()
                consumed[blanket_line] += sale_line.product_uom._compute_quantity(
                    sale_line.product_uom_qty, blanket_line.product_uom)
        for blanket_line, qty in consumed.items():
            available = blanket_line.qty_remaining
            if float_compare(qty, available, precision_rounding=blanket_line.product_uom.rounding or 0.01) > 0:
                raise ValidationError(_(
                    "Ordered quantity for %(product)s (%(qty)s) exceeds the remaining quantity "
                    "on agreement %(agreement)s (%(remaining)s).",
                    product=blanket_line.product_id.display_name,
                    qty=formatLang(self.env, qty, dp="Product Unit of Measure"),
                    agreement=blanket_line.agreement_id.name,
                    remaining=formatLang(self.env, available, dp="Product Unit of Measure")))
        self.order_line.filtered("blanket_line_id")._check_blanket_price()


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    blanket_line_id = fields.Many2one(
        "blanket.sale.agreement.line", string="Agreement Line", index=True, copy=False, ondelete="restrict",
        domain="[('agreement_id', '=', parent.blanket_agreement_id), ('product_id', '=', product_id)]",
    )

    @api.constrains("blanket_line_id", "product_id")
    def _check_blanket_line_product(self):
        for line in self.filtered("blanket_line_id"):
            if line.blanket_line_id.product_id != line.product_id:
                raise ValidationError(_("The agreement line product must match the order line product."))

    @api.constrains("blanket_line_id", "order_id")
    def _check_blanket_line_order(self):
        for line in self.filtered("blanket_line_id"):
            agreement = line.blanket_line_id.sudo().agreement_id
            if line.order_id.blanket_agreement_id != agreement:
                raise ValidationError(_(
                    "Order %(order)s uses a line of agreement %(agreement)s but is not linked to it. "
                    "Set %(agreement)s as the order's blanket agreement.",
                    order=line.order_id.name, agreement=agreement.name))

    # ------------------------------------------------------------------
    # Agreed price on linked lines
    # ------------------------------------------------------------------
    def _uses_blanket_price(self):
        self.ensure_one()
        blanket_line = self.blanket_line_id.sudo()
        return bool(blanket_line) and not self.qty_invoiced and \
            self.order_id.currency_id == blanket_line.currency_id and bool(self.product_uom)

    @api.depends("blanket_line_id")
    def _compute_price_unit(self):
        super()._compute_price_unit()
        for line in self:
            if line._uses_blanket_price():
                blanket_line = line.blanket_line_id.sudo()
                line.price_unit = blanket_line.product_uom._compute_price(blanket_line.price_unit, line.product_uom)

    @api.depends("blanket_line_id")
    def _compute_discount(self):
        super()._compute_discount()
        for line in self:
            if line._uses_blanket_price():
                line.discount = line.blanket_line_id.sudo().discount

    def _blanket_price_deviation(self):
        """(agreed, ordered) net unit prices in the agreement unit when they differ, else None."""
        self.ensure_one()
        blanket_line = self.blanket_line_id.sudo()
        agreed = blanket_line.price_unit * (1 - (blanket_line.discount or 0.0) / 100.0)
        ordered = self.product_uom._compute_price(
            self.price_unit * (1 - (self.discount or 0.0) / 100.0), blanket_line.product_uom)
        rounding = blanket_line.currency_id.rounding or 0.01
        if float_compare(agreed, ordered, precision_rounding=rounding):
            return agreed, ordered
        return None

    def _check_blanket_price(self):
        """Linked lines are sold at the agreed net price. Sales Administrators may deviate;
        the deviation is logged on the order."""
        deviations = [(line, deviation) for line in self.filtered("blanket_line_id")
                      for deviation in [line._blanket_price_deviation()] if deviation]
        if not deviations:
            return
        lines_text = [
            _("%(product)s: agreed %(agreed)s, ordered %(ordered)s",
              product=line.product_id.display_name,
              agreed=formatLang(self.env, agreed, currency_obj=line.blanket_line_id.sudo().currency_id),
              ordered=formatLang(self.env, ordered, currency_obj=line.blanket_line_id.sudo().currency_id))
            for line, (agreed, ordered) in deviations]
        if not (self.env.su or self.env.user.has_group("sales_team.group_sale_manager")):
            raise ValidationError(_(
                "These prices differ from the blanket agreement. Use the agreed price, "
                "or ask a Sales Administrator to confirm:\n%s", "\n".join(lines_text)))
        for order in self.order_id:
            order_lines = [text for (line, _dev), text in zip(deviations, lines_text) if line.order_id == order]
            order.message_post(
                body=Markup("%s<br/>%s") % (
                    _("Prices differing from the agreed prices were accepted by %s:", self.env.user.name),
                    Markup("<br/>").join(order_lines)),
                subtype_xmlid="mail.mt_note",
            )

    # ------------------------------------------------------------------
    # Changes after confirmation are checked like the confirmation itself
    # ------------------------------------------------------------------
    def _check_confirmed_blanket_lines(self):
        lines = self.filtered(lambda line: line.blanket_line_id and line.order_id.state in CONFIRMED_ORDER_STATES)
        if not lines:
            return
        for blanket_line in lines.blanket_line_id.sudo():
            if float_compare(blanket_line.qty_ordered, blanket_line.product_uom_qty,
                             precision_rounding=blanket_line.product_uom.rounding or 0.01) > 0:
                raise ValidationError(_(
                    "Confirmed orders for %(product)s would total %(qty)s, more than the %(agreed)s "
                    "agreed on %(agreement)s.",
                    product=blanket_line.product_id.display_name,
                    qty=formatLang(self.env, blanket_line.qty_ordered, dp="Product Unit of Measure"),
                    agreed=formatLang(self.env, blanket_line.product_uom_qty, dp="Product Unit of Measure"),
                    agreement=blanket_line.agreement_id.name))
        lines._check_blanket_price()

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines._check_confirmed_blanket_lines()
        return lines

    def write(self, vals):
        result = super().write(vals)
        if set(vals) & BLANKET_CHECKED_FIELDS:
            self._check_confirmed_blanket_lines()
        return result
