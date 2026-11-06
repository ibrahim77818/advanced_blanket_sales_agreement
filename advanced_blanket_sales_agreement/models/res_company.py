# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    blanket_approval_template_id = fields.Many2one(
        "blanket.agreement.approval.template",
        string="Default Agreement Approval Template",
        domain="[('company_id', 'in', [id, False])]",
    )
    blanket_reminder_days = fields.Integer(
        string="Agreement Expiry Reminder (Days)",
        default=30,
        help="Default number of days before the agreement end date on which "
             "the customer receives a pre-expiration reminder.",
    )
    blanket_segregation_of_duties = fields.Boolean(
        string="Agreement Segregation of Duties",
        default=True,
        help="Approvers cannot approve their own agreements, and one person cannot approve "
             "more than one tier of the same submission.",
    )
    blanket_default_terms = fields.Html(
        string="Default Agreement Terms & Conditions",
        translate=True,
        sanitize=True,
    )
