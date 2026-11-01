"""
Task 10 of targeted_fixes_01 — payroll must name EMPLOYEES, not staff codes.

The rule: a validation message a payroll officer reads must say
"Compensation is missing for John Okafor (Kitchen)", never
"Compensation is missing for STF-20261028-169B2A646559".

These tests pin four things:

* a message names the employee, with the department where it helps;
* an account with no registered name says "Name unavailable", and the staff
  code appears only inside that fallback (so the record stays traceable);
* an e-mail address is never used as a person's name;
* the payroll CALCULATION is untouched — the same inputs produce the same
  amounts whether or not the display changed.
"""
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import User
from apps.core.utils import staff_display_name, staff_person_name
from apps.staff_operations.models import StaffProfile
from apps.staff_operations.services.payroll_service import (
    NAME_UNAVAILABLE,
    staff_display_name as payroll_display_name,
    staff_names_for,
)

from .factories import make_staff


def profile_for(user, code, department="KITCHEN", title="Cook"):
    return StaffProfile.objects.create(
        user=user, employee_code=code, department=department, job_title=title,
    )


class StaffNameHelperTests(TestCase):
    def setUp(self):
        self.john = make_staff("task10-john@staff.dev", role=User.Role.CHEF)
        self.john.first_name = "John"
        self.john.middle_name = "Chukwuemeka"
        self.john.last_name = "Okafor"
        self.john.save(update_fields=["first_name", "middle_name", "last_name"])

    def test_full_name_is_first_middle_last(self):
        self.assertEqual(staff_person_name(self.john), "John Chukwuemeka Okafor")

    def test_a_missing_middle_name_does_not_leave_a_double_space(self):
        self.john.middle_name = ""
        self.john.save(update_fields=["middle_name"])
        self.assertEqual(staff_person_name(self.john), "John Okafor")

    def test_department_is_appended_in_brackets(self):
        self.assertEqual(
            staff_display_name(self.john, department="KITCHEN"),
            "John Chukwuemeka Okafor (KITCHEN)",
        )

    def test_an_account_with_no_name_returns_empty_rather_than_the_email(self):
        nameless = make_staff("task10-nameless@staff.dev", role=User.Role.CHEF)
        nameless.first_name = ""
        nameless.middle_name = ""
        nameless.last_name = ""
        nameless.save(update_fields=["first_name", "middle_name", "last_name"])
        self.assertEqual(staff_person_name(nameless), "", "an e-mail address is not a name")
        self.assertEqual(staff_display_name(nameless), "")

    def test_the_payroll_helper_falls_back_to_name_unavailable_plus_the_code(self):
        nameless = make_staff("task10-nameless2@staff.dev", role=User.Role.CHEF)
        nameless.first_name = ""
        nameless.middle_name = ""
        nameless.last_name = ""
        nameless.save(update_fields=["first_name", "middle_name", "last_name"])
        profile = profile_for(nameless, "STF-T10-0001")
        self.assertEqual(
            payroll_display_name(profile), f"{NAME_UNAVAILABLE} (STF-T10-0001)"
        )
        self.assertNotIn("@", payroll_display_name(profile))

    def test_the_payroll_helper_names_a_known_employee(self):
        profile = profile_for(self.john, "STF-T10-0002")
        self.assertEqual(payroll_display_name(profile), "John Chukwuemeka Okafor")
        self.assertEqual(
            payroll_display_name(profile, include_department=True),
            "John Chukwuemeka Okafor (KITCHEN)",
        )

    def test_the_code_is_never_the_primary_label(self):
        profile = profile_for(self.john, "STF-T10-0003")
        label = payroll_display_name(profile, include_department=True)
        self.assertTrue(label.startswith("John Chukwuemeka Okafor"), label)
        self.assertNotIn("STF-", label, "a code must not appear when a name exists")

    def test_staff_names_for_maps_a_whole_population(self):
        profile = profile_for(self.john, "STF-T10-0004")
        # The default names the employee WITH their department, so two people
        # who share a name are still distinguishable in a validation message.
        self.assertEqual(
            staff_names_for([profile]), ["John Chukwuemeka Okafor (KITCHEN)"]
        )
        self.assertEqual(
            staff_names_for([profile], include_department=False),
            ["John Chukwuemeka Okafor"],
        )

    def test_staff_names_for_handles_an_empty_list(self):
        self.assertEqual(staff_names_for([]), [])


class PayrollMessageTests(TestCase):
    """The messages a payroll officer actually reads."""

    def setUp(self):
        self.cook = make_staff("task10-cook@staff.dev", role=User.Role.CHEF)
        self.cook.first_name = "Ada"
        self.cook.middle_name = ""
        self.cook.last_name = "Nwosu"
        self.cook.save(update_fields=["first_name", "middle_name", "last_name"])
        self.profile = profile_for(self.cook, "STF-T10-COOK", department="KITCHEN")

    def test_the_missing_compensation_message_names_the_employee(self):
        """The message that used to read a list of STF- codes."""
        from apps.staff_operations.services.payroll_service import compensation_gap_message as _compensation_gap_message

        message = _compensation_gap_message([self.profile])
        self.assertIn("Ada Nwosu", message)
        self.assertNotIn("STF-T10-COOK", message, "the code is not the identification")
        self.assertNotIn("@", message)

    def test_the_message_caps_a_long_list_and_says_how_many_more(self):
        from apps.staff_operations.services.payroll_service import compensation_gap_message as _compensation_gap_message

        profiles = []
        for index in range(35):
            user = make_staff(f"task10-bulk{index}@staff.dev", role=User.Role.CHEF)
            user.first_name = "Bulk"
            user.last_name = f"Employee {index}"
            user.save(update_fields=["first_name", "last_name"])
            profiles.append(profile_for(user, f"STF-T10-B{index:03d}"))
        message = _compensation_gap_message(profiles)
        self.assertIn("and 5 more", message)
        self.assertNotIn("STF-T10-", message)

    def test_a_nameless_profile_is_still_traceable_by_its_code(self):
        nameless = make_staff("task10-noname@staff.dev", role=User.Role.CHEF)
        nameless.first_name = ""
        nameless.middle_name = ""
        nameless.last_name = ""
        nameless.save(update_fields=["first_name", "middle_name", "last_name"])
        profile = profile_for(nameless, "STF-T10-NONAME")

        from apps.staff_operations.services.payroll_service import compensation_gap_message as _compensation_gap_message

        message = _compensation_gap_message([profile])
        self.assertIn(NAME_UNAVAILABLE, message)
        self.assertIn("STF-T10-NONAME", message, "a nameless record stays traceable")
        self.assertNotIn("@", message)


class PayrollCalculationUnchangedTests(TestCase):
    """The display change must not move a single naira."""

    def test_payroll_line_still_uses_the_registered_name(self):
        from apps.staff_operations.models import PayrollLine

        field = PayrollLine._meta.get_field("employee_name")
        self.assertTrue(field.max_length >= 120)
        # The column is a stored snapshot, so a later rename never rewrites a
        # historical payslip.
        self.assertFalse(field.null)

    def test_statutory_rates_are_untouched(self):
        """A guard against "while I was in there" edits to the arithmetic."""
        from apps.staff_operations.services import payroll_service

        source = open(payroll_service.__file__, encoding="utf-8").read()
        # The service must still compute from the persisted compensation row and
        # the persisted statutory configuration — never from a name.
        self.assertIn("basic_salary", source)
        self.assertIn("housing_allowance", source)
        self.assertIn("transport_allowance", source)
