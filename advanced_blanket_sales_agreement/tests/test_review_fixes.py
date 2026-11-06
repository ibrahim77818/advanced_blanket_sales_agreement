# -*- coding: utf-8 -*-
"""Regression tests for the issues found in the functional review of 17.0.1.0.0 / 16.0.1.0.1.

Each test is numbered after the review item it covers.
"""
from datetime import timedelta

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged

from .common import BlanketAgreementCommon

LOCKED = UserError  # base class of AccessError and ValidationError


@tagged("post_install", "-at_install", "blanket_agreement")
class TestReviewFixes(BlanketAgreementCommon):

    def _quote(self, agreement, user=None):
        action = agreement.with_user(user or self.responsible).action_create_quotation()
        return self.env["sale.order"].browse(action["res_id"])

    def _confirmed_order(self, agreement, qty):
        order = self._quote(agreement)
        order.order_line.product_uom_qty = qty
        order.with_user(self.responsible).action_confirm()
        return order

    # ---------------------------------------------------------------- 1
    def test_r01_negotiated_price_survives_quantity_and_pricelist_changes(self):
        agreement = self._create_agreement()
        line = agreement.line_ids
        line.price_unit = 80.0
        line.product_uom_qty = 25.0
        self.assertEqual(line.price_unit, 80.0, "Changing the quantity must keep the negotiated price")
        vip = self.env["product.pricelist"].create({
            "name": "VIP", "currency_id": agreement.currency_id.id,
            "item_ids": [Command.create({"compute_price": "percentage", "percent_price": 10,
                                         "applied_on": "3_global"})]})
        agreement.pricelist_id = vip
        self.assertEqual(line.price_unit, 80.0, "Changing the pricelist must keep the negotiated price")
        with Form(agreement.with_user(self.responsible)) as form:
            with form.line_ids.edit(0) as form_line:
                form_line.product_uom_qty = 40
                self.assertEqual(form_line.price_unit, 80.0)
        # positive control: choosing a product still fills its price
        with Form(agreement.with_user(self.responsible)) as form:
            with form.line_ids.new() as form_line:
                form_line.product_id = self.product2
                self.assertEqual(form_line.price_unit, 45.0, "VIP list: 50 - 10%")

    # ---------------------------------------------------------------- 2
    def test_r02_confirmed_line_quantity_is_checked_against_agreement(self):
        agreement = self._issued_agreement()  # 10 agreed
        order = self._confirmed_order(agreement, 5)
        with self.assertRaises(ValidationError):
            order.order_line.with_user(self.responsible).product_uom_qty = 20
        order.order_line.with_user(self.responsible).product_uom_qty = 8   # within the agreement
        self.assertEqual(agreement.line_ids.qty_ordered, 8.0)
        with self.assertRaises(ValidationError):  # extra linked line on the confirmed order
            order.with_user(self.responsible).write({"order_line": [Command.create(
                agreement.line_ids._prepare_sale_order_line_vals(5))]})
        self.assertEqual(agreement.line_ids.qty_ordered, 8.0)
        self.assertLessEqual(agreement.consumption_percent, 100.0)
        with self.assertRaises(ValidationError):  # price changed after confirmation
            order.order_line.with_user(self.responsible).price_unit = 1.0

    # ---------------------------------------------------------------- 3
    def test_r03_quotation_currency_follows_agreement(self):
        company_currency = self.env.company.currency_id
        other = self.env.ref("base.EUR") if company_currency != self.env.ref("base.EUR") else self.env.ref("base.USD")
        other.active = True
        self.env["res.currency.rate"].search([("currency_id", "=", other.id)]).unlink()
        self.env["res.currency.rate"].create({"currency_id": other.id, "company_id": self.env.company.id,
                                               "name": self.today - timedelta(days=30), "rate": 0.5})
        foreign = self.env["product.pricelist"].create({"name": "Foreign", "currency_id": other.id})
        home = self.env["product.pricelist"].create({"name": "Home", "currency_id": company_currency.id})
        self.customer.property_product_pricelist = foreign

        # created through the API without a pricelist: it takes the customer's, like the form
        api_agreement = self._create_agreement()
        self.assertEqual(api_agreement.pricelist_id, foreign)
        self.assertEqual(api_agreement.currency_id, other)

        # agreement without any pricelist (legacy data): quotation stays in the agreement currency
        legacy = self._create_agreement(line_ids=[Command.create({
            "product_id": self.product.id, "product_uom_qty": 10, "price_unit": 60.0})])
        legacy.pricelist_id = False
        self.assertEqual(legacy.currency_id, company_currency)
        self._approve_all(legacy)
        legacy.with_user(self.responsible).action_issue()
        order = self._quote(legacy)
        self.assertEqual(order.currency_id, legacy.currency_id)
        self.assertIn(order.pricelist_id, home | self.env["product.pricelist"].search(
            [("currency_id", "=", company_currency.id)]))
        self.assertEqual(order.order_line.price_unit, 60.0, "No conversion: same currency as the agreement")

        # an order in another currency cannot be confirmed against the agreement
        order.pricelist_id = foreign
        order.order_line.price_unit = 60.0
        with self.assertRaises(ValidationError):
            order.with_user(self.responsible).action_confirm()

    # ---------------------------------------------------------------- 4 + 9
    def test_r04_agreement_locked_on_server_once_submitted(self):
        agreement = self._create_agreement()
        line = agreement.line_ids
        agreement.with_user(self.responsible).action_submit()
        as_resp = agreement.with_user(self.responsible)
        for vals in ({"date_end": self.today + timedelta(days=400)},
                     {"partner_id": self.env["res.partner"].create({"name": "Other"}).id},
                     {"terms_conditions": "<p>new terms</p>"},
                     {"line_ids": [Command.update(line.id, {"price_unit": 45.9})]},
                     {"line_ids": [Command.create({"product_id": self.product2.id, "product_uom_qty": 1})]}):
            with self.assertRaises(LOCKED, msg="%s must be locked while waiting approval" % list(vals)):
                as_resp.write(vals)
        with self.assertRaises(LOCKED):
            line.with_user(self.responsible).price_unit = 45.9
        with self.assertRaises(LOCKED):
            line.with_user(self.responsible).unlink()
        as_resp.note = "internal notes stay editable"

        agreement.with_user(self.approver_finance).action_approve()
        agreement.with_user(self.approver_director).action_approve()
        as_resp.action_issue()
        total = agreement.amount_total
        for vals in ({"terms_conditions": "<p>changed after issue</p>"},  # review item 9
                     {"governing_law": "Other law"},
                     {"line_ids": [Command.delete(line.id)]}):
            with self.assertRaises(LOCKED, msg="%s must be locked once issued" % list(vals)):
                as_resp.write(vals)
        with self.assertRaises(LOCKED):
            line.with_user(self.responsible).unlink()
        self.assertTrue(line.exists())
        self.assertEqual(agreement.amount_total, total)
        as_resp.date_end = agreement.date_end + timedelta(days=30)  # extension stays allowed

    # ---------------------------------------------------------------- 5
    def test_r05_agreed_quantity_cannot_drop_below_ordered(self):
        agreement = self._issued_agreement()
        self._confirmed_order(agreement, 7)
        with self.assertRaises(LOCKED):
            agreement.line_ids.with_user(self.responsible).product_uom_qty = 3
        self.assertEqual(agreement.line_ids.product_uom_qty, 10.0)
        with self.assertRaises(LOCKED):  # the order line keeps its agreement line
            agreement.line_ids.sudo().unlink()

    def test_r05b_line_used_on_orders_cannot_be_deleted(self):
        agreement = self._issued_agreement(line_ids=[
            Command.create({"product_id": self.product.id, "product_uom_qty": 10}),
            Command.create({"product_id": self.product2.id, "product_uom_qty": 5})])
        quote = self._quote(agreement)
        quote.with_user(self.responsible)._action_cancel()
        agreement.with_user(self.responsible).action_cancel()
        agreement.with_user(self.responsible).action_draft()
        used = agreement.line_ids.filtered(lambda line: line.product_id == self.product)
        with self.assertRaises(UserError):
            used.with_user(self.responsible).unlink()
        self.assertTrue(used.exists())

    # ---------------------------------------------------------------- 6
    def test_r06_schedule_with_confirmed_order_cannot_be_deleted(self):
        agreement = self._issued_agreement()
        Schedule = self.env["blanket.sale.agreement.schedule"].with_user(self.responsible)
        planned = Schedule.create({"agreement_id": agreement.id, "line_id": agreement.line_ids.id,
                                   "scheduled_date": self.today + timedelta(days=3), "quantity": 1})
        ordered = Schedule.create({"agreement_id": agreement.id, "line_id": agreement.line_ids.id,
                                   "scheduled_date": self.today + timedelta(days=9), "quantity": 2})
        ordered.action_create_quotation()
        ordered.sale_order_id.with_user(self.responsible).action_confirm()
        with self.assertRaises(UserError):
            ordered.unlink()
        with self.assertRaises(UserError):
            ordered.quantity = 4
        planned.unlink()  # still allowed
        self.assertTrue(ordered.exists())

    # ---------------------------------------------------------------- 7
    def test_r07_schedule_line_must_belong_to_its_agreement(self):
        first = self._issued_agreement()
        second = self._issued_agreement()
        with self.assertRaises(ValidationError):
            self.env["blanket.sale.agreement.schedule"].create({
                "agreement_id": second.id, "line_id": first.line_ids.id,
                "scheduled_date": self.today + timedelta(days=5), "quantity": 1})

    # ---------------------------------------------------------------- 8
    def test_r08_past_agreements_cannot_be_submitted_or_issued(self):
        past = self._create_agreement(date_start=self.today - timedelta(days=60),
                                      date_end=self.today - timedelta(days=1))
        with self.assertRaises(UserError):
            past.with_user(self.responsible).action_submit()
        agreement = self._create_agreement(date_end=self.today + timedelta(days=1))
        self._approve_all(agreement)
        agreement.sudo().date_end = self.today - timedelta(days=1)  # time passes while it waits
        with self.assertRaises(UserError):
            agreement.with_user(self.responsible).action_issue()
        agreement.with_user(self.responsible).date_end = self.today + timedelta(days=30)
        agreement.with_user(self.responsible).action_issue()
        self.assertEqual(agreement.state, "issued")

    # ---------------------------------------------------------------- 10
    def test_r10_order_price_must_match_agreement(self):
        agreement = self._issued_agreement()
        order = self._quote(agreement)
        self.assertEqual(order.order_line.price_unit, 100.0)
        order.order_line.price_unit = 1.0
        with self.assertRaises(ValidationError):
            order.with_user(self.responsible).action_confirm()
        order.order_line.discount = 99.0
        order.order_line.price_unit = 100.0
        with self.assertRaises(ValidationError):
            order.with_user(self.responsible).action_confirm()
        # a Sales Administrator may confirm a deviating price; it is logged on the order
        order.with_user(self.approver_director).action_confirm()
        self.assertEqual(order.state, "sale")
        self.assertTrue(order.message_ids.filtered(lambda m: "agreed" in (m.body or "")))
        # a linked line added by hand gets the agreed price and discount
        manual = self.env["sale.order"].create({
            "partner_id": self.customer.id, "blanket_agreement_id": agreement.id,
            "order_line": [Command.create({"product_id": self.product.id, "product_uom_qty": 1,
                                          "blanket_line_id": agreement.line_ids.id})]})
        self.assertEqual(manual.order_line.price_unit, 100.0)
        self.assertEqual(manual.order_line.discount, 0.0)

    # ---------------------------------------------------------------- 11
    def test_r11_create_quotation_skips_quantities_already_quoted(self):
        agreement = self._issued_agreement()
        first = self._quote(agreement)
        self.assertEqual(agreement.line_ids.qty_quoted, 10.0)
        with self.assertRaises(UserError):
            self._quote(agreement)
        first.order_line.product_uom_qty = 6
        second = self._quote(agreement)
        self.assertEqual(second.order_line.product_uom_qty, 4.0)
        first.with_user(self.responsible)._action_cancel()
        third = self._quote(agreement)
        self.assertEqual(third.order_line.product_uom_qty, 6.0)

    # ---------------------------------------------------------------- 12
    def test_r12_agreement_line_requires_the_order_to_be_linked(self):
        agreement = self._issued_agreement()
        with self.assertRaises(ValidationError):
            self.env["sale.order"].create({
                "partner_id": self.customer.id,
                "order_line": [Command.create({"product_id": self.product.id, "product_uom_qty": 2,
                                              "blanket_line_id": agreement.line_ids.id})]})
        order = self._quote(agreement)
        with self.assertRaises(ValidationError):
            order.blanket_agreement_id = False

    # ---------------------------------------------------------------- 13
    def test_r13_product_only_once_per_agreement(self):
        with self.assertRaises(ValidationError):
            self._create_agreement(line_ids=[
                Command.create({"product_id": self.product.id, "product_uom_qty": 1}),
                Command.create({"product_id": self.product.id, "product_uom_qty": 2})])

    # ---------------------------------------------------------------- 14
    def test_r14_resubmission_keeps_the_approval_history(self):
        agreement = self._create_agreement()
        agreement.with_user(self.responsible).action_submit()
        agreement.with_user(self.approver_finance)._do_reject("Price too low")
        agreement.with_user(self.responsible).action_submit()
        history = agreement.approval_ids.filtered("superseded")
        current = agreement.approval_ids - history
        self.assertEqual(history.mapped("state"), ["rejected", "waiting"])
        self.assertEqual(history[0].comment, "Price too low")
        self.assertEqual(set(history.mapped("cycle")), {1})
        self.assertEqual(current.mapped("state"), ["pending", "waiting"])
        self.assertEqual(set(current.mapped("cycle")), {2})
        agreement.with_user(self.approver_finance).action_approve()
        agreement.with_user(self.approver_director).action_approve()
        self.assertEqual(agreement.state, "approved")

    # ---------------------------------------------------------------- 15
    def test_r15_segregation_of_duties(self):
        agreement = self._create_agreement()
        agreement.with_user(self.responsible).action_submit()
        pending = agreement.current_approval_id
        # approval rows are written by the workflow only, even for Sales Administrators
        with self.assertRaises(AccessError):
            pending.with_user(self.approver_director).write({"approver_ids": [Command.link(self.approver_director.id)]})
        # adding yourself to the role after submission does not make you an approver of the open tier
        self.role_finance.user_ids = [Command.link(self.approver_director.id)]
        with self.assertRaises(AccessError):
            agreement.with_user(self.approver_director).action_approve()

        # one person cannot approve two tiers when someone else could have taken the first one
        self.role_finance.user_ids = [Command.link(self.approver_director.id)]
        second = self._create_agreement()
        second.with_user(self.responsible).action_submit()
        with self.assertRaises(UserError):
            second.with_user(self.approver_director).action_approve()
        second.with_user(self.approver_finance).action_approve()
        second.with_user(self.approver_director).action_approve()
        self.assertEqual(second.state, "approved")

        # the responsible user never approves their own agreement
        self.role_finance.user_ids = [Command.link(self.responsible.id)]
        own = self._create_agreement()
        own.with_user(self.responsible).action_submit()
        self.assertNotIn(self.responsible, own.current_approval_id.approver_ids)
        self.role_finance.user_ids = [Command.set(self.responsible.ids)]
        lonely = self._create_agreement()
        with self.assertRaises(UserError):
            lonely.with_user(self.responsible).action_submit()

        # companies that do not want segregation can switch it off
        self.env.company.blanket_segregation_of_duties = False
        lonely.with_user(self.responsible).action_submit()
        lonely.with_user(self.responsible).action_approve()
        self.assertEqual(lonely.current_approval_id.name, "Director")

    # ---------------------------------------------------------------- 16
    def test_r16_lines_schedules_and_approvals_follow_document_access(self):
        agreement = self._issued_agreement()
        self.env["blanket.sale.agreement.schedule"].create({
            "agreement_id": agreement.id, "line_id": agreement.line_ids.id,
            "scheduled_date": self.today + timedelta(days=5), "quantity": 1})
        domain = [("agreement_id", "=", agreement.id)]
        for model in ("blanket.sale.agreement.line", "blanket.sale.agreement.schedule",
                      "blanket.sale.agreement.approval"):
            self.assertTrue(self.env[model].with_user(self.responsible).search(domain), model)
            self.assertFalse(self.env[model].with_user(self.outsider).search(domain),
                             "%s of another salesperson's agreement must be hidden" % model)
            self.assertTrue(self.env[model].with_user(self.approver_director).search(domain), model)

        other_company = self.env["res.company"].create({"name": "Second Co", "currency_id": self.env.company.currency_id.id})
        foreign = self.env["blanket.sale.agreement"].sudo().create({
            "partner_id": self.customer.id, "company_id": other_company.id, "pricelist_id": False,
            "approval_template_id": False, "payment_term_id": False,
            "date_start": self.today, "date_end": self.today + timedelta(days=30),
            "line_ids": [Command.create({"product_id": self.product2.id, "product_uom_qty": 3})]})
        self.env["blanket.sale.agreement.schedule"].sudo().create({
            "agreement_id": foreign.id, "line_id": foreign.line_ids.id,
            "scheduled_date": self.today + timedelta(days=2), "quantity": 1})
        self.env["blanket.sale.agreement.approval"].sudo().create({
            "agreement_id": foreign.id, "name": "Tier", "cycle": 1})
        domain = [("agreement_id", "=", foreign.id)]
        for model in ("blanket.sale.agreement.line", "blanket.sale.agreement.schedule",
                      "blanket.sale.agreement.approval"):
            self.assertFalse(self.env[model].with_user(self.approver_director).search(domain),
                             "%s of a company the user cannot access must be hidden" % model)
