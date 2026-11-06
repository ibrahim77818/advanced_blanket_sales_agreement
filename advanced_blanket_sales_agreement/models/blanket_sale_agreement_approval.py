# -*- coding: utf-8 -*-
from odoo import fields, models


class BlanketSaleAgreementApproval(models.Model):
    """One approval tier of one submission. Rows are written by the workflow only (sudo);
    users have read access. Rows of earlier submissions are kept as history (superseded)."""
    _name = "blanket.sale.agreement.approval"
    _description = "Blanket Sales Agreement Approval Tier"
    _order = "agreement_id, cycle desc, sequence, id"

    agreement_id = fields.Many2one("blanket.sale.agreement", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="agreement_id.company_id", store=True, index=True)
    cycle = fields.Integer(string="Submission", default=1, readonly=True,
                           help="Number of the submission this decision belongs to.")
    superseded = fields.Boolean(readonly=True, help="Belongs to an earlier submission of the agreement.")
    sequence = fields.Integer(default=10)
    name = fields.Char(string="Tier", required=True)
    role_id = fields.Many2one("blanket.agreement.approval.role", string="Role", ondelete="set null")
    approver_ids = fields.Many2many(
        "res.users", "blanket_agreement_approval_users_rel", "approval_id", "user_id",
        string="Approvers",
    )
    state = fields.Selection(
        [("waiting", "Waiting"), ("pending", "Pending Approval"), ("approved", "Approved"),
         ("rejected", "Rejected")],
        default="waiting", required=True,
    )
    user_id = fields.Many2one("res.users", string="Decided By")
    date = fields.Datetime(string="Decision Date")
    comment = fields.Text()
