# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, formatLang

from .blanket_sale_agreement import EDITABLE_STATES

# Line fields that may still be written while the agreement is locked.
UNLOCKED_LINE_FIELDS = {"sequence"}


class BlanketSaleAgreementLine(models.Model):
    _name = "blanket.sale.agreement.line"
    _description = "Blanket Sales Agreement Line"
    _order = "agreement_id, sequence, id"
    _check_company_auto = True

    agreement_id = fields.Many2one(
        "blanket.sale.agreement", required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(related="agreement_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="agreement_id.currency_id", store=True)
    partner_id = fields.Many2one(related="agreement_id.partner_id")
    state = fields.Selection(related="agreement_id.state")
    product_id = fields.Many2one(
        "product.product", string="Product", required=True, check_company=True,
        domain="[('sale_ok', '=', True), '|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    name = fields.Text(
        string="Description", compute="_compute_name", store=True, readonly=False, precompute=True,
    )
    product_uom_category_id = fields.Many2one(related="product_id.uom_id.category_id")
    product_uom = fields.Many2one(
        "uom.uom", string="Unit of Measure", required=True,
        compute="_compute_product_uom", store=True, readonly=False, precompute=True,
        domain="[('category_id', '=', product_uom_category_id)]",
    )
    product_uom_qty = fields.Float(string="Agreed Qty", digits="Product Unit of Measure", default=1.0,
                                   required=True)
    price_unit = fields.Float(
        string="Unit Price", digits="Product Price",
        compute="_compute_price_unit", store=True, readonly=False, precompute=True,
        help="Negotiated price. Filled from the pricelist when the product is chosen, "
             "then kept as entered.",
    )
    discount = fields.Float(string="Disc.%", digits="Discount", default=0.0)
    tax_ids = fields.Many2many(
        "account.tax", string="Taxes", check_company=True,
        compute="_compute_tax_ids", store=True, readonly=False, precompute=True,
        domain="[('type_tax_use', '=', 'sale'), ('company_id', '=', company_id)]",
    )
    price_subtotal = fields.Monetary(compute="_compute_amount", store=True, string="Subtotal")
    price_tax = fields.Monetary(compute="_compute_amount", store=True, string="Tax")
    price_total = fields.Monetary(compute="_compute_amount", store=True, string="Total")

    sale_line_ids = fields.One2many("sale.order.line", "blanket_line_id", string="Sales Order Lines")
    qty_ordered = fields.Float(compute="_compute_quantities", digits="Product Unit of Measure",
                               string="Ordered Qty")
    qty_quoted = fields.Float(compute="_compute_quantities", digits="Product Unit of Measure",
                              string="Quoted Qty",
                              help="Quantity in open quotations that are not confirmed yet.")
    qty_delivered = fields.Float(compute="_compute_quantities", digits="Product Unit of Measure",
                                 string="Delivered Qty")
    qty_remaining = fields.Float(compute="_compute_quantities", digits="Product Unit of Measure",
                                 string="Remaining Qty")
    qty_scheduled = fields.Float(compute="_compute_quantities", digits="Product Unit of Measure",
                                 string="Scheduled Qty")
    schedule_ids = fields.One2many("blanket.sale.agreement.schedule", "line_id", string="Schedules")

    _sql_constraints = [
        ("qty_positive", "CHECK(product_uom_qty >= 0)", "The agreed quantity cannot be negative."),
        ("discount_range", "CHECK(discount >= 0 AND discount <= 100)", "Discount must be between 0 and 100."),
    ]

    @api.depends("product_id")
    def _compute_name(self):
        for line in self:
            if line.product_id:
                product = line.product_id.with_context(lang=line.agreement_id.partner_id.lang or self.env.lang)
                line.name = product.get_product_multiline_description_sale()
            elif not line.name:
                line.name = False

    @api.depends("product_id")
    def _compute_product_uom(self):
        for line in self:
            if not line.product_uom or line.product_id.uom_id.category_id != line.product_uom.category_id:
                line.product_uom = line.product_id.uom_id

    @api.depends("product_id")
    def _compute_price_unit(self):
        """Suggest a price when the product is chosen. The price is negotiated: later changes
        of quantity or pricelist never overwrite it."""
        for line in self:
            if not line.product_id:
                line.price_unit = 0.0
                continue
            line.price_unit = line._get_pricelist_price()

    def _get_pricelist_price(self):
        self.ensure_one()
        agreement = self.agreement_id
        uom = self.product_uom or self.product_id.uom_id
        date = agreement.date_start or fields.Date.context_today(self)
        if agreement.pricelist_id:
            return agreement.pricelist_id._get_product_price(
                self.product_id, self.product_uom_qty or 1.0, uom=uom, date=date)
        price = self.product_id.uom_id._compute_price(self.product_id.lst_price, uom)
        company = agreement.company_id or self.env.company
        if self.currency_id and self.currency_id != company.currency_id:
            price = company.currency_id._convert(price, self.currency_id, company, date)
        return price

    @api.onchange("product_uom")
    def _onchange_product_uom_convert_price(self):
        """Switching the unit converts the negotiated price instead of re-pricing it."""
        previous = self._origin.product_uom or self.product_id.uom_id
        if self.product_uom and previous and previous != self.product_uom \
                and previous.category_id == self.product_uom.category_id:
            self.price_unit = previous._compute_price(self.price_unit, self.product_uom)

    @api.depends("product_id", "agreement_id.partner_id")
    def _compute_tax_ids(self):
        for line in self:
            company = line.company_id or self.env.company
            taxes = line.product_id.taxes_id.filtered(lambda t: t.company_id == company)
            fpos = self.env["account.fiscal.position"].with_company(company)._get_fiscal_position(
                line.agreement_id.partner_id) if line.agreement_id.partner_id else False
            line.tax_ids = fpos.map_tax(taxes) if fpos else taxes

    @api.depends("product_uom_qty", "price_unit", "discount", "tax_ids", "currency_id")
    def _compute_amount(self):
        for line in self:
            price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
            currency = line.currency_id or line.company_id.currency_id or self.env.company.currency_id
            taxes = line.tax_ids.compute_all(
                price, currency, line.product_uom_qty, product=line.product_id,
                partner=line.agreement_id.partner_id)
            line.price_subtotal = taxes["total_excluded"]
            line.price_total = taxes["total_included"]
            line.price_tax = taxes["total_included"] - taxes["total_excluded"]

    @api.depends("sale_line_ids.product_uom_qty", "sale_line_ids.qty_delivered", "sale_line_ids.state",
                 "sale_line_ids.product_uom", "schedule_ids.quantity", "product_uom_qty")
    def _compute_quantities(self):
        for line in self:
            ordered = delivered = quoted = 0.0
            # sudo: quantities must be visible to approvers without Sales rights
            for sale_line in line.sudo().sale_line_ids:
                qty = sale_line.product_uom._compute_quantity(sale_line.product_uom_qty, line.product_uom)
                if sale_line.state in ("sale", "done"):
                    ordered += qty
                    delivered += sale_line.product_uom._compute_quantity(sale_line.qty_delivered, line.product_uom)
                elif sale_line.state in ("draft", "sent"):
                    quoted += qty
            line.qty_ordered = ordered
            line.qty_quoted = quoted
            line.qty_delivered = delivered
            line.qty_remaining = max(line.product_uom_qty - ordered, 0.0)
            line.qty_scheduled = sum(line.schedule_ids.mapped("quantity"))

    @api.constrains("product_id", "product_uom")
    def _check_uom_category(self):
        for line in self:
            if line.product_uom and line.product_id and \
                    line.product_uom.category_id != line.product_id.uom_id.category_id:
                raise ValidationError(_(
                    "The unit of measure of line '%s' must belong to the same category as the product's unit.",
                    line.product_id.display_name))

    @api.constrains("product_id", "agreement_id")
    def _check_unique_product(self):
        for line in self:
            twins = line.agreement_id.line_ids.filtered(lambda other: other.product_id == line.product_id)
            if len(twins) > 1:
                raise ValidationError(_(
                    "%(product)s appears more than once on agreement %(agreement)s. "
                    "Use a single line per product.",
                    product=line.product_id.display_name, agreement=line.agreement_id.name))

    @api.constrains("product_uom_qty")
    def _check_qty_not_below_ordered(self):
        for line in self:
            if float_compare(line.product_uom_qty, line.qty_ordered,
                             precision_rounding=line.product_uom.rounding or 0.01) < 0:
                raise ValidationError(_(
                    "The agreed quantity of %(product)s (%(agreed)s) cannot be lower than the quantity "
                    "already ordered (%(ordered)s).",
                    product=line.product_id.display_name,
                    agreed=formatLang(self.env, line.product_uom_qty, dp="Product Unit of Measure"),
                    ordered=formatLang(self.env, line.qty_ordered, dp="Product Unit of Measure")))

    def name_get(self):
        return [(line.id, "%s - %s" % (line.agreement_id.name, line.product_id.display_name)) for line in self]

    # ------------------------------------------------------------------
    # Lock: lines change only while the agreement is a draft or rejected
    # ------------------------------------------------------------------
    def _check_agreement_editable(self, agreements):
        for agreement in agreements:
            if agreement.state not in EDITABLE_STATES:
                raise UserError(_(
                    "The lines of agreement %(agreement)s can no longer be changed: it is %(state)s. "
                    "Only draft or rejected agreements can be edited.",
                    agreement=agreement.name,
                    state=dict(agreement._fields["state"]._description_selection(self.env))[agreement.state]))

    @api.model_create_multi
    def create(self, vals_list):
        agreement_ids = {vals.get("agreement_id") for vals in vals_list if vals.get("agreement_id")}
        self._check_agreement_editable(self.env["blanket.sale.agreement"].browse(agreement_ids))
        return super().create(vals_list)

    def write(self, vals):
        if set(vals) - UNLOCKED_LINE_FIELDS:
            agreements = self.agreement_id
            if "agreement_id" in vals:
                agreements |= self.env["blanket.sale.agreement"].browse(vals["agreement_id"])
            self._check_agreement_editable(agreements)
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_editable(self):
        self._check_agreement_editable(self.agreement_id)
        if self.sudo().sale_line_ids:
            raise UserError(_(
                "%s is used on sales orders and cannot be removed from the agreement.",
                ", ".join(self.mapped("product_id.display_name"))))

    def _prepare_sale_order_line_vals(self, quantity):
        """Values of a quotation line. Quotations are created in the agreement currency
        (see ``_get_quotation_pricelist``), so the agreed price is used as it is."""
        self.ensure_one()
        return {
            "product_id": self.product_id.id,
            "name": self.name,
            "product_uom_qty": quantity,
            "product_uom": self.product_uom.id,
            "price_unit": self.price_unit,
            "discount": self.discount,
            "tax_id": [(6, 0, self.tax_ids.ids)],
            "blanket_line_id": self.id,
        }
