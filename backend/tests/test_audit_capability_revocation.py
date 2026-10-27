"""Disposable regression: a persisted empty role must mean no baseline grants."""
from django.test import TestCase
from rest_framework.test import APIClient
from apps.accounts.capabilities import capability_codes_for_user
from apps.accounts.models import Capability, RoleCapability, User, UserCapabilityOverride


class EmptyRoleGrantsRegression(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email='matrix-admin@example.invalid', role=User.Role.ADMIN)
        self.cashier = User.objects.create_user(email='matrix-cashier@example.invalid', role=User.Role.CASHIER)

    def test_revoke_last_grant_in_management_api_remains_revoked(self):
        RoleCapability.objects.filter(role=User.Role.CASHIER).exclude(capability__code='payment.capture').delete()
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post('/api/admin/users/permissions-matrix/', {'role':'CASHIER', 'capability_code':'payment.capture', 'effect':'REVOKE'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(RoleCapability.objects.filter(role=User.Role.CASHIER).exists())
        self.cashier.refresh_from_db()
        self.assertEqual(capability_codes_for_user(self.cashier), frozenset(), 'An empty persisted role must not silently regrant the code defaults')

    def test_inactive_role_capabilities_are_not_restored_by_fallback(self):
        Capability.objects.filter(role_grants__role=User.Role.CASHIER).update(is_active=False)
        self.assertFalse(RoleCapability.objects.filter(role=User.Role.CASHIER, capability__is_active=True).exists())
        self.assertEqual(capability_codes_for_user(self.cashier), frozenset(), 'Inactive capabilities must not be reactivated by a static fallback')

    def test_an_explicit_user_grant_still_works_when_role_is_empty(self):
        RoleCapability.objects.filter(role=User.Role.CASHIER).delete()
        cap = Capability.objects.get(code='payment.capture')
        UserCapabilityOverride.objects.create(user=self.cashier, capability=cap, effect=UserCapabilityOverride.Effect.GRANT)
        self.assertEqual(capability_codes_for_user(self.cashier), frozenset({'payment.capture'}))
