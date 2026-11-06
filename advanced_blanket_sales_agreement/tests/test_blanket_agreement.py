# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged

from .common import GROUP, BlanketAgreementCommon


@tagged("post_install", "-at_install", "blanket_agreement")
class TestBlanketAgreement(BlanketAgreementCommon):

    def test_01_settings_toggle(self):
        group = self.env.ref(GROUP)
        user_group = self.env.ref("base.group_user")
        self.assertNotIn(group, user_group.implied_ids, "Feature must be disabled by default")
        settings = self.env["res.config.settings"].create({})
        self.assertFalse(settings.group_blanket_sale_agreement)
        settings.group_blanket_sale_agreement = True
        settings.execute()
        self.assertIn(group, user_group.implied_ids)
        self.assertTrue(self.env["res.config.settings"].create({}).group_blanket_sale_agreement)

    def test_02_creation_defaults_and_amounts(self):
        agreement = self._create_agreement()
        self.assertTrue(agreement.name.startswith("BSA/"))
        self.assertEqual(agreement.user_id, self.responsible, "Creator is the responsible user")
        self.assertEqual(agreement.approval_template_id, self.template)
        self.assertEqual(agreement.state, "draft")
        line = agreement.line_ids
        self.assertEqual(line.price_unit, 100.0)
        self.assertEqual(line.tax_ids, self.tax)
        self.assertAlmostEqual(agreement.amount_untaxed, 1000.0)
        self.assertAlmostEqual(agreement.amount_tax, 150.0)
        self.assertAlmostEqual(agreement.amount_total, 1150.0)
        self.assertEqual(agreement.reminder_date, agreement.date_end - timedelta(days=30))

    def test_03_multi_tier_approval_cycle(self):
        agreement = self._create_agreement()
        agreement.with_user(self.responsible).action_submit()
        self.assertEqual(agreement.state, "to_approve")
        self.assertEqual(agreement.approval_ids.mapped("state"), ["pending", "waiting"])
        self.assertEqual(len(self._approval_activities(agreement, self.approver_finance)), 1)
        self.assertFalse(self._approval_activities(agreement, self.approver_director),
                         "Second tier must not be notified before the first one approves")

        # Only designated approvers of the current tier may decide
        with self.assertRaises(AccessError):
            agreement.with_user(self.outsider).action_approve()
        with self.assertRaises(AccessError):
            agreement.with_user(self.approver_director).action_approve()
        self.assertTrue(agreement.with_user(self.approver_finance).can_approve)
        # An approver without any Sales rights must be able to open the agreement form
        approver_form = Form(agreement.with_user(self.approver_finance))
        self.assertEqual(approver_form.state, "to_approve")
        self.assertEqual(agreement.with_user(self.approver_finance).sale_order_count, 0)
        self.assertEqual(agreement.line_ids.with_user(self.approver_finance).qty_remaining, 10.0)
        own_activity = agreement.with_user(self.approver_finance).activity_ids
        self.assertEqual(own_activity.user_id, self.approver_finance,
                         "Read-only approvers see their approval activity on the agreement")
        self.assertFalse(agreement.with_user(self.approver_director).can_approve)

        agreement.with_user(self.approver_finance).action_approve()
        tier1, tier2 = agreement.approval_ids.sorted("sequence")
        self.assertEqual(tier1.state, "approved")
        self.assertEqual(tier1.user_id, self.approver_finance)
        self.assertEqual(tier2.state, "pending")
        self.assertFalse(self._approval_activities(agreement, self.approver_finance), "Activity marked done")
        done_msg = agreement.message_ids.filtered(
            lambda m: m.mail_activity_type_id == self.env.ref(
                "advanced_blanket_sales_agreement.mail_activity_blanket_approval"))
        self.assertTrue(done_msg, "Completing the activity logs it in the chatter")
        self.assertEqual(len(self._approval_activities(agreement, self.approver_director)), 1)

        agreement.with_user(self.approver_director).action_approve()
        self.assertEqual(agreement.state, "approved")
        self.assertTrue(agreement.approved_date)
        self.assertFalse(self._approval_activities(agreement))
        issue_type = self.env.ref("advanced_blanket_sales_agreement.mail_activity_blanket_issue")
        issue_activity = agreement.activity_ids.filtered(lambda a: a.activity_type_id == issue_type)
        self.assertEqual(issue_activity.user_id, self.responsible, "Responsible user is asked to issue")
        notified = agreement.message_ids.filtered(lambda m: self.responsible.partner_id in m.partner_ids)
        self.assertTrue(notified, "Responsible user is notified once all approvers signed off")

    def test_04_issue_sends_pdf_to_customer(self):
        agreement = self._create_agreement()
        self._approve_all(agreement)
        with self.assertRaises(AccessError):
            agreement.with_user(self.outsider).action_issue()
        agreement.with_user(self.responsible).action_issue()
        self.assertEqual(agreement.state, "issued")
        self.assertEqual(agreement.issue_date, self.today)
        self.assertFalse(agreement.activity_ids, "Issue activity is marked done")
        message = self._customer_messages(agreement, agreement.name)
        self.assertEqual(len(message), 1, "Formal agreement e-mailed to the customer")
        self.assertEqual(message.message_type, "comment")
        # 17.0: message_post_with_source defaults to an internal note; it must be a public comment
        self.assertEqual(message.subtype_id, self.env.ref("mail.mt_comment"))
        self.assertFalse(message.subtype_id.internal, "Customer e-mail must not be an internal note")
        self.assertTrue(message.attachment_ids, "Agreement PDF attached")
        self.assertIn(agreement.name.replace("/", "_"), message.attachment_ids.name)
        email_notif = message.sudo().notification_ids.filtered(
            lambda n: n.res_partner_id == self.customer and n.notification_type == "email")
        self.assertTrue(email_notif, "Customer is notified by e-mail")

    def test_05_issue_without_customer_email(self):
        self.customer.email = False
        agreement = self._create_agreement()
        self._approve_all(agreement)
        agreement.with_user(self.responsible).action_issue()
        self.assertEqual(agreement.state, "issued")
        self.assertFalse(self._customer_messages(agreement, agreement.name))
        self.assertTrue(agreement.message_ids.filtered(lambda m: "no e-mail address" in (m.body or "")))

    def test_06_reject_and_resubmit(self):
        agreement = self._create_agreement()
        agreement.with_user(self.responsible).action_submit()
        action = agreement.with_user(self.approver_finance).action_open_reject_wizard()
        wizard = self.env[action["res_model"]].with_user(self.approver_finance).with_context(
            action["context"]).create({"reason": "Prices below floor"})
        wizard.action_reject()
        self.assertEqual(agreement.state, "rejected")
        self.assertEqual(agreement.approval_ids[0].state, "rejected")
        self.assertEqual(agreement.approval_ids[0].comment, "Prices below floor")
        self.assertFalse(self._approval_activities(agreement))
        # Lines are editable again and agreement can be resubmitted
        agreement.with_user(self.responsible).write({"line_ids": [Command.update(
            agreement.line_ids.id, {"price_unit": 120.0})]})
        agreement.with_user(self.responsible).action_submit()
        self.assertEqual(agreement.state, "to_approve")
        self.assertEqual(agreement._active_approvals().mapped("state"), ["pending", "waiting"])
        self.assertEqual(agreement.approval_ids.filtered("superseded").mapped("state"), ["rejected", "waiting"])

    def test_07_no_template_is_approved_immediately(self):
        agreement = self._create_agreement(approval_template_id=False)
        self.env.company.blanket_approval_template_id = False
        agreement.with_user(self.responsible).action_submit()
        self.assertEqual(agreement.state, "approved")
        self.assertFalse(agreement.approval_ids)

    def test_08_submission_checks(self):
        empty_role = self.env["blanket.agreement.approval.role"].create({"name": "Nobody"})
        template = self.env["blanket.agreement.approval.template"].create({
            "name": "Broken", "line_ids": [Command.create({"name": "Ghost", "role_id": empty_role.id})]})
        agreement = self._create_agreement(approval_template_id=template.id)
        with self.assertRaises(UserError):
            agreement.with_user(self.responsible).action_submit()
        no_lines = self._create_agreement(line_ids=[])
        with self.assertRaises(UserError):
            no_lines.with_user(self.responsible).action_submit()
        with self.assertRaises(ValidationError):
            self._create_agreement(date_end=self.today - timedelta(days=20))
        with self.assertRaises(ValidationError):
            self.env["blanket.agreement.approval.template.line"].create({
                "template_id": template.id, "name": "No user", "approver_type": "user"})

    def test_09_cron_reminder_expiry_notice_and_expiration(self):
        agreement = self._issued_agreement(date_end=self.today + timedelta(days=40), reminder_days=30)
        cron_model = self.env["blanket.sale.agreement"]

        stats = cron_model._cron_process_expirations()
        self.assertFalse(agreement.reminder_sent, "Threshold date not reached yet")
        self.assertEqual(stats["reminders"], 0)

        agreement.date_end = self.today + timedelta(days=20)
        self.assertEqual(agreement.reminder_date, self.today - timedelta(days=10))
        cron_model._cron_process_expirations()
        self.assertTrue(agreement.reminder_sent)
        self.assertEqual(len(self._customer_messages(agreement, "Reminder")), 1)
        cron_model._cron_process_expirations()
        self.assertEqual(len(self._customer_messages(agreement, "Reminder")), 1, "Reminder sent only once")
        self.assertEqual(agreement.state, "issued")

        agreement.date_end = self.today
        cron_model._cron_process_expirations()
        self.assertTrue(agreement.expiry_notice_sent)
        self.assertEqual(len(self._customer_messages(agreement, "end date")), 1, "Final notice on end date")
        self.assertEqual(agreement.state, "issued", "Still valid on its last day")

        agreement.date_end = self.today - timedelta(days=1)
        stats = cron_model._cron_process_expirations()
        self.assertEqual(agreement.state, "expired")
        self.assertEqual(stats["expired"], 1)
        self.assertEqual(len(self._customer_messages(agreement, "end date")), 1, "Notice not duplicated")

    def test_10_cron_job_registered(self):
        cron = self.env.ref("advanced_blanket_sales_agreement.ir_cron_blanket_agreement_expiration")
        self.assertTrue(cron.active)
        self.assertEqual(cron.interval_type, "days")
        cron.method_direct_trigger()

    def test_11_quotation_and_quantity_consumption(self):
        draft = self._create_agreement()
        with self.assertRaises(UserError):
            draft.action_create_quotation()
        agreement = self._issued_agreement()
        action = agreement.with_user(self.responsible).action_create_quotation()
        order = self.env["sale.order"].browse(action["res_id"])
        self.assertEqual(order.blanket_agreement_id, agreement)
        self.assertEqual(order.order_line.blanket_line_id, agreement.line_ids)
        self.assertEqual(order.order_line.product_uom_qty, 10.0)
        self.assertEqual(order.order_line.price_unit, 100.0)
        order.order_line.product_uom_qty = 4.0
        order.action_confirm()
        line = agreement.line_ids
        self.assertEqual(line.qty_ordered, 4.0)
        self.assertEqual(line.qty_remaining, 6.0)
        self.assertEqual(agreement.sale_order_count, 1)
        self.assertAlmostEqual(agreement.consumption_percent, 40.0)

        second = agreement.with_user(self.responsible).action_create_quotation()
        second_order = self.env["sale.order"].browse(second["res_id"])
        self.assertEqual(second_order.order_line.product_uom_qty, 6.0)
        second_order.order_line.product_uom_qty = 7.0
        with self.assertRaises(ValidationError):
            second_order.action_confirm()

        with self.assertRaises(UserError):
            agreement.action_cancel()

    def test_12_delivery_schedules(self):
        agreement = self._issued_agreement()
        line = agreement.line_ids
        schedule = self.env["blanket.sale.agreement.schedule"].create({
            "agreement_id": agreement.id, "line_id": line.id,
            "scheduled_date": self.today + timedelta(days=15), "quantity": 3.0})
        self.assertEqual(schedule.state, "planned")
        with self.assertRaises(ValidationError):
            self.env["blanket.sale.agreement.schedule"].create({
                "agreement_id": agreement.id, "line_id": line.id,
                "scheduled_date": self.today + timedelta(days=30), "quantity": 8.0})
        with self.assertRaises(ValidationError):
            self.env["blanket.sale.agreement.schedule"].create({
                "agreement_id": agreement.id, "line_id": line.id,
                "scheduled_date": agreement.date_end + timedelta(days=1), "quantity": 1.0})
        schedule.action_create_quotation()
        self.assertTrue(schedule.sale_order_id)
        self.assertEqual(schedule.state, "quoted")
        self.assertEqual(schedule.sale_order_id.order_line.product_uom_qty, 3.0)
        self.assertEqual(schedule.sale_order_id.commitment_date.date(), schedule.scheduled_date)
        schedule.sale_order_id.action_confirm()
        self.assertEqual(schedule.state, "ordered")
        with self.assertRaises(UserError):
            schedule.action_create_quotation()

    def test_13_confirm_requires_issued_agreement(self):
        agreement = self._issued_agreement()
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "blanket_agreement_id": agreement.id,
            "order_line": [Command.create({"product_id": self.product.id, "product_uom_qty": 2.0,
                                          "blanket_line_id": agreement.line_ids.id})],
        })
        agreement.date_end = self.today - timedelta(days=1)
        self.env["blanket.sale.agreement"]._cron_process_expirations()
        self.assertEqual(agreement.state, "expired")
        with self.assertRaises(ValidationError):
            order.action_confirm()

    def test_14_report_rendering_ltr_and_rtl(self):
        agreement = self._issued_agreement(governing_law="Laws of the Kingdom of Saudi Arabia")
        report = self.env.ref("advanced_blanket_sales_agreement.action_report_blanket_agreement")
        html = report._render_qweb_html(report.report_name, agreement.ids)[0].decode()
        self.assertIn(agreement.name, html)
        self.assertIn('dir="ltr"', html)
        self.assertIn("Laws of the Kingdom of Saudi Arabia", html)
        self.assertIn("Internal Approvals", html)

        self.env["res.lang"]._activate_lang("ar_001")
        self.customer.lang = "ar_001"
        html_ar = report._render_qweb_html(report.report_name, agreement.ids)[0].decode()
        self.assertIn('dir="rtl"', html_ar)

    def test_15_unlink_and_reset(self):
        agreement = self._issued_agreement()
        with self.assertRaises(UserError):
            agreement.unlink()
        draft = self._create_agreement()
        draft.with_user(self.responsible).action_submit()
        draft.with_user(self.responsible).action_cancel()
        self.assertEqual(draft.state, "cancel")
        self.assertFalse(self._approval_activities(draft))
        draft.with_user(self.responsible).action_draft()
        self.assertEqual(draft.state, "draft")
        self.assertFalse(draft._active_approvals(), "Reset to draft closes the open approval cycle")
        self.assertTrue(draft.approval_ids, "... and keeps it as history")
        draft.unlink()
        self.assertFalse(draft.exists())

    def test_16_form_onchange_flow(self):
        with Form(self.env["blanket.sale.agreement"].with_user(self.responsible)) as form:
            form.partner_id = self.customer
            form.date_end = self.today + timedelta(days=90)
            with form.line_ids.new() as line:
                line.product_id = self.product
                line.product_uom_qty = 5
        agreement = form.save()
        self.assertAlmostEqual(agreement.amount_untaxed, 500.0)
        self.assertEqual(agreement.line_ids.name, self.product.get_product_multiline_description_sale())

    def test_17_view_readonly_rules(self):
        """17.0 removed field-level `states=`; the locking now lives in the form view."""
        agreement = self._create_agreement()
        # positive control: everything is editable in draft
        with Form(agreement.with_user(self.responsible)) as form:
            form.partner_id = self.customer
            form.date_start = self.today
            form.approval_template_id = self.template
            with form.line_ids.new() as line:
                line.product_id = self.product2
        self.assertEqual(len(agreement.line_ids), 2)
        agreement.with_user(self.responsible).action_submit()
        form = Form(agreement.with_user(self.responsible))
        for field in ("partner_id", "date_start", "date_end", "approval_template_id"):
            with self.assertRaises(AssertionError, msg="%s must be read-only while waiting approval" % field):
                setattr(form, field, getattr(agreement, field))
        with self.assertRaises(AssertionError, msg="lines must be read-only while waiting approval"):
            form.line_ids.new()
        self._approve_all_from_submitted(agreement)
        agreement.with_user(self.responsible).action_issue()
        form = Form(agreement.with_user(self.responsible))
        form.date_end = agreement.date_end + timedelta(days=30)  # extending an issued agreement is allowed
        form.save()
        with self.assertRaises(AssertionError):
            form.partner_id = self.customer

    def _approve_all_from_submitted(self, agreement):
        agreement.with_user(self.approver_finance).action_approve()
        agreement.with_user(self.approver_director).action_approve()
