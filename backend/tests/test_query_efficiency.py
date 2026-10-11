# tests/test_query_efficiency.py
"""Task 24: the list endpoints this project added or changed must not issue a
query per row. Each test asserts the query count is stable as the number of
rows grows — the only reliable way to catch an N+1 regression.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.audit.services import log_action
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction
from apps.finance.services.ledger_service import create_posted_transaction
from apps.housekeeping.models import HousekeepingTask
from apps.housekeeping.services.task_service import create_housekeeping_task
from apps.maintenance.models import MaintenanceWorkOrder
from apps.maintenance.services.work_order_service import create_work_order
from apps.notifications.models import Notification
from apps.notifications.services import notify_users
from apps.offers.models import GuestDiscount, Offer
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services.order_service import create_order, submit_order

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_booking,
    make_guest,
    make_room,
    make_room_type,
    make_staff,
)


class QueryEfficiencyTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.room_type = make_room_type("Efficiency", price="20000.00")
        self.rooms = [make_room(self.room_type, f"E{i:02d}") for i in range(1, 13)]
        self.manager = make_staff("qe.manager@staff.dev", role=User.Role.MANAGER)
        self.auth(self.manager)
        self.today = hotel_today()

    def _guests(self, count, prefix):
        return [make_guest(f"{prefix}{i}@example.com") for i in range(count)]

    def _count(self, url):
        """Queries issued by one GET, excluding first-request warm-up.

        The hotel settings singleton and similar per-process caches are filled
        on the first call, so an unwarmed measurement would overstate the
        baseline and hide a real N+1.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.get(url)  # warm caches
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return len(ctx.captured_queries)

    def test_public_room_option_availability_query_count_is_flat(self):
        """One blocked-ID query must serve every physical room in the response."""
        check_in = self.today + timedelta(days=10)
        check_out = check_in + timedelta(days=3)
        url = (
            f"/api/rooms/{self.room_type.slug}/rooms/?"
            f"check_in={check_in.isoformat()}&check_out={check_out.isoformat()}"
        )
        few = self._count(url)

        for index in range(13, 21):
            make_room(self.room_type, f"E{index:02d}")
        many = self._count(url)

        self.assertEqual(few, many, "physical room options issue a blocking query per room")

    def test_public_availability_query_count_is_flat_across_room_types(self):
        """Range search batches inventory and public-offer work by room type."""
        check_in = self.today + timedelta(days=10)
        check_out = check_in + timedelta(days=3)
        Offer.objects.create(
            title="Efficiency public", discount_type=Offer.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"), start_date=self.today,
            end_date=self.today + timedelta(days=60),
        )
        url = (
            "/api/rooms/availability/?"
            f"check_in={check_in.isoformat()}&check_out={check_out.isoformat()}&guests=2&rooms=1"
        )
        few = self._count(url)

        for index in range(1, 9):
            room_type = make_room_type(f"Efficiency extra {index}", price="20000.00")
            make_room(room_type, f"X{index:02d}")
        many = self._count(url)

        self.assertEqual(few, many, "availability search issues work per room type")

    def test_guests_list_query_count_is_flat(self):
        """Per-guest booking counts must be annotated, not looped."""
        for index, guest in enumerate(self._guests(2, "qe.few")):
            make_booking(guest, self.room_type, [self.rooms[index]])
        few = self._count("/api/admin/guests/")

        for index, guest in enumerate(self._guests(8, "qe.many")):
            make_booking(guest, self.room_type, [self.rooms[index + 2]])
        many = self._count("/api/admin/guests/")

        self.assertEqual(few, many, "guest list issues a query per guest")

    def test_occupancy_calendar_query_count_is_flat(self):
        guest = make_guest("qe.occ@example.com")
        make_booking(guest, self.room_type, self.rooms[:1],
                     check_in=self.today + timedelta(days=2))
        few = self._count(f"/api/admin/bookings/calendar/?year={self.today.year}&month={self.today.month}")

        for offset, room in enumerate(self.rooms[1:9]):
            make_booking(make_guest(f"qe.occ{offset}@example.com"), self.room_type, [room],
                         check_in=self.today + timedelta(days=2 + offset % 3))
        many = self._count(f"/api/admin/bookings/calendar/?year={self.today.year}&month={self.today.month}")

        self.assertEqual(few, many, "occupancy calendar issues a query per booking/room")

    def test_missed_bookings_query_count_is_flat(self):
        def missed(email, room):
            return make_booking(
                make_guest(email), self.room_type, [room],
                check_in=self.today - timedelta(days=4),
                check_out=self.today - timedelta(days=2),
                amount_paid="20000.00",
            )

        missed("qe.miss0@example.com", self.rooms[0])
        few = self._count("/api/admin/bookings/missed/")

        for index in range(1, 9):
            missed(f"qe.miss{index}@example.com", self.rooms[index])
        many = self._count("/api/admin/bookings/missed/")

        self.assertEqual(few, many, "missed bookings issues a query per booking")

    def test_guest_discount_list_query_count_is_flat(self):
        def discount(email):
            guest = make_guest(email)
            return GuestDiscount.objects.create(
                guest=guest, discount_type=GuestDiscount.DiscountType.PERCENTAGE,
                discount_value=Decimal("10.00"), start_date=self.today,
                created_by=self.manager, reason="Loyalty",
            )

        discount("qe.gd0@example.com")
        few = self._count("/api/admin/guest-discounts/")

        for index in range(1, 9):
            discount(f"qe.gd{index}@example.com")
        many = self._count("/api/admin/guest-discounts/")

        self.assertEqual(few, many, "guest discount list issues a query per row")


