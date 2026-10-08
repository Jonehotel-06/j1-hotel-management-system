# apps/hotel/management/commands/seed_demo.py
"""Seed DEVELOPMENT/DEMO data.

Loads the hotel's real identity (name, address, phone, email per the supplied
hotel information) plus clearly-labeled DEMO room types, rooms, amenities,
facilities, policies and one demo offer so the frontend can be integrated
against representative content.

DEMO PRICES AND COUNTS ARE PLACEHOLDERS — replace them with real hotel data
via the staff APIs before going live. Never run this in production against a
live database (it is idempotent but only intended for development).
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from apps.hotel.models import Facility, HotelPolicy, HotelSettings
from apps.offers.models import Offer
from apps.rooms.models import Amenity, Room, RoomType


AMENITIES = [
    ("Wi-Fi", "wifi"), ("Air Conditioning", "air-vent"), ("TV", "tv"),
    ("Refrigerator", "refrigerator"), ("Wardrobe", "cabinet"), ("Desk", "lamp-desk"),
    ("Private Bathroom", "bath"), ("Hot Water", "shower-head"), ("Room Service", "bell-concierge"),
]

FACILITIES = [
    ("Restaurant", "utensils", "On-site dining for breakfast, lunch and dinner."),
    ("Bar", "wine", "Lounge bar serving drinks and light refreshments."),
    ("Wi-Fi", "wifi", "Complimentary wireless internet throughout the property."),
    ("Parking", "car", "Free on-site guest parking."),
    ("Laundry", "washing-machine", "Same-day laundry service on request."),
    ("Room Service", "bell-concierge", "Meals and essentials delivered to your room."),
    ("Security", "shield-check", "On-site security around the clock."),
    ("Standby Power", "zap", "Backup generator for uninterrupted power supply."),
]

ROOM_TYPES = [
    {
        "name": "Standard Room",
        "short_description": "Comfortable en-suite room for short stays. (DEMO data — replace with real content)",
        "description": "A clean, comfortable room with air conditioning, TV and free Wi-Fi. "
                       "This is demo content seeded for development.",
        "base_price": "15000.00", "max_guests": 2, "bed_type": "Queen Bed", "bed_count": 1,
        "room_size": "20 m²", "view": "Street View", "display_order": 1,
    },
    {
        "name": "Deluxe Room",
        "short_description": "Spacious room with upgraded fittings and a seating area. (DEMO data)",
        "description": "Larger room with premium furnishings, refrigerator and work desk. "
                       "This is demo content seeded for development.",
        "base_price": "25000.00", "max_guests": 2, "bed_type": "King Bed", "bed_count": 1,
        "room_size": "28 m²", "view": "Courtyard View", "display_order": 2,
    },
    {
        "name": "Executive Suite",
        "short_description": "Two-room suite suitable for families or business guests. (DEMO data)",
        "description": "Separate living area, two TVs, refrigerator and dedicated workspace. "
                       "This is demo content seeded for development.",
        "base_price": "45000.00", "max_guests": 4, "bed_type": "King Bed + Sofa Bed", "bed_count": 2,
        "room_size": "45 m²", "view": "Courtyard View", "display_order": 3,
        "extra_guest_allowed": True, "extra_guest_fee": "5000.00",
    },
]

POLICIES = [
    ("check-in-out", "Check-in & Check-out", "Check-in from 14:00. Check-out by 12:00.", 1),
    ("cancellation", "Cancellation Policy",
     "Free cancellation up to 48 hours before check-in. Later cancellations may forfeit the booking deposit.", 2),
    ("identification", "Identification",
     "A valid government-issued photo ID is required at check-in for all adult guests.", 3),
    ("payment", "Payment & Deposit",
     "Online bookings are confirmed upon successful payment. Balances may be settled at the front desk by cash, POS or bank transfer.", 4),
    ("smoking", "Smoking Policy", "Smoking is not permitted inside guest rooms.", 5),
    ("pets", "Pet Policy", "Pets are not allowed on the property.", 6),
]


class Command(BaseCommand):
    help = "Seed development/demo data (hotel identity + demo content)."

    def handle(self, *args, **options):
        created = []

        settings_obj = HotelSettings.get_settings()
        self.stdout.write(f"Hotel settings ready: {settings_obj.hotel_name}")

        amenities = {}
        for name, icon in AMENITIES:
            obj, was_created = Amenity.objects.get_or_create(name=name, defaults={"icon": icon})
            amenities[name] = obj
            if was_created:
                created.append(f"amenity:{name}")

        for name, icon, description in FACILITIES:
            obj, was_created = Facility.objects.get_or_create(
                name=name, defaults={"icon": icon, "description": description}
            )
            if was_created:
                created.append(f"facility:{name}")

        for key, title, content, order in POLICIES:
            obj, was_created = HotelPolicy.objects.get_or_create(
                key=key, defaults={"title": title, "content": content, "display_order": order}
            )
            if was_created:
                created.append(f"policy:{key}")

        all_amenities = list(amenities.values())
        type_objs = {}
        for spec in ROOM_TYPES:
            defaults = {k: v for k, v in spec.items() if k != "name"}
            obj, was_created = RoomType.objects.get_or_create(name=spec["name"], defaults=defaults)
            if was_created:
                obj.amenities.set(all_amenities)
                created.append(f"room_type:{spec['name']}")
            type_objs[spec["name"]] = obj

        inventory_plan = {
            "Standard Room": [(101, 1), (102, 1), (103, 1), (104, 1)],
            "Deluxe Room": [(201, 2), (202, 2), (203, 2)],
            "Executive Suite": [(301, 3), (302, 3)],
        }
        for type_name, rooms in inventory_plan.items():
            for number, floor in rooms:
                obj, was_created = Room.objects.get_or_create(
                    room_number=str(number),
                    defaults={"room_type": type_objs[type_name], "floor": floor},
                )
                if was_created:
                    created.append(f"room:{number}")

        start = date.today()
        offer, was_created = Offer.objects.get_or_create(
            code="DEMO10",
            defaults={
                "title": "Demo Offer — 10% Off (development only)",
                "description": "Seeded development offer demonstrating percentage discounts.",
                "short_description": "10% off demo bookings.",
                "discount_type": Offer.DiscountType.PERCENTAGE,
                "discount_value": "10.00",
                "start_date": start,
                "end_date": start + timedelta(days=365),
                "min_nights": 1,
            },
        )
        if was_created:
            created.append("offer:DEMO10")

        self.stdout.write(self.style.SUCCESS(
            f"Demo seed complete. {len(created)} object(s) created."
        ))
        if created:
            self.stdout.write("Created: " + ", ".join(created))
