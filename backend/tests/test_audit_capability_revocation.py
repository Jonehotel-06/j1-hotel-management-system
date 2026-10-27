"""Disposable regression: a persisted empty role must mean no baseline grants."""
from django.test import TestCase
from rest_framework.test import APIClient
from apps.accounts.capabilities import capability_codes_for_user
from apps.accounts.models import Capability, RoleCapability, User, UserCapabilityOverride


class EmptyRoleGrantsRegression(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email='matrix-admin@example.invalid', role=User.Role.ADMIN)
        self.waiter = User.objects.create_user(email='matrix-waiter@example.invalid', role=User.Role.WAITER)

    def test_revoke_last_grant_in_management_api_remains_revoked(self):
        RoleCapability.objects.filter(role=User.Role.WAITER).exclude(capability__code='payment.capture').delete()
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post('/api/admin/users/permissions-matrix/', {'role':'WAITER', 'capability_code':'payment.capture', 'effect':'REVOKE'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(RoleCapability.objects.filter(role=User.Role.WAITER).exists())
        self.waiter.refresh_from_db()
        self.assertEqual(capability_codes_for_user(self.waiter), frozenset(), 'An empty persisted role must not silently regrant the code defaults')

    def test_inactive_role_capabilities_are_not_restored_by_fallback(self):
        Capability.objects.filter(role_grants__role=User.Role.WAITER).update(is_active=False)
        self.assertFalse(RoleCapability.objects.filter(role=User.Role.WAITER, capability__is_active=True).exists())
        self.assertEqual(capability_codes_for_user(self.waiter), frozenset(), 'Inactive capabilities must not be reactivated by a static fallback')

    def test_an_explicit_user_grant_still_works_when_role_is_empty(self):
        RoleCapability.objects.filter(role=User.Role.WAITER).delete()
        cap = Capability.objects.get(code='payment.capture')
        UserCapabilityOverride.objects.create(user=self.waiter, capability=cap, effect=UserCapabilityOverride.Effect.GRANT)
        self.assertEqual(capability_codes_for_user(self.waiter), frozenset({'payment.capture'}))
