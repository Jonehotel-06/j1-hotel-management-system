"""One role per department, department isolation, and audited normalisation.

Covers the owner's requirement:

* every department has exactly ONE standard role and no higher/lower staff role
  inside it (``apps.accounts.departments``);
* the API refuses to recreate a retired departmental title, and a request body
  can never pick a different department to obtain permissions;
* an account on a retired title keeps every capability it already held when it
  is moved onto the department role, and the move is audited (migration 0027).
"""
import importlib

from apps.accounts.capabilities import capability_codes_for_user
from apps.accounts.departments import (
    ASSIGNABLE_ROLES,
    DEPARTMENT_ROLES,
    GLOBAL_ROLES,
    RETIRED_DEPARTMENTAL_ROLES,
    RetiredDepartmentalRole,
    assert_assignable_role,
    canonical_role,
    department_for_role,
    department_for_user,
    is_cross_department,
)
from apps.accounts.models import Capability, RoleCapability, User, UserCapabilityOverride, Workstation
from apps.audit.models import AuditLog

from .base import BaseAPITestCase
from .factories import make_staff, make_user

PASSWORD = "Str0ng!Pass"
LOGIN_URL = "/api/auth/login/"


class DepartmentRoleModelTests(BaseAPITestCase):
    def test_each_department_has_exactly_one_canonical_role(self):
        operational = {
            code for code, _ in Workstation.Department.choices
            if code != Workstation.Department.SHARED
        }
        self.assertEqual(set(DEPARTMENT_ROLES), operational)
        roles = list(DEPARTMENT_ROLES.values())
        self.assertEqual(len(roles), len(set(roles)), "a role may serve only one department")
        for role in roles:
            self.assertIn(role, User.Role.values)

    def test_retired_titles_are_not_assignable_and_resolve_to_the_department_role(self):
        self.assertTrue(set(DEPARTMENT_ROLES.values()).issubset(ASSIGNABLE_ROLES))
        self.assertTrue(GLOBAL_ROLES.issubset(ASSIGNABLE_ROLES))
        for retired, canonical in RETIRED_DEPARTMENTAL_ROLES.items():
            self.assertNotIn(retired, ASSIGNABLE_ROLES)
            self.assertEqual(canonical_role(retired), canonical)
            self.assertIn(canonical, DEPARTMENT_ROLES.values())
            with self.assertRaisesMessage(RetiredDepartmentalRole, "retired departmental title"):
                assert_assignable_role(retired)

    def test_global_oversight_roles_are_outside_every_department(self):
        for role in GLOBAL_ROLES:
            self.assertEqual(department_for_role(role), "")
            self.assertNotIn(role, DEPARTMENT_ROLES.values())

    def test_department_context_is_read_from_stored_records_only(self):
        bartender = make_staff("dept.bartender@staff.dev", role=User.Role.BARTENDER)
        self.assertEqual(bartender.department, Workstation.Department.BAR)
        self.assertEqual(department_for_user(bartender), Workstation.Department.BAR)
        self.assertTrue(is_cross_department(bartender, Workstation.Department.ACCOUNTS))
        self.assertFalse(is_cross_department(bartender, Workstation.Department.BAR))
        manager = make_staff("dept.manager@staff.dev", role=User.Role.MANAGER)
        self.assertEqual(manager.department, Workstation.Department.MANAGEMENT)

    def test_grants_are_never_silently_widened_by_resolving_a_legacy_title(self):
        """Resolution reads persisted rows only — the migration does the moving.

        A retired title is *displayed* as its department role, but it does not
        inherit that role's grants at request time: an authorization decision
        has to be traceable to a row somebody can review. Migration 0027 is what
        puts the account on the department role, and it preserves what the
        person already held as explicit, reasoned overrides.
        """
        legacy = make_staff("dept.legacy.barmanager@staff.dev", role=User.Role.BAR_MANAGER)
        peer = make_staff("dept.peer.bartender@staff.dev", role=User.Role.BARTENDER)
        self.assertEqual(legacy.department_role, User.Role.BARTENDER)
        legacy_codes = capability_codes_for_user(legacy)
        expected = set(
            RoleCapability.objects.filter(role=User.Role.BAR_MANAGER, capability__is_active=True)
            .values_list("capability__code", flat=True)
        )
        self.assertEqual(legacy_codes, frozenset(expected))
        # The department roles themselves are what they always were.
        self.assertIn("bar.order.manage", capability_codes_for_user(peer))
        self.assertIn("bar.order.manage", legacy_codes)


class StaffAssignmentApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("dept.admin@staff.dev", role=User.Role.ADMIN)

    def _create(self, **payload):
        body = {
            "email": "dept.new@staff.dev",
            "first_name": "New",
            "last_name": "Starter",
            "role": User.Role.WAITER,
            "password": "Str0ngPass!234",
        }
        body.update(payload)
        return self.client.post("/api/admin/users/", body, format="json")

    def test_retired_departmental_titles_cannot_be_created_again(self):
        self.auth(self.admin)
        for retired in ("BAR_MANAGER", "RESTAURANT_MANAGER", "KITCHEN_MANAGER", "ACCOUNTS_MANAGER"):
            response = self._create(email=f"{retired.lower()}@staff.dev", role=retired)
            self.assertEqual(response.status_code, 400, f"{retired}: {response.content}")
            self.assertIn("retired departmental title", response.content.decode())

    def test_a_department_role_is_created_and_the_department_must_agree_with_the_role(self):
        self.auth(self.admin)
        created = self._create(
            email="dept.bartender2@staff.dev", role=User.Role.BARTENDER,
            department=Workstation.Department.BAR,
        )
        self.assertEqual(created.status_code, 201, created.content)
        mismatched = self._create(
            email="dept.mismatch@staff.dev", role=User.Role.BARTENDER,
            department=Workstation.Department.ACCOUNTS,
        )
        self.assertEqual(mismatched.status_code, 400, mismatched.content)
        self.assertIn("belongs to", mismatched.content.decode())

    def test_role_change_cannot_restore_a_retired_title(self):
        self.auth(self.admin)
        waiter = make_staff("dept.promote@staff.dev", role=User.Role.WAITER)
        response = self.client.patch(
            f"/api/admin/users/{waiter.pk}/", {"role": "RESTAURANT_MANAGER"}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content)
        waiter.refresh_from_db()
        self.assertEqual(waiter.role, User.Role.WAITER)


class DepartmentRoleMigrationTests(BaseAPITestCase):
    """Migration 0027, executed against the live schema."""

    def _migration(self):
        return importlib.import_module("apps.accounts.migrations.0027_normalize_department_roles")

    def _fake_apps(self):
        from django.apps import apps

        return apps

    def test_normalisation_preserves_every_capability_and_is_audited(self):
        migration = self._migration()
        bar_manager = make_staff("dept.migrate.barmanager@staff.dev", role=User.Role.BAR_MANAGER)
        before = set(capability_codes_for_user(bar_manager))
        # A capability only the retired title has, so preservation is observable.
        capability, _ = Capability.objects.get_or_create(
            code="reports.department.view",
            defaults={"name": "View department reports", "category": "Reports"},
        )
        RoleCapability.objects.get_or_create(role=User.Role.BAR_MANAGER, capability=capability)
        bar_manager.refresh_from_db()
        before = set(capability_codes_for_user(bar_manager))

        migration.normalize_accounts(self._fake_apps(), None)

        bar_manager.refresh_from_db()
        self.assertEqual(bar_manager.role, User.Role.BARTENDER)
        after = set(capability_codes_for_user(bar_manager))
        self.assertTrue(before.issubset(after), f"lost: {sorted(before - after)}")
        self.assertTrue(
            UserCapabilityOverride.objects.filter(
                user=bar_manager, capability=capability, effect="GRANT"
            ).exists()
        )
        audit = AuditLog.objects.filter(
            action="STAFF_DEPARTMENT_ROLE_NORMALIZED", object_id=str(bar_manager.pk)
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.changes["role"], ["BAR_MANAGER", "BARTENDER"])
        self.assertIn("reports.department.view", audit.metadata["preserved_capabilities"])

    def test_normalisation_is_idempotent(self):
        migration = self._migration()
        make_staff("dept.migrate.twice@staff.dev", role=User.Role.KITCHEN_MANAGER)
        migration.normalize_accounts(self._fake_apps(), None)
        first = AuditLog.objects.filter(action="STAFF_DEPARTMENT_ROLE_NORMALIZED").count()
        migration.normalize_accounts(self._fake_apps(), None)
        self.assertEqual(
            AuditLog.objects.filter(action="STAFF_DEPARTMENT_ROLE_NORMALIZED").count(), first
        )

    def test_hr_officer_role_is_seeded_without_sensitive_approvals(self):
        migration = self._migration()
        migration.seed_hr_officer(self._fake_apps(), None)
        hr = make_staff("dept.hr@staff.dev", role=User.Role.HR_OFFICER)
        codes = capability_codes_for_user(hr)
        self.assertIn("staff.manage", codes)
        self.assertIn("payroll.manage", codes)
        self.assertNotIn("payroll.approve", codes)
        self.assertNotIn("leave.approve", codes)
        self.assertNotIn("payment.refund.approve", codes)


