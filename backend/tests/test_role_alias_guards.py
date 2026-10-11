"""D5-003 / N8-004: keep role-alias handling in one place.

`User.Role` carries two pairs of identifiers with identical capability grants:

    HOUSEKEEPING  <-> HOUSEKEEPER
    MAINTENANCE   <-> MAINTENANCE_TECHNICIAN

Workflow code that compared a user's role against ONE literal member of a pair
silently excluded real accounts: a HOUSEKEEPER could not be assigned or claim a
housekeeping task, and the maintenance pair had the identical latent defect.
`apps.accounts.capabilities` is the single place that maps alias onto canonical
role, and workflow code must ask `is_housekeeping_worker` / `is_maintenance_worker`
(or test membership in the `*_WORKER_ROLES` sets) instead of comparing literals.

The behavioural tests for that fix live in `test_maintenance.py`,
`test_guest_services.py` and `test_housekeeping.py`. This module is the cheap
regression net: it fails the moment a new literal role comparison is written
into application code, which is how the defect entered in the first place.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

from apps.accounts import capabilities

ALIAS_PAIR_CODES = (
    "HOUSEKEEPING",
    "HOUSEKEEPER",
    "MAINTENANCE",
    "MAINTENANCE_TECHNICIAN",
)

# Files allowed to name the identifiers directly. `capabilities.py` declares the
# mapping; `models.py` declares the enum; migrations rewrite historical rows.
EXEMPT_PATHS = {
    "apps/accounts/capabilities.py",
    "apps/accounts/models.py",
}

# A line is only a *decision* about a role if it compares or filters on one.
DECISION_TOKENS = ("==", "!=", " in ", " in(", "filter(", "exclude(", "get(", "if ", "?:")
# ...unless it routes through the alias mapping, which is the required pattern.
ALIAS_ROUTING_TOKENS = (
    "HOUSEKEEPING_WORKER_ROLES",
    "MAINTENANCE_WORKER_ROLES",
    "is_housekeeping_worker",
    "is_maintenance_worker",
    "HOUSEKEEPING_ROLE_ALIASES",
    "MAINTENANCE_ROLE_ALIASES",
)

APPS_ROOT = Path(__file__).resolve().parent.parent / "apps"
ROLE_COMPARISON = re.compile(r"\.role\s*(==|!=)")


def _iter_source_files():
    for path in sorted(APPS_ROOT.rglob("*.py")):
        relative = path.relative_to(APPS_ROOT.parent).as_posix()
        if "/migrations/" in relative or relative in EXEMPT_PATHS:
            continue
        yield relative, path


class RoleAliasSourceGuardTests(SimpleTestCase):
    def test_no_workflow_code_compares_a_role_against_one_alias_code(self):
        offenders = []
        for relative, path in _iter_source_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                code = line.split("#", 1)[0]
                # Only *role* decisions are in scope: Room.Status.MAINTENANCE and
                # ServiceRequest.Category.HOUSEKEEPING are different concepts.
                if "role" not in code.lower():
                    continue
                if not any(re.search(rf"\b{c}\b", code) for c in ALIAS_PAIR_CODES):
                    continue
                if any(token in code for token in ALIAS_ROUTING_TOKENS):
                    continue
                if not any(token in code for token in DECISION_TOKENS):
                    continue
                offenders.append(f"{relative}:{number}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "compare against the alias sets in apps/accounts/capabilities.py instead:\n"
            + "\n".join(offenders),
        )

    def test_no_role_attribute_equality_comparison_against_a_literal_string(self):
        offenders = []
        for relative, path in _iter_source_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                code = line.split("#", 1)[0]
                if not ROLE_COMPARISON.search(code):
                    continue
                right = code.split("==", 1)[-1] if "==" in code else code
                if right.strip().startswith(('"', "'", "User.Role.")) and not any(
                    token in code for token in ALIAS_ROUTING_TOKENS
                ):
                    # ADMIN and GUEST are single-member codes; the alias pairs are not.
                    if any(re.search(rf"\b{c}\b", code) for c in ALIAS_PAIR_CODES):
                        offenders.append(f"{relative}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class RoleAliasMappingTests(SimpleTestCase):
    """The mapping itself must keep both members, or the guards above are theatre."""

    def test_both_housekeeping_codes_are_workers(self):
        self.assertIn("HOUSEKEEPING", capabilities.HOUSEKEEPING_WORKER_ROLES)
        self.assertIn("HOUSEKEEPER", capabilities.HOUSEKEEPING_WORKER_ROLES)
        self.assertIn("HOUSEKEEPER", capabilities.HOUSEKEEPING_ROLE_ALIASES)

    def test_both_maintenance_codes_are_workers(self):
        self.assertIn("MAINTENANCE", capabilities.MAINTENANCE_WORKER_ROLES)
        self.assertIn("MAINTENANCE_TECHNICIAN", capabilities.MAINTENANCE_WORKER_ROLES)
        self.assertIn("MAINTENANCE_TECHNICIAN", capabilities.MAINTENANCE_ROLE_ALIASES)

    def test_the_helpers_accept_both_spellings(self):
        class Account:
            def __init__(self, role):
                self.role = role

        for role in ("HOUSEKEEPING", "HOUSEKEEPER"):
            with self.subTest(role=role):
                self.assertTrue(capabilities.is_housekeeping_worker(Account(role)))
        for role in ("MAINTENANCE", "MAINTENANCE_TECHNICIAN"):
            with self.subTest(role=role):
                self.assertTrue(capabilities.is_maintenance_worker(Account(role)))

    def test_an_unrelated_role_is_not_a_worker(self):
        class Account:
            role = "RECEPTIONIST"

        self.assertFalse(capabilities.is_housekeeping_worker(Account()))
        self.assertFalse(capabilities.is_maintenance_worker(Account()))

    def test_the_grant_codes_of_an_alias_pair_are_identical(self):
        grants = capabilities.ROLE_CAPABILITY_CODES
        self.assertEqual(grants["HOUSEKEEPING"], grants["HOUSEKEEPER"])
        self.assertEqual(grants["MAINTENANCE"], grants["MAINTENANCE_TECHNICIAN"])