class AdminListFlatnessTests(BaseAPITestCase):
    """The admin tables the dashboard actually loads.

    Same method as above, tightened in two ways: the endpoint is asked for a
    full 100-row page, and 20 further rows are added between the two
    measurements - so a serializer that queries once per row shows up as a
    ~20-query jump instead of hiding behind pagination. `_count` also fails if it
    captures nothing, because a guard that measured zero queries twice would pass
    no matter what the endpoint did.
    """

    def setUp(self):
        super().setUp()
        hotel_settings()
        self.today = hotel_today()
        self.admin = make_staff("qe.admin@staff.dev", role=User.Role.ADMIN)
        self.room_type = make_room_type("Flatness", price="18000.00")
        self.category = MenuCategory.objects.create(name="Flatness menu")
        self.menu_item = MenuItem.objects.create(
            category=self.category, name="Jollof", sku="QE-JOLLOF", base_price=Decimal("2000.00"),
        )
        self.next_index = 0
        self._grow(3, only="everything")
        self.auth(self.admin)

    def _grow(self, count, *, only):
        """Create `count` valid rows of the entity family a guarded list reads.

        Only what the endpoint under test lists is created, so the guard stays
        sensitive without seeding every other module on each call. Every row is
        built through the real service layer, so the listed data is valid ERP
        data rather than raw fixtures.
        """
        needs_room = only in ("everything", "rooms", "bookings", "housekeeping", "maintenance")
        for index in range(self.next_index, self.next_index + count):
            if needs_room:
                room = make_room(self.room_type, f"F{index:03d}")
            if only in ("everything", "bookings"):
                make_booking(
                    make_guest(f"flat{index}@example.com"), self.room_type, [room],
                    check_in=self.today + timedelta(days=1), check_out=self.today + timedelta(days=3),
                    total="36000.00",
                )
            if only in ("everything", "users"):
                make_staff(f"qe.staff{index}@staff.dev", role=User.Role.RECEPTIONIST)
            if only in ("everything", "audit"):
                log_action(actor=self.admin, action="QE_FLATNESS", summary=f"row {index}")
            if only in ("everything", "notifications"):
                notify_users(
                    [self.admin], type=Notification.Type.BOOKING_CREATED,
                    title=f"Row {index}", message=f"Flatness row {index}",
                )
            if only in ("everything", "housekeeping"):
                create_housekeeping_task(
                    room=room, task_type=HousekeepingTask.Type.values[0], summary=f"Clean {index}",
                    actor=self.admin, source_key=f"qe-hk-{index}",
                )
            if only in ("everything", "maintenance"):
                create_work_order(
                    category=MaintenanceWorkOrder.Category.values[0], summary=f"Fix {index}",
                    room=room, actor=self.admin, source_key=f"qe-mnt-{index}",
                )
            if only in ("everything", "pos", "kitchen"):
                order, _ = create_order(
                    mode=PosOrder.Mode.RESTAURANT,
                    lines=[{"menu_item_id": self.menu_item.pk, "quantity": 1}],
                    actor=self.admin, idempotency_key=f"qe-pos-{index}",
                )
                if only in ("everything", "kitchen"):
                    submit_order(order=order, actor=self.admin)
            if only in ("everything", "transactions"):
                create_posted_transaction(
                    transaction_type=FinancialTransaction.Type.POS_CHARGE,
                    lines=[
                        {"account_code": "REVENUE.FOOD_BEVERAGE",
                         "direction": FinancialLine.Direction.CREDIT, "amount": Decimal("2000.00")},
                        {"account_code": "CASH.ON_HAND",
                         "direction": FinancialLine.Direction.DEBIT, "amount": Decimal("2000.00")},
                    ],
                    actor=self.admin, source_key=f"qe-txn-{index}", narrative=f"Sale {index}",
                )
        self.next_index += count

    def _count(self, url):
        self.client.get(url)  # warm the settings/content caches
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
            queries = len(ctx.captured_queries)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertGreater(
            queries, 0,
            f"{url} captured no queries - the guard would pass vacuously",
        )
        return queries

    def _assert_flat(self, url, label, *, only, added=20):
        few = self._count(url)
        self._grow(added, only=only)
        many = self._count(url)
        self.assertEqual(
            few, many,
            f"{label} issues {(many - few) / added:.2f} queries per rendered row "
            f"({few} at {self.next_index - added} rows, {many} at {self.next_index} rows)",
        )

    def test_bookings_list_query_count_is_flat(self):
        self._assert_flat("/api/admin/bookings/?page_size=100", "the bookings list", only="bookings")

    def test_rooms_list_query_count_is_flat(self):
        self._assert_flat("/api/admin/rooms/?page_size=100", "the rooms list", only="rooms")

    def test_users_list_query_count_is_flat(self):
        self._assert_flat("/api/admin/users/?page_size=100", "the staff account list", only="users")

    def test_staff_directory_query_count_is_flat(self):
        self._assert_flat("/api/admin/users/directory/?page_size=100", "the staff picker directory", only="users")

    def test_audit_log_list_query_count_is_flat(self):
        self._assert_flat("/api/admin/audit-logs/?page_size=100", "the audit log list", only="audit")

    def test_notification_feed_query_count_is_flat(self):
        self._assert_flat("/api/notifications/?page_size=100", "the notification feed", only="notifications")

    def test_housekeeping_queue_query_count_is_flat(self):
        self._assert_flat("/api/admin/housekeeping/?page_size=100", "the housekeeping queue", only="housekeeping")

    def test_maintenance_queue_query_count_is_flat(self):
        self._assert_flat("/api/admin/maintenance/?page_size=100", "the maintenance queue", only="maintenance")

    def test_pos_order_list_query_count_is_flat(self):
        self._assert_flat("/api/admin/pos/orders/?page_size=100", "the POS order list", only="pos")

    def test_kitchen_ticket_queue_query_count_is_flat(self):
        self._assert_flat("/api/admin/pos/kitchen-tickets/?page_size=100", "the kitchen queue", only="kitchen")

    def test_finance_transaction_list_query_count_is_flat(self):
        self._assert_flat("/api/admin/finance/transactions/?page_size=100", "the ledger transaction list",
                          only="transactions")