class ManagerSignInWithoutCodesTests(BaseAPITestCase):
    """A manager signs in with email + password alone, from any device.

    No attendance code, no workstation key, no fingerprint, and no front-desk
    desktop — the manager screens are reached straight after sign-in. The
    exemption is authority (the ``sign_in.any_device`` capability), not a title,
    so it survives the one-role-per-department model and stays individually
    reviewable.
    """

    def _login(self, user, ip="198.51.100.7"):
        return self.client.post(
            LOGIN_URL, {"email": user.email, "password": PASSWORD}, REMOTE_ADDR=ip
        )

    def test_manager_signs_in_with_only_email_and_password_from_any_device(self):
        manager = make_user("signin.manager@staff.dev", password=PASSWORD, role=User.Role.MANAGER)
        response = self._login(manager)
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["data"]
        self.assertIn("access", payload["tokens"])
        self.assertEqual(payload["user"]["role"], User.Role.MANAGER)
        # No attendance/access code was required, offered, or returned.
        body = response.content.decode()
        for forbidden in ("access_code", "attendance_code", "temporary_token"):
            self.assertNotIn(forbidden, body)
        # And the manager workspace is reachable with that session immediately.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {payload['tokens']['access']}")
        self.assertNotEqual(self.client.get("/api/admin/dashboard/").status_code, 403)

    def test_general_manager_and_admin_are_exempt_and_an_operator_is_not(self):
        from apps.accounts.desktop_policy import may_sign_in_anywhere, sign_in_policy_applies

        gm = make_staff("signin.gm@staff.dev", role=User.Role.GENERAL_MANAGER)
        admin = make_staff("signin.admin@staff.dev", role=User.Role.ADMIN)
        cashier = make_staff("signin.cashier@staff.dev", role=User.Role.CASHIER)
        self.assertTrue(may_sign_in_anywhere(gm))
        self.assertTrue(may_sign_in_anywhere(admin))
        self.assertFalse(sign_in_policy_applies(gm))
        self.assertTrue(sign_in_policy_applies(cashier))
        # An operator at a front-desk PC still uses the desk, as before.
        cashier_login = self._login(make_user(
            "signin.cashier2@staff.dev", password=PASSWORD, role=User.Role.CASHIER
        ))
        self.assertEqual(cashier_login.status_code, 403, cashier_login.content)
        self.assertEqual(cashier_login.json()["code"], "STAFF_SIGN_IN_RESTRICTED")

    def test_an_operator_can_be_given_device_freedom_explicitly_and_it_is_audited(self):
        from apps.accounts.desktop_policy import may_sign_in_anywhere

        capability = Capability.objects.get(code="sign_in.any_device")
        supervisor = make_user(
            "signin.supervisor@staff.dev", password=PASSWORD, role=User.Role.RECEPTIONIST
        )
        self.assertFalse(may_sign_in_anywhere(supervisor))
        UserCapabilityOverride.objects.create(
            user=supervisor, capability=capability, effect="GRANT",
            reason="Night-shift supervisor covers two desks; approved by the owner.",
        )
        supervisor.refresh_from_db()
        self.assertTrue(may_sign_in_anywhere(supervisor))
        response = self._login(supervisor)
        self.assertEqual(response.status_code, 200, response.content)


class AdminSiteRoleFormTests(BaseAPITestCase):
    """The Django admin is a second write path; it must obey the same rule."""

    def _admin_form_class(self):
        from django.contrib import admin
        from django.test import RequestFactory

        admin_user = make_user("adminform.admin@staff.dev", role=User.Role.ADMIN, is_superuser=True, is_staff=True)
        request = RequestFactory().get("/admin/")
        request.user = admin_user
        return admin.site._registry[User].get_form(request)

    def test_admin_change_form_does_not_offer_a_retired_title(self):
        form = self._admin_form_class()()
        offered = {value for value, _label in form.fields["role"].choices}
        self.assertEqual(offered & set(RETIRED_DEPARTMENTAL_ROLES), set())
        self.assertIn(User.Role.BARTENDER, offered)
        self.assertIn(User.Role.HR_OFFICER, offered)

    def test_admin_forms_refuse_a_crafted_retired_title(self):
        from django import forms as django_forms

        from apps.accounts.admin import JOneUserChangeForm

        waiter = make_staff("adminform.waiter2@staff.dev", role=User.Role.WAITER)
        for retired in ("RESTAURANT_MANAGER", "BAR_MANAGER", "HR_MANAGER"):
            form = JOneUserChangeForm(instance=waiter)
            form.cleaned_data = {"role": retired}
            with self.assertRaises(django_forms.ValidationError) as caught:
                form.clean_role()
            self.assertIn("retired departmental title", str(caught.exception))
        form = JOneUserChangeForm(instance=waiter)
        form.cleaned_data = {"role": User.Role.CHEF}
        self.assertEqual(form.clean_role(), User.Role.CHEF)
