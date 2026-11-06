# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase, new_test_user

GROUP = "advanced_blanket_sales_agreement.group_blanket_sale_agreement"


class BlanketAgreementCommon(TransactionCase):
    """Shared fixtures; holds no tests itself."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.lang"]._activate_lang("en_US")
        cls.env = cls.env(context=dict(cls.env.context, lang="en_US", tracking_disable=False, mail_create_nolog=True))
        cls.today = fields.Date.context_today(cls.env.user)
        sales = "base.group_user,sales_team.group_sale_salesman," + GROUP
        cls.responsible = new_test_user(cls.env, "bsa_responsible", groups=sales, email="resp@example.com")
        cls.approver_finance = new_test_user(cls.env, "bsa_finance", groups="base.group_user," + GROUP,
                                             email="finance@example.com")
        cls.approver_director = new_test_user(
            cls.env, "bsa_director", groups="base.group_user,sales_team.group_sale_manager," + GROUP,
            email="director@example.com")
        cls.outsider = new_test_user(cls.env, "bsa_outsider", groups=sales, email="out@example.com")
        (cls.responsible | cls.approver_finance | cls.approver_director | cls.outsider).lang = "en_US"

        cls.role_finance = cls.env["blanket.agreement.approval.role"].create({
            "name": "Finance Controller", "user_ids": [Command.set(cls.approver_finance.ids)]})
        cls.template = cls.env["blanket.agreement.approval.template"].create({
            "name": "Two-tier approval",
            "line_ids": [
                Command.create({"sequence": 1, "name": "Finance", "approver_type": "role",
                                "role_id": cls.role_finance.id}),
                Command.create({"sequence": 2, "name": "Director", "approver_type": "user",
                                "user_id": cls.approver_director.id}),
            ],
        })
        cls.env.company.blanket_approval_template_id = cls.template
        cls.tax = cls.env["account.tax"].create({
            "name": "VAT 15% BSA", "amount": 15.0, "amount_type": "percent", "type_tax_use": "sale",
            "country_id": (cls.env.company.account_fiscal_country_id or cls.env.ref("base.sa")).id})
        cls.product = cls.env["product.product"].create({
            "name": "Steel Coil", "type": "consu", "list_price": 100.0,
            "taxes_id": [Command.set(cls.tax.ids)]})
        cls.product2 = cls.env["product.product"].create({
            "name": "Galvanized Sheet", "type": "consu", "list_price": 50.0,
            "taxes_id": [Command.set(cls.tax.ids)]})
        cls.customer = cls.env["res.partner"].create({"name": "Gulf Trading Co.", "email": "buyer@gulf.example",
                                                    "lang": "en_US"})

    # ------------------------------------------------------------------
    def _create_agreement(self, **kwargs):
        vals = {
            "partner_id": self.customer.id,
            "date_start": self.today - timedelta(days=10),
            "date_end": self.today + timedelta(days=180),
            "reminder_days": 30,
            "line_ids": [Command.create({"product_id": self.product.id, "product_uom_qty": 10.0})],
        }
        vals.update(kwargs)
        return self.env["blanket.sale.agreement"].with_user(self.responsible).create(vals).sudo(False)

    def _approval_activities(self, agreement, user=None):
        domain = [("res_model", "=", agreement._name), ("res_id", "=", agreement.id),
                  ("activity_type_id", "=", self.env.ref(
                      "advanced_blanket_sales_agreement.mail_activity_blanket_approval").id)]
        if user:
            domain.append(("user_id", "=", user.id))
        return self.env["mail.activity"].sudo().search(domain)

    def _approve_all(self, agreement):
        agreement.with_user(self.responsible).action_submit()
        agreement.with_user(self.approver_finance).action_approve()
        agreement.with_user(self.approver_director).action_approve()

    def _issued_agreement(self, **kwargs):
        agreement = self._create_agreement(**kwargs)
        self._approve_all(agreement)
        agreement.with_user(self.responsible).action_issue()
        return agreement

    def _customer_messages(self, agreement, subject_part):
        return agreement.message_ids.filtered(
            lambda m: self.customer in m.partner_ids and subject_part in (m.subject or ""))
