# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import format_date, formatLang

_logger = logging.getLogger(__name__)

ACTIVITY_APPROVAL = "advanced_blanket_sales_agreement.mail_activity_blanket_approval"
ACTIVITY_ISSUE = "advanced_blanket_sales_agreement.mail_activity_blanket_issue"

# States in which the agreement content can be edited freely.
EDITABLE_STATES = ("draft", "rejected")
# Business content the approvers signed off: frozen once the agreement leaves draft.
LOCKED_FIELDS = {
    "partner_id", "client_order_ref", "pricelist_id", "payment_term_id", "incoterm_id",
    "date_start", "date_end", "approval_template_id", "company_id", "line_ids", "schedule_ids",
    "terms_conditions", "governing_law", "dispute_resolution", "termination_notice_days",
}
# Still editable once approved / issued: extending the end date and planning deliveries.
POST_APPROVAL_FIELDS = {"date_end", "schedule_ids"}


def has_distinct_approvers(candidates):
    """True when every tier can be given a different approver.

    ``candidates`` is a list with, per tier, the set of user ids allowed to approve it.
    Bipartite matching (Kuhn's algorithm); approval templates are small.
    """
    owner = {}

    def assign(tier, seen):
        for user in candidates[tier]:
            if user in seen:
                continue
            seen.add(user)
            if user not in owner or assign(owner[user], seen):
                owner[user] = tier
                return True
        return False

    return all(assign(tier, set()) for tier in range(len(candidates)))


