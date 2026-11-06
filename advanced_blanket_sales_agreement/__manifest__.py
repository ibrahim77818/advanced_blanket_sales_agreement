# -*- coding: utf-8 -*-
{
    "name": "Advanced Blanket Sales Agreement",
    "version": "17.0.1.1.0",
    "category": "Sales/Sales",
    "summary": "Blanket sales agreements with multi-tier approvals, delivery schedules, "
               "legal terms, automated customer emails and expiry reminders",
    "description": """
Advanced Blanket Sales Agreement
================================
* Enable / disable from Sales settings (disabled by default)
* Approval templates with customizable roles and sequential approval tiers
* Agreement lines, delivery schedules and detailed legal terms & conditions
* mail.activity driven approval cycle, issuance by the responsible user
* Formal agreement PDF e-mailed to the customer on issuance
* Daily scheduled action: pre-expiration reminders and final expiration notices
* Quotations generated from agreement lines or delivery schedules, with
  quantity consumption control on order confirmation
* Full Arabic (ar_001) translation and RTL-safe PDF report
""",
    "author": "Ibrahim Yahia",
    "website": "https://www.linkedin.com/in/ibrahim-yahia",
    "license": "LGPL-3",
    "depends": ["sale_management", "account"],
    "data": [
        "security/blanket_agreement_security.xml",
        "security/ir.model.access.csv",
        "data/ir_sequence_data.xml",
        "data/mail_activity_type_data.xml",
        "report/blanket_agreement_report.xml",
        "report/blanket_agreement_templates.xml",
        "data/mail_template_data.xml",
        "data/ir_cron_data.xml",
        "wizard/blanket_agreement_reject_wizard_views.xml",
        "views/blanket_approval_views.xml",
        "views/blanket_sale_agreement_views.xml",
        "views/sale_order_views.xml",
        "views/res_config_settings_views.xml",
        "views/menus.xml",
    ],
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
    "auto_install": False,
}
