# -*- coding: utf-8 -*-
"""Odoo 19 units: no UoM categories. A line may use the product unit or one of the
product's packaging units; quantities and prices convert through the common reference."""
from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import BlanketAgreementCommon


@tagged("post_install", "-at_install", "blanket_agreement")
class TestUom(BlanketAgreementCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.unit = cls.env.ref("uom.product_uom_unit")
        cls.pack6 = cls.env.ref("uom.product_uom_pack_6")
        cls.kg = cls.env.ref("uom.product_uom_kgm")
        cls.product.write({"uom_id": cls.unit.id, "uom_ids": [Command.set(cls.pack6.ids)]})

    def test_u01_packaging_unit_allowed_and_converted(self):
        agreement = self._create_agreement(line_ids=[Command.create({
            "product_id": self.product.id, "product_uom_qty": 5.0, "product_uom": self.pack6.id,
            "price_unit": 600.0})])
        line = agreement.line_ids
        self.assertEqual(line.allowed_uom_ids, self.unit | self.pack6)
        self.assertEqual(line.product_uom, self.pack6)
        self._approve_all(agreement)
        agreement.with_user(self.responsible).action_issue()
        order = self.env["sale.order"].browse(
            agreement.with_user(self.responsible).action_create_quotation()["res_id"])
        sol = order.order_line
        self.assertEqual(sol.product_uom_id, self.pack6)
        self.assertEqual(sol.product_uom_qty, 5.0)
        self.assertAlmostEqual(sol.price_unit, 600.0)
        # ordering 12 single units consumes 2 packs of 6
        sol.write({"product_uom_id": self.unit.id, "product_uom_qty": 12.0})
        self.assertAlmostEqual(sol.price_unit, 100.0)
        order.with_user(self.responsible).action_confirm()
        self.assertAlmostEqual(line.qty_ordered, 2.0)
        self.assertAlmostEqual(line.qty_remaining, 3.0)

    def test_u02_unrelated_unit_rejected(self):
        with self.assertRaises(ValidationError):
            self._create_agreement(line_ids=[Command.create({
                "product_id": self.product.id, "product_uom_qty": 1.0, "product_uom": self.kg.id})])
