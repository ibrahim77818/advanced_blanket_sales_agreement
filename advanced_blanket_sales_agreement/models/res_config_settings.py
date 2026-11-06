# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    group_blanket_sale_agreement = fields.Boolean(
        string="Blanket Sales Agreements",
        implied_group="advanced_blanket_sales_agreement.group_blanket_sale_agreement",
        help="Manage long-term blanket sales agreements with approval cycles, "
             "delivery schedules and automated customer notifications.",
    )
    blanket_approval_template_id = fields.Many2one(
        related="company_id.blanket_approval_template_id",
        readonly=False,
    )
    blanket_reminder_days = fields.Integer(
        related="company_id.blanket_reminder_days",
        readonly=False,
    )
    blanket_segregation_of_duties = fields.Boolean(
        related="company_id.blanket_segregation_of_duties",
        readonly=False,
    )
    blanket_default_terms = fields.Html(
        related="company_id.blanket_default_terms",
        readonly=False,
    )