class BlanketSaleAgreement(models.Model):
    _name = "blanket.sale.agreement"
    _description = "Blanket Sales Agreement"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start desc, id desc"
    _check_company_auto = True
    # Approvers only have read access: let them see their activities and comment.
    _mail_post_access = "read"

    name = fields.Char(
        string="Agreement Reference", required=True, copy=False, readonly=True,
        index="trigram", default=lambda self: _("New"),
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("to_approve", "Waiting Approval"),
            ("approved", "Approved"),
            ("issued", "Issued"),
            ("expired", "Expired"),
            ("rejected", "Rejected"),
            ("cancel", "Cancelled"),
        ],
        default="draft", required=True, readonly=True, copy=False, tracking=True, index=True,
    )
    partner_id = fields.Many2one(
        "res.partner", string="Customer", required=True, tracking=True, index=True,
        check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    client_order_ref = fields.Char(string="Customer Reference", copy=False)
    user_id = fields.Many2one(
        "res.users", string="Responsible", required=True, readonly=True, tracking=True,
        default=lambda self: self.env.user, copy=False, index=True,
        help="User who created the agreement. They are notified once all "
             "approvers sign off and are in charge of issuing it.",
    )
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True,
        default=lambda self: self.env.company,
    )
    pricelist_id = fields.Many2one(
        "product.pricelist", string="Pricelist", check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Fills the agreed prices when a product is chosen and sets the agreement currency. "
             "Defaults to the customer's pricelist.",
    )
    currency_id = fields.Many2one(
        "res.currency", compute="_compute_currency_id", store=True, precompute=True,
    )
    payment_term_id = fields.Many2one(
        "account.payment.term", string="Payment Terms", check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    incoterm_id = fields.Many2one("account.incoterms", string="Incoterm")
    date_start = fields.Date(
        string="Start Date", required=True, tracking=True,
        default=fields.Date.context_today,
    )
    date_end = fields.Date(
        string="End Date", required=True, tracking=True,
    )
    reminder_days = fields.Integer(
        string="Reminder Before Expiry (Days)",
        default=lambda self: self.env.company.blanket_reminder_days,
        help="Number of days before the end date on which the customer "
             "receives the pre-expiration reminder. 0 disables the reminder.",
    )
    reminder_date = fields.Date(
        compute="_compute_reminder_date", store=True,
        help="Date on which the pre-expiration reminder is e-mailed to the customer.",
    )
    days_to_expiry = fields.Integer(compute="_compute_days_to_expiry")

    approval_template_id = fields.Many2one(
        "blanket.agreement.approval.template", string="Approval Template",
        check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        default=lambda self: self.env.company.blanket_approval_template_id,
    )
    approval_ids = fields.One2many(
        "blanket.sale.agreement.approval", "agreement_id", string="Approval Trail",
        copy=False, readonly=True,
    )
    current_approval_id = fields.Many2one(
        "blanket.sale.agreement.approval", compute="_compute_current_approval",
    )
    current_approver_ids = fields.Many2many(
        "res.users", compute="_compute_current_approval", string="Current Approvers",
    )
    can_approve = fields.Boolean(compute="_compute_current_approval")
    submitted_date = fields.Datetime(readonly=True, copy=False)
    approved_date = fields.Datetime(readonly=True, copy=False)
    issue_date = fields.Date(readonly=True, copy=False, tracking=True)

    line_ids = fields.One2many(
        "blanket.sale.agreement.line", "agreement_id", string="Order Lines", copy=True,
    )
    schedule_ids = fields.One2many(
        "blanket.sale.agreement.schedule", "agreement_id", string="Delivery Schedules",
        copy=True,
    )

    # Legal terms
    terms_conditions = fields.Html(
        string="Terms & Conditions", sanitize=True,
        default=lambda self: self.env.company.blanket_default_terms,
    )
    governing_law = fields.Char(string="Governing Law")
    dispute_resolution = fields.Char(string="Dispute Resolution")
    termination_notice_days = fields.Integer(string="Termination Notice (Days)", default=30)
    note = fields.Text(string="Internal Notes")

    amount_untaxed = fields.Monetary(string="Untaxed Amount", compute="_compute_amounts", store=True, tracking=True)
    amount_tax = fields.Monetary(string="Taxes", compute="_compute_amounts", store=True)
    amount_total = fields.Monetary(string="Total", compute="_compute_amounts", store=True)
    amount_ordered = fields.Monetary(
        string="Ordered Amount", compute="_compute_consumption",
        help="Untaxed amount of confirmed sales orders placed against this agreement.",
    )
    consumption_percent = fields.Float(string="Consumed (%)", compute="_compute_consumption")

    sale_order_ids = fields.One2many("sale.order", "blanket_agreement_id", string="Sales Orders")
    sale_order_count = fields.Integer(compute="_compute_sale_order_count")

    reminder_sent = fields.Boolean(readonly=True, copy=False)
    expiry_notice_sent = fields.Boolean(readonly=True, copy=False)

    _sql_constraints = [
        ("name_company_uniq", "unique(name, company_id)", "Agreement reference must be unique per company!"),
        ("reminder_days_positive", "CHECK(reminder_days >= 0)", "Reminder days cannot be negative."),
    ]

    # ------------------------------------------------------------------
    # Computes & constraints
    # ------------------------------------------------------------------
    @api.depends("pricelist_id", "company_id")
    def _compute_currency_id(self):
        for agreement in self:
            agreement.currency_id = agreement.pricelist_id.currency_id or agreement.company_id.currency_id

    @api.depends("date_end", "reminder_days")
    def _compute_reminder_date(self):
        for agreement in self:
            if agreement.date_end and agreement.reminder_days > 0:
                agreement.reminder_date = agreement.date_end - timedelta(days=agreement.reminder_days)
            else:
                agreement.reminder_date = False

    def _compute_days_to_expiry(self):
        today = fields.Date.context_today(self)
        for agreement in self:
            agreement.days_to_expiry = (agreement.date_end - today).days if agreement.date_end else 0

    @api.depends("line_ids.price_subtotal", "line_ids.price_tax", "line_ids.price_total")
    def _compute_amounts(self):
        for agreement in self:
            agreement.amount_untaxed = sum(agreement.line_ids.mapped("price_subtotal"))
            agreement.amount_tax = sum(agreement.line_ids.mapped("price_tax"))
            agreement.amount_total = sum(agreement.line_ids.mapped("price_total"))

    @api.depends("line_ids.qty_ordered", "line_ids.price_unit", "amount_untaxed")
    def _compute_consumption(self):
        for agreement in self:
            ordered = sum(
                line.qty_ordered * line.price_unit * (1 - (line.discount or 0.0) / 100.0)
                for line in agreement.line_ids
            )
            agreement.amount_ordered = ordered
            agreement.consumption_percent = (
                ordered / agreement.amount_untaxed * 100.0 if agreement.amount_untaxed else 0.0
            )

    @api.depends("sale_order_ids")
    def _compute_sale_order_count(self):
        # sudo: approvers without Sales rights only need the count, not the orders
        data = self.env["sale.order"].sudo()._read_group(
            [("blanket_agreement_id", "in", self.ids)], ["blanket_agreement_id"], ["__count"],
        )
        counts = {agreement.id: count for agreement, count in data}
        for agreement in self:
            agreement.sale_order_count = counts.get(agreement.id, 0)

    @api.depends("approval_ids.state", "approval_ids.approver_ids", "approval_ids.superseded", "state")
    @api.depends_context("uid")
    def _compute_current_approval(self):
        for agreement in self:
            current = agreement._active_approvals().filtered(lambda a: a.state == "pending")[:1]
            agreement.current_approval_id = current
            agreement.current_approver_ids = current.approver_ids
            agreement.can_approve = bool(
                agreement.state == "to_approve" and current and self.env.user in current.approver_ids
            )

    @api.constrains("date_start", "date_end")
    def _check_dates(self):
        for agreement in self:
            if agreement.date_start and agreement.date_end and agreement.date_end < agreement.date_start:
                raise ValidationError(_("The agreement end date must be on or after its start date."))

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company = self.env["res.company"].browse(vals.get("company_id")) or self.env.company
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].with_company(company).next_by_code(
                    "blanket.sale.agreement") or _("New")
            # Same defaults as the form: agreements created by import or API get the customer's
            # pricelist, so their currency matches the quotations generated from them.
            if vals.get("partner_id"):
                partner = self.env["res.partner"].browse(vals["partner_id"]).with_company(company)
                if "pricelist_id" not in vals and partner.property_product_pricelist:
                    vals["pricelist_id"] = partner.property_product_pricelist.id
                if "payment_term_id" not in vals and partner.property_payment_term_id:
                    vals["payment_term_id"] = partner.property_payment_term_id.id
        return super().create(vals_list)

    def write(self, vals):
        self._check_locked_fields(vals)
        return super().write(vals)

    def _check_locked_fields(self, vals):
        """Server-side counterpart of the form's read-only rules."""
        if "approval_ids" in vals:
            raise UserError(_("The approval trail is maintained by the approval workflow and cannot be edited."))
        changed = set(vals) & LOCKED_FIELDS
        if not changed:
            return
        states = dict(self._fields["state"]._description_selection(self.env))
        for agreement in self:
            if agreement.state in EDITABLE_STATES:
                continue
            allowed = POST_APPROVAL_FIELDS if agreement.state in ("approved", "issued") else set()
            blocked = changed - allowed
            if blocked:
                labels = ", ".join(sorted(self._fields[name]._description_string(self.env) for name in blocked))
                raise UserError(_(
                    "Agreement %(agreement)s is %(state)s: %(fields)s can no longer be changed. "
                    "Only draft or rejected agreements can be edited.",
                    agreement=agreement.name, state=states[agreement.state], fields=labels))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_draft_or_cancel(self):
        for agreement in self:
            if agreement.state not in ("draft", "cancel", "rejected"):
                raise UserError(_("You can only delete draft, rejected or cancelled agreements."))

    @api.onchange("partner_id")
    def _onchange_partner_id(self):
        if self.partner_id:
            self.pricelist_id = self.partner_id.property_product_pricelist or self.pricelist_id
            self.payment_term_id = self.partner_id.property_payment_term_id or self.payment_term_id

    # ------------------------------------------------------------------
    # Approval workflow
    # ------------------------------------------------------------------
    def _active_approvals(self):
        """Approval rows of the current submission (earlier submissions are kept as history)."""
        return self.approval_ids.filtered(lambda approval: not approval.superseded)

    def _segregation_of_duties(self):
        self.ensure_one()
        return self.company_id.blanket_segregation_of_duties

    def _check_not_expired(self):
        today = fields.Date.context_today(self)
        for agreement in self:
            if agreement.date_end and agreement.date_end < today:
                raise UserError(_(
                    "Agreement %(agreement)s ended on %(date)s. Extend its end date first.",
                    agreement=agreement.name, date=format_date(self.env, agreement.date_end)))

    def _check_ready_for_submission(self):
        for agreement in self:
            if agreement.state not in ("draft", "rejected"):
                raise UserError(_("Only draft or rejected agreements can be submitted for approval."))
            if not agreement.line_ids:
                raise UserError(_("Add at least one order line before submitting agreement %s.", agreement.name))
            if any(line.product_uom_qty <= 0 for line in agreement.line_ids):
                raise UserError(_("All agreement lines must have a positive agreed quantity."))
        self._check_not_expired()

    def action_submit(self):
        self._check_ready_for_submission()
        Approval = self.env["blanket.sale.agreement.approval"].sudo()
        for agreement in self:
            template = agreement.approval_template_id or agreement.company_id.blanket_approval_template_id
            segregation = agreement._segregation_of_duties()
            history = agreement.approval_ids
            cycle = max(history.mapped("cycle") or [0]) + 1
            history.filtered(lambda a: not a.superseded).sudo().write({"superseded": True})
            approval_vals = []
            for tier in template.line_ids:
                approvers = tier._get_approver_users()
                if segregation:
                    approvers -= agreement.user_id
                if not approvers:
                    if segregation:
                        raise UserError(_(
                            "Approval tier '%(tier)s' of template '%(template)s' has no active approver "
                            "other than %(user)s, who cannot approve their own agreement.",
                            tier=tier.name, template=template.name, user=agreement.user_id.name))
                    raise UserError(_(
                        "Approval tier '%(tier)s' of template '%(template)s' has no active approver.",
                        tier=tier.name, template=template.name))
                approval_vals.append({
                    "agreement_id": agreement.id,
                    "sequence": tier.sequence,
                    "name": tier.name,
                    "role_id": tier.role_id.id,
                    "approver_ids": [(6, 0, approvers.ids)],
                    "state": "waiting",
                    "cycle": cycle,
                })
            if segregation and not has_distinct_approvers([set(vals["approver_ids"][0][2]) for vals in approval_vals]):
                raise UserError(_(
                    "Template '%s' cannot be approved by a different person at each tier: "
                    "add approvers to its roles or turn off segregation of duties in the Sales settings.",
                    template.name))
            Approval.create(approval_vals)
            agreement.write({
                "state": "to_approve",
                "approval_template_id": template.id,
                "submitted_date": fields.Datetime.now(),
            })
            agreement.message_post(
                body=_("Agreement submitted for approval by %s.", self.env.user.name),
                subtype_xmlid="mail.mt_note",
            )
            agreement._activate_next_tier()
        return True

    def _activate_next_tier(self):
        """Open the next waiting tier and schedule one activity per approver.
        When no tier remains, the agreement becomes approved."""
        self.ensure_one()
        next_tier = self._active_approvals().filtered(lambda a: a.state == "waiting").sorted(
            lambda a: (a.sequence, a.id))[:1]
        if not next_tier:
            self._finalize_approval()
            return
        next_tier.sudo().state = "pending"
        deadline = fields.Date.context_today(self)
        decided = self._active_approvals().mapped("user_id") if self._segregation_of_duties() else self.env["res.users"]
        for user in next_tier.approver_ids - decided:
            self.sudo().activity_schedule(
                ACTIVITY_APPROVAL,
                date_deadline=deadline,
                summary=_("Approve %(agreement)s - %(tier)s", agreement=self.name, tier=next_tier.name),
                note=_("Please review blanket sales agreement %(agreement)s for %(partner)s "
                       "(total %(amount)s %(currency)s) and approve or reject it.",
                       agreement=self.name, partner=self.partner_id.display_name,
                       amount=formatLang(self.env, self.amount_total, digits=self.currency_id.decimal_places),
                       currency=self.currency_id.name),
                user_id=user.id,
            )

    def _search_workflow_activities(self, xmlids, users=None):
        """Workflow activities of these agreements, searched directly (not through the
        activity_ids cache, which depends on the reading user's access)."""
        domain = [
            ("res_model", "=", self._name),
            ("res_id", "in", self.ids),
            ("activity_type_id", "in", [self.env.ref(xmlid).id for xmlid in xmlids]),
        ]
        if users is not None:
            domain.append(("user_id", "in", users.ids))
        return self.env["mail.activity"].sudo().search(domain)

    def _get_tier_activities(self, approval):
        return self._search_workflow_activities([ACTIVITY_APPROVAL], approval.approver_ids)

    def _check_current_approver(self):
        self.ensure_one()
        if self.state != "to_approve" or not self.current_approval_id:
            raise UserError(_("Agreement %s is not waiting for an approval.", self.name))
        if self.env.user not in self.current_approval_id.approver_ids and not self.env.su:
            raise AccessError(_(
                "You are not an approver of tier '%(tier)s' for agreement %(agreement)s.",
                tier=self.current_approval_id.name, agreement=self.name))

    def _check_segregation_of_duties(self):
        """One person approves at most one tier, never their own agreement, and approving must
        not leave a later tier without a possible approver."""
        self.ensure_one()
        if self.env.su or not self._segregation_of_duties():
            return
        user = self.env.user
        current = self.current_approval_id
        if user == self.user_id:
            raise UserError(_("You cannot approve agreement %s: you are its responsible user.", self.name))
        approvals = self._active_approvals()
        decided = approvals.filtered(lambda a: a.state == "approved").mapped("user_id")
        if user in decided:
            raise UserError(_(
                "You already approved another tier of agreement %(agreement)s; "
                "tier '%(tier)s' needs a different approver.", agreement=self.name, tier=current.name))
        excluded = set((decided | user | self.user_id).ids)
        remaining = approvals.filtered(lambda a: a.state == "waiting").sorted(lambda a: (a.sequence, a.id))
        candidates = [set(tier.approver_ids.ids) - excluded for tier in remaining]
        if not has_distinct_approvers(candidates):
            blocked = next((tier for tier, users in zip(remaining, candidates) if not users), remaining[:1])
            raise UserError(_(
                "If you approve tier '%(tier)s', nobody would be left to approve tier '%(next)s'. "
                "Another approver of tier '%(tier)s' must approve it.",
                tier=current.name, next=blocked.name))

    def action_approve(self):
        for agreement in self:
            agreement._check_current_approver()
            agreement._check_segregation_of_duties()
            approval = agreement.current_approval_id
            approval.sudo().write({
                "state": "approved",
                "user_id": self.env.user.id,
                "date": fields.Datetime.now(),
            })
            activities = agreement._get_tier_activities(approval)
            own = activities.filtered(lambda act: act.user_id == self.env.user)
            (activities - own).unlink()
            if own:
                own.action_feedback(feedback=_("Approved"))
            agreement.sudo().message_post(
                body=_("Tier '%(tier)s' approved by %(user)s.", tier=approval.name, user=self.env.user.name),
                subtype_xmlid="mail.mt_note",
                author_id=self.env.user.partner_id.id,
            )
            agreement._activate_next_tier()
        return True

    def action_open_reject_wizard(self):
        self.ensure_one()
        self._check_current_approver()
        return {
            "name": _("Reject Agreement"),
            "type": "ir.actions.act_window",
            "res_model": "blanket.agreement.reject.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_agreement_id": self.id},
        }

    def _do_reject(self, reason):
        for agreement in self:
            agreement._check_current_approver()
            approval = agreement.current_approval_id
            approval.sudo().write({
                "state": "rejected",
                "user_id": self.env.user.id,
                "date": fields.Datetime.now(),
                "comment": reason,
            })
            agreement._get_tier_activities(approval).unlink()
            agreement.sudo().write({"state": "rejected"})
            agreement.sudo().message_post(
                body=_("Agreement rejected at tier '%(tier)s' by %(user)s. Reason: %(reason)s",
                       tier=approval.name, user=self.env.user.name, reason=reason),
                subtype_xmlid="mail.mt_note",
                author_id=self.env.user.partner_id.id,
                partner_ids=agreement.user_id.partner_id.ids,
            )
        return True

    def _finalize_approval(self):
        self.ensure_one()
        self.sudo().write({"state": "approved", "approved_date": fields.Datetime.now()})
        self.sudo().activity_schedule(
            ACTIVITY_ISSUE,
            date_deadline=fields.Date.context_today(self),
            summary=_("Issue agreement %s", self.name),
            note=_("All approvers signed off. Issue the agreement to send it to the customer."),
            user_id=self.user_id.id,
        )
        self.sudo().message_post(
            body=_("All approval tiers are complete. %s, the agreement is ready to be issued.",
                   self.user_id.name),
            subtype_xmlid="mail.mt_note",
            partner_ids=self.user_id.partner_id.ids,
        )

    # ------------------------------------------------------------------
    # Issuance & lifecycle
    # ------------------------------------------------------------------
    def action_issue(self):
        for agreement in self:
            if agreement.state != "approved":
                raise UserError(_("Only approved agreements can be issued."))
            if self.env.user != agreement.user_id and not self.env.user.has_group(
                    "sales_team.group_sale_manager") and not self.env.su:
                raise AccessError(_("Only the responsible user or a sales manager can issue agreement %s.",
                                    agreement.name))
            agreement._check_not_expired()
            agreement.write({"state": "issued", "issue_date": fields.Date.context_today(agreement)})
            agreement._search_workflow_activities([ACTIVITY_ISSUE]).action_feedback(
                feedback=_("Agreement issued"))
            agreement._send_customer_mail("advanced_blanket_sales_agreement.mail_template_blanket_issued")
        return True

    def _send_customer_mail(self, template_xmlid):
        """Post a templated e-mail to the customer in the chatter. Returns True when sent."""
        self.ensure_one()
        if not self.partner_id.email:
            self.message_post(
                body=_("No e-mail sent: customer %s has no e-mail address.", self.partner_id.display_name),
                subtype_xmlid="mail.mt_note",
            )
            return False
        template = self.env.ref(template_xmlid)
        self.with_context(mail_post_autofollow=False).message_post_with_source(
            template,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            email_layout_xmlid="mail.mail_notification_light",
        )
        return True

    def action_cancel(self):
        for agreement in self:
            if agreement.sale_order_ids.filtered(lambda so: so.state == "sale"):
                raise UserError(_("Agreement %s has confirmed sales orders and cannot be cancelled.",
                                  agreement.name))
            agreement._clear_workflow_activities()
        self.write({"state": "cancel"})
        return True

    def action_draft(self):
        for agreement in self:
            if agreement.state not in ("rejected", "cancel"):
                raise UserError(_("Only rejected or cancelled agreements can be reset to draft."))
            agreement._clear_workflow_activities()
            # keep the decisions taken so far as history
            agreement._active_approvals().sudo().write({"superseded": True})
        self.write({"state": "draft", "reminder_sent": False, "expiry_notice_sent": False})
        return True

    def _clear_workflow_activities(self):
        self._search_workflow_activities([ACTIVITY_APPROVAL, ACTIVITY_ISSUE]).unlink()

    def action_print(self):
        return self.env.ref("advanced_blanket_sales_agreement.action_report_blanket_agreement").report_action(self)

    def action_view_sale_orders(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("sale.action_orders")
        action["domain"] = [("blanket_agreement_id", "=", self.id)]
        action["context"] = {"default_blanket_agreement_id": self.id, "default_partner_id": self.partner_id.id}
        if self.sale_order_count == 1:
            action["views"] = [(self.env.ref("sale.view_order_form").id, "form")]
            action["res_id"] = self.sale_order_ids.id
        return action

    # ------------------------------------------------------------------
    # Quotation generation
    # ------------------------------------------------------------------
    def _check_can_order(self):
        self.ensure_one()
        if self.state != "issued":
            raise UserError(_("Quotations can only be created from issued agreements."))

    def _get_quotation_pricelist(self):
        """Pricelist of generated quotations. It is always in the agreement currency, so the
        agreed prices are used as they are: the agreement's pricelist, else the customer's
        when its currency matches, else any pricelist of the company in that currency."""
        self.ensure_one()
        if self.pricelist_id:
            return self.pricelist_id
        partner_pricelist = self.partner_id.with_company(self.company_id).property_product_pricelist
        if partner_pricelist.currency_id == self.currency_id:
            return partner_pricelist
        return self.env["product.pricelist"].search([
            ("currency_id", "=", self.currency_id.id),
            ("company_id", "in", [self.company_id.id, False]),
        ], limit=1)

    def _prepare_sale_order_vals(self, commitment_date=False):
        self.ensure_one()
        vals = {
            "partner_id": self.partner_id.id,
            "company_id": self.company_id.id,
            "blanket_agreement_id": self.id,
            "client_order_ref": self.client_order_ref or self.name,
            "origin": self.name,
            "user_id": self.user_id.id,
        }
        pricelist = self._get_quotation_pricelist()
        if pricelist:
            vals["pricelist_id"] = pricelist.id
        elif self.env["sale.order"]._fields["pricelist_id"].required or \
                self.currency_id != self.company_id.currency_id:
            raise UserError(_(
                "No pricelist in %(currency)s is available for agreement %(agreement)s. "
                "Set a pricelist on the agreement.", currency=self.currency_id.name, agreement=self.name))
        if self.payment_term_id:
            vals["payment_term_id"] = self.payment_term_id.id
        if self.incoterm_id and "incoterm" in self.env["sale.order"]._fields:
            vals["incoterm"] = self.incoterm_id.id
        if commitment_date:
            vals["commitment_date"] = fields.Datetime.to_datetime(commitment_date)
        return vals

    def action_create_quotation(self):
        """Create a quotation for what is neither ordered nor already in an open quotation."""
        self.ensure_one()
        self._check_can_order()
        quantities = {line: line.qty_remaining - line.qty_quoted for line in self.line_ids}
        lines = self.line_ids.filtered(lambda line: quantities[line] > 0)
        if not lines:
            open_quotes = self.line_ids.sudo().sale_line_ids.order_id.filtered(
                lambda so: so.state in ("draft", "sent"))
            if open_quotes:
                raise UserError(_(
                    "Nothing left to quote: the remaining quantities are already in open quotations %s.",
                    ", ".join(open_quotes.mapped("name"))))
            raise UserError(_("All agreed quantities have already been ordered."))
        order_vals = self._prepare_sale_order_vals()
        order_vals["order_line"] = [(0, 0, line._prepare_sale_order_line_vals(quantities[line])) for line in lines]
        order = self.env["sale.order"].create(order_vals)
        return self._action_open_order(order)

    def _action_open_order(self, order):
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": order.id,
            "view_mode": "form",
            "target": "current",
        }

    # ------------------------------------------------------------------
    # Scheduled action: reminders & expiration
    # ------------------------------------------------------------------
    @api.model
    def _cron_process_expirations(self):
        today = fields.Date.context_today(self)
        stats = {"reminders": 0, "notices": 0, "expired": 0}

        reminders = self.search([
            ("state", "=", "issued"),
            ("reminder_sent", "=", False),
            ("reminder_date", "!=", False),
            ("reminder_date", "<=", today),
            ("date_end", ">", today),
        ])
        for agreement in reminders:
            with self.env.cr.savepoint():
                agreement._send_customer_mail("advanced_blanket_sales_agreement.mail_template_blanket_reminder")
                agreement.reminder_sent = True
                stats["reminders"] += 1

        notices = self.search([
            ("state", "=", "issued"),
            ("expiry_notice_sent", "=", False),
            ("date_end", "<=", today),
        ])
        for agreement in notices:
            with self.env.cr.savepoint():
                agreement._send_customer_mail("advanced_blanket_sales_agreement.mail_template_blanket_expired")
                agreement.expiry_notice_sent = True
                stats["notices"] += 1

        to_expire = self.search([("state", "=", "issued"), ("date_end", "<", today)])
        if to_expire:
            to_expire._clear_workflow_activities()
            to_expire.write({"state": "expired"})
            for agreement in to_expire:
                agreement.message_post(
                    body=_("Agreement expired on %s.", format_date(self.env, agreement.date_end)),
                    subtype_xmlid="mail.mt_note",
                )
            stats["expired"] = len(to_expire)
        _logger.info("Blanket agreements cron: %(reminders)s reminder(s), %(notices)s expiry notice(s), "
                     "%(expired)s agreement(s) expired.", stats)
        return stats
