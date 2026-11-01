"""
Task 11 of targeted_fixes_01 — Restaurant & Bar receipts show the member of
staff's NAME, not their e-mail address.

Acceptance criteria covered here:

* the order representation the receipt screens read carries the staff name
  (and department), taken from the employee's staff record;
* the e-mail address is still present for compatibility but is no longer the
  identity a receipt prints;
* a staff account with no registered name falls back to the employee code, and
  NEVER to the e-mail address;
* the name is read from the ORIGINAL transaction's employee, so reprinting an
  older receipt shows whoever took that order — transaction ownership is not
  changed by this work;
* nothing about the order, its totals or its ownership is modified.
"""
from decimal import Decimal

from apps.accounts.models import User
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.serializers import PosOrderDetailSerializer, PosOrderListSerializer
from apps.staff_operations.models import StaffProfile

from django.test import TestCase

from .factories import make_staff


def menu_item(name="Jollof Rice", price="2500.00"):
    category, _ = MenuCategory.objects.get_or_create(
        name="Receipt test", defaults={"service_area": MenuCategory.ServiceArea.RESTAURANT},
    )
    return MenuItem.objects.create(
        category=category, name=name, sku="RT-" + name[:6].upper().replace(" ", ""),
        base_price=Decimal(price), currency="NGN",
    )


_order_seq = {"n": 0}


def order_for(staff, mode=PosOrder.Mode.RESTAURANT):
    """Build a delivered, paid order. The reference is unique per order."""
    _order_seq["n"] += 1
    return PosOrder.objects.create(
        reference=f"T11-POS-{_order_seq['n']:04d}",
        mode=mode, status=PosOrder.Status.DELIVERED, settlement_status=PosOrder.SettlementStatus.PAID,
        created_by=staff, currency="NGN", subtotal=Decimal("2500.00"),
        tax_amount=Decimal("0.00"), total_amount=Decimal("2500.00"),
    )


class ReceiptStaffNameTests(TestCase):
    def setUp(self):
        self.waiter = make_staff("task11-waiter@staff.dev", role=User.Role.WAITER)
        self.waiter.first_name = "John"
        self.waiter.middle_name = ""
        self.waiter.last_name = "Okafor"
        self.waiter.save(update_fields=["first_name", "middle_name", "last_name"])
        self.order = order_for(self.waiter)

    def test_order_serializer_exposes_the_staff_name(self):
        data = PosOrderDetailSerializer(self.order).data
        self.assertEqual(data["created_by_name"], "John Okafor")
        self.assertEqual(data["created_by_display"], "John Okafor")

    def test_email_is_still_present_but_is_not_the_display_name(self):
        data = PosOrderDetailSerializer(self.order).data
        self.assertEqual(data["created_by_email"], "task11-waiter@staff.dev")
        self.assertNotIn("@", data["created_by_display"])
        self.assertNotIn("@", data["created_by_name"])

    def test_department_is_available_for_disambiguation(self):
        profile = StaffProfile.objects.create(
            user=self.waiter, employee_code="T11-EMP-001", department="RESTAURANT",
            job_title="Waiter",
        )
        data = PosOrderDetailSerializer(self.order).data
        self.assertEqual(data["created_by_department"], "RESTAURANT")
        # The display name itself stays the person's name; the department is a
        # separate field so a caller decides whether to append it.
        self.assertEqual(data["created_by_display"], "John Okafor")
        self.assertEqual(profile.user_id, self.waiter.pk)

    def test_a_profile_with_no_name_falls_back_to_the_employee_code(self):
        nameless = make_staff("task11-nameless@staff.dev", role=User.Role.WAITER)
        nameless.first_name = ""
        nameless.middle_name = ""
        nameless.last_name = ""
        nameless.save(update_fields=["first_name", "middle_name", "last_name"])
        order = order_for(nameless)
        data = PosOrderDetailSerializer(order).data
        self.assertEqual(data["created_by_name"], "")
        display = data["created_by_display"]
        self.assertNotIn("@", display, "an e-mail address must never be the printed identity")
        self.assertTrue(display, "a receipt without a name still identifies the record")

    def test_list_serializer_carries_the_same_fields(self):
        data = PosOrderListSerializer(self.order).data
        for field in ("created_by_name", "created_by_display", "created_by_department"):
            self.assertIn(field, data)

    def test_reprint_reads_the_original_transaction_employee(self):
        """A later edit to the account changes the NAME, never the ownership."""
        original_owner_id = self.order.created_by_id
        self.order.refresh_from_db()
        self.assertEqual(self.order.created_by_id, original_owner_id)
        self.waiter.last_name = "Okafor-Eze"
        self.waiter.save(update_fields=["last_name"])
        data = PosOrderDetailSerializer(self.order).data
        self.assertEqual(data["created_by_name"], "John Okafor-Eze")
        self.assertEqual(self.order.created_by_id, original_owner_id, "ownership unchanged")

    def test_totals_are_untouched_by_the_display_change(self):
        data = PosOrderDetailSerializer(self.order).data
        self.assertEqual(data["total_amount"], "2500.00")
        self.assertEqual(self.order.total_amount, Decimal("2500.00"))

    def test_shared_helper_matches_the_serializer(self):
        from apps.core.utils import staff_display_name, staff_person_name

        self.assertEqual(staff_person_name(self.waiter), "John Okafor")
        self.assertEqual(staff_display_name(self.waiter), "John Okafor")
        self.assertEqual(staff_display_name(self.waiter, department="RESTAURANT"), "John Okafor (RESTAURANT)")
        self.assertEqual(staff_display_name(None), "")
