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
from apps.staff_operations.models import StaffProfile

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
        for index, role in enumerate(GLOBAL_ROLES):
            self.assertEqual(department_for_role(role), "")
            self.assertNotIn(role, DEPARTMENT_ROLES.values())
            user = make_staff(f"dept.global{index}@staff.dev", role=role)
            self.assertFalse(is_cross_department(user, Workstation.Department.ACCOUNTS))

    def test_combined_procurement_and_security_are_single_canonical_departments(self):
        self.assertEqual(
            [dept for dept in DEPARTMENT_ROLES if dept == Workstation.Department.PROCUREMENT],
            [Workstation.Department.PROCUREMENT],
        )
        self.assertEqual(DEPARTMENT_ROLES[Workstation.Department.PROCUREMENT], User.Role.PROCUREMENT_OFFICER)
        self.assertEqual(DEPARTMENT_ROLES[Workstation.Department.SECURITY], User.Role.SECURITY)
        self.assertEqual(DEPARTMENT_ROLES[Workstation.Department.KITCHEN], User.Role.CHEF)
        self.assertNotIn(User.Role.KITCHEN_ASSISTANT, DEPARTMENT_ROLES.values())

    def test_profile_department_conflict_fails_closed_for_backend_capabilities(self):
        from apps.accounts.capabilities import has_capability

        manager = make_staff("dept.conflict@staff.dev", role=User.Role.MANAGER)
        profile = StaffProfile.objects.create(
            user=manager, employee_code="SEC-CONFLICT-1", department="ACCOUNTS"
        )
        self.assertEqual(department_for_user(manager), "")
        self.assertTrue(is_cross_department(manager, Workstation.Department.MANAGEMENT))
        self.assertTrue(is_cross_department(manager, Workstation.Department.ACCOUNTS))
        self.assertFalse(has_capability(manager, "staff.profile.manage"))
        # Keep the row reference explicit: a conflicting persisted profile is
        # what invalidates the account context, not request data.
        self.assertEqual(profile.user_id, manager.pk)

    def test_security_and_procurement_role_grants_stay_narrow(self):
        from apps.accounts.capabilities import capability_codes_for_user

        security = make_staff("dept.security.permissions@staff.dev", role=User.Role.SECURITY)
        procurement = make_staff("dept.procurement.permissions@staff.dev", role=User.Role.PROCUREMENT_OFFICER)
        self.assertEqual(
            capability_codes_for_user(security),
            frozenset({"guest_request.manage", "attendance.clock"}),
        )
        self.assertEqual(
            capability_codes_for_user(procurement),
            frozenset({"procurement.manage", "attendance.clock", "leave.request"}),
        )
        self.assertNotIn("procurement.approve", capability_codes_for_user(procurement))
        self.assertNotIn("booking.read", capability_codes_for_user(security))

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

    def test_secondary_staff_profile_creation_path_rejects_retired_and_conflicting_assignments(self):
        self.auth(self.admin)
        base = {
            "email": "dept.profile-create@staff.dev", "first_name": "Kitchen", "last_name": "Staff",
            "password": "Str0ngPass!234", "role": "CHEF", "department": "ACCOUNTS",
        }
        mismatch = self.client.post("/api/admin/staff-operations/profiles/", base, format="json")
        self.assertEqual(mismatch.status_code, 400, mismatch.content)
        self.assertFalse(User.objects.filter(email=base["email"]).exists())

        retired = dict(base, email="dept.profile-retired@staff.dev", role="KITCHEN_ASSISTANT", department="KITCHEN")
        refused = self.client.post("/api/admin/staff-operations/profiles/", retired, format="json")
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIn("retired departmental title", refused.content.decode())
        self.assertFalse(User.objects.filter(email=retired["email"]).exists())

    def test_kitchen_assistant_is_a_job_title_under_the_chef_role(self):
        self.auth(self.admin)
        response = self._create(
            email="dept.kitchen-assistant@staff.dev", role=User.Role.CHEF,
            department="KITCHEN", job_title="Kitchen Assistant",
        )
        self.assertEqual(response.status_code, 201, response.content)
        staff = User.objects.get(email="dept.kitchen-assistant@staff.dev")
        self.assertEqual(staff.role, User.Role.CHEF)
        self.assertEqual(staff.staff_profile.job_title, "Kitchen Assistant")
        self.assertEqual(staff.department, Workstation.Department.KITCHEN)

    def test_conflicting_persisted_profile_blocks_capability_and_legacy_role_endpoints(self):
        manager = make_staff("dept.conflict-endpoint@staff.dev", role=User.Role.MANAGER)
        StaffProfile.objects.create(
            user=manager, employee_code="SEC-CONFLICT-2", department="ACCOUNTS"
        )
        self.auth(manager)
        self.assertEqual(self.client.get("/api/admin/staff-operations/profiles/").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/dashboard/").status_code, 403)

    def test_profile_patch_cannot_spoof_role_or_department_and_role_transfer_syncs_profile(self):
        self.auth(self.admin)
        waiter = make_staff("dept.profile-patch@staff.dev", role=User.Role.WAITER)
        profile = StaffProfile.objects.create(
            user=waiter, employee_code="PROFILE-PATCH-1", department="RESTAURANT"
        )
        url = f"/api/admin/staff-operations/profiles/{profile.pk}/"
        retired = self.client.patch(url, {"role": "RESTAURANT_MANAGER"}, format="json")
        self.assertEqual(retired.status_code, 400, retired.content)
        mismatch = self.client.patch(url, {"department": "ACCOUNTS"}, format="json")
        self.assertEqual(mismatch.status_code, 400, mismatch.content)
        changed = self.client.patch(
            url, {"role": "CHEF", "department": "KITCHEN", "job_title": "Kitchen Assistant"}, format="json"
        )
        self.assertEqual(changed.status_code, 200, changed.content)
        waiter.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(waiter.role, User.Role.CHEF)
        self.assertEqual(profile.department, Workstation.Department.KITCHEN)
        self.assertEqual(profile.job_title, "Kitchen Assistant")

    def test_lifecycle_transfer_enforces_department_role_pair_and_rejects_retired_titles(self):
        self.auth(self.admin)
        waiter = make_staff("dept.transfer@staff.dev", role=User.Role.WAITER)
        profile = StaffProfile.objects.create(
            user=waiter, employee_code="PROFILE-TRANSFER-1", department="RESTAURANT"
        )
        url = f"/api/admin/users/{waiter.pk}/actions/"
        mismatch = self.client.post(url, {
            "action": "transfer_department", "department": "ACCOUNTS", "role": "CHEF",
        }, format="json")
        self.assertEqual(mismatch.status_code, 400, mismatch.content)
        waiter.refresh_from_db()
        self.assertEqual(waiter.role, User.Role.WAITER)
        retired = self.client.post(url, {
            "action": "transfer_department", "department": "RESTAURANT", "role": "RESTAURANT_MANAGER",
        }, format="json")
        self.assertEqual(retired.status_code, 400, retired.content)
        moved = self.client.post(url, {
            "action": "transfer_department", "department": "KITCHEN", "role": "CHEF",
            "job_title": "Kitchen Assistant",
        }, format="json")
        self.assertEqual(moved.status_code, 200, moved.content)
        waiter.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(waiter.role, User.Role.CHEF)
        self.assertEqual(profile.department, Workstation.Department.KITCHEN)
        self.assertEqual(profile.job_title, "Kitchen Assistant")

    def test_account_role_update_keeps_existing_staff_profile_in_sync(self):
        self.auth(self.admin)
        waiter = make_staff("dept.role-sync@staff.dev", role=User.Role.WAITER)
        profile = StaffProfile.objects.create(
            user=waiter, employee_code="PROFILE-ROLE-SYNC-1", department="RESTAURANT"
        )
        response = self.client.patch(
            f"/api/admin/users/{waiter.pk}/", {"role": "CHEF"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        waiter.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(waiter.role, User.Role.CHEF)
        self.assertEqual(profile.department, Workstation.Department.KITCHEN)
        self.assertEqual(department_for_user(waiter), Workstation.Department.KITCHEN)

    def test_permission_matrix_cannot_grant_capabilities_to_a_retired_role_alias(self):
        self.auth(self.admin)
        response = self.client.post("/api/admin/users/permissions-matrix/", {
            "role": "BAR_MANAGER", "capability_code": "bar.order.manage", "effect": "GRANT",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("retired departmental title", response.content.decode())

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

    def test_staff_profile_admin_rejects_department_mismatch_and_user_admin_syncs_role(self):
        from django import forms as django_forms
        from django.contrib import admin
        from django.test import RequestFactory

        from apps.accounts.admin import UserAdmin
        from apps.staff_operations.admin import StaffProfileAdminForm

        waiter = make_staff("adminform.profile-waiter@staff.dev", role=User.Role.WAITER)
        profile = StaffProfile.objects.create(
            user=waiter, employee_code="ADMIN-PROFILE-1", department="RESTAURANT"
        )
        profile_form = StaffProfileAdminForm(instance=profile)
        profile_form.cleaned_data = {
            "user": waiter, "department": "ACCOUNTS", "department_ref": None,
        }
        with self.assertRaises(django_forms.ValidationError):
            profile_form.clean()

        admin_user = make_user(
            "adminform.role-sync@staff.dev", role=User.Role.ADMIN,
            is_superuser=True, is_staff=True,
        )
        request = RequestFactory().post("/admin/")
        request.user = admin_user
        target = User.objects.get(pk=waiter.pk)
        target.role = User.Role.CHEF
        user_admin = admin.site._registry[User]
        self.assertIsInstance(user_admin, UserAdmin)
        user_admin.save_model(request, target, form=None, change=True)
        profile.refresh_from_db()
        self.assertEqual(profile.department, Workstation.Department.KITCHEN)
