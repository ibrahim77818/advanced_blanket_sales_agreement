# -*- coding: utf-8 -*-
from odoo import fields, models


class BlanketAgreementRejectWizard(models.TransientModel):
    _name = "blanket.agreement.reject.wizard"
    _description = "Reject Blanket Sales Agreement"

    agreement_id = fields.Many2one("blanket.sale.agreement", required=True, ondelete="cascade")
    reason = fields.Text(string="Rejection Reason", required=True)

    def action_reject(self):
        self.ensure_one()
        self.agreement_id._do_reject(self.reason)
        return {"type": "ir.actions.act_window_close"}
