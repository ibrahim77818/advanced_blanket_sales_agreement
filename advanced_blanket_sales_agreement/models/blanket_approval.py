# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class BlanketAgreementApprovalRole(models.Model):
    _name = "blanket.agreement.approval.role"
    _description = "Blanket Agreement Approval Role"
    _order = "name"

    name = fields.Char(required=True, translate=True)
    description = fields.Text(translate=True)
    user_ids = fields.Many2many(
        "res.users",
        "blanket_approval_role_users_rel",
        "role_id",
        "user_id",
        string="Members",
        domain=[("share", "=", False)],
        help="Any member of the role can approve a tier assigned to this role.",
    )
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)
    active = fields.Boolean(default=True)

    _name_company_uniq = models.Constraint(
        "unique(name, company_id)", "An approval role with this name already exists for this company.")


class BlanketAgreementApprovalTemplate(models.Model):
    _name = "blanket.agreement.approval.template"
    _description = "Blanket Agreement Approval Template"
    _order = "name"

    name = fields.Char(required=True, translate=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)
    note = fields.Text(string="Description", translate=True)
    line_ids = fields.One2many(
        "blanket.agreement.approval.template.line", "template_id",
        string="Approval Tiers", copy=True,
    )
    tier_count = fields.Integer(compute="_compute_tier_count")

    @api.depends("line_ids")
    def _compute_tier_count(self):
        for template in self:
            template.tier_count = len(template.line_ids)


class BlanketAgreementApprovalTemplateLine(models.Model):
    _name = "blanket.agreement.approval.template.line"
    _description = "Blanket Agreement Approval Tier"
    _order = "sequence, id"

    template_id = fields.Many2one(
        "blanket.agreement.approval.template", required=True, ondelete="cascade",
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string="Tier", required=True, translate=True)
    approver_type = fields.Selection(
        [("role", "Role"), ("user", "Specific User")],
        string="Approver Type", required=True, default="role",
    )
    role_id = fields.Many2one("blanket.agreement.approval.role", string="Role", ondelete="restrict")
    user_id = fields.Many2one(
        "res.users", string="Approver", domain=[("share", "=", False)], ondelete="restrict",
    )

    @api.constrains("approver_type", "role_id", "user_id")
    def _check_approver(self):
        for line in self:
            if line.approver_type == "role" and not line.role_id:
                raise ValidationError(_("Tier '%s' must define an approval role.", line.name))
            if line.approver_type == "user" and not line.user_id:
                raise ValidationError(_("Tier '%s' must define an approver.", line.name))

    @api.onchange("approver_type")
    def _onchange_approver_type(self):
        if self.approver_type == "role":
            self.user_id = False
        else:
            self.role_id = False

    def _get_approver_users(self):
        self.ensure_one()
        if self.approver_type == "user":
            return self.user_id
        return self.role_id.user_ids.filtered("active")
