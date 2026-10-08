# tests/factories.py
"""Shared test helpers."""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache

from apps.accounts.models import User
from apps.bookings.models import Booking, BookingRoom, Guest
from apps.core.utils import hotel_today
from apps.hotel.models import HotelSettings
from apps.rooms.models import Room, RoomType


def clear_cache():
    cache.clear()


def hotel_settings(**overrides):
    settings_obj = HotelSettings.get_settings()
    for key, value in overrides.items():
        setattr(settings_obj, key, value)
    settings_obj.save()
    cache.clear()
    return settings_obj


def make_user(email="guest@example.com", password="Passw0rd!234", role=User.Role.GUEST, **extra):
    return User.objects.create_user(
        email=email, password=password, first_name=extra.pop("first_name", "Test"),
        last_name=extra.pop("last_name", "User"), role=role, **extra,
    )


def make_staff(email, role=User.Role.RECEPTIONIST):
    user = make_user(email=email, role=role, first_name="Staff", last_name=role.title())
    return user


def make_room_type(name="Standard", price="15000.00", max_guests=2, **extra):
    room_type = RoomType.objects.create(
        name=name, base_price=Decimal(price), max_guests=max_guests,
        short_description=f"{name} room", **extra,
    )
    return room_type


def make_room(room_type, number, status=Room.Status.AVAILABLE, floor=1):
    return Room.objects.create(
        room_type=room_type, room_number=str(number), floor=floor, status=status,
    )


def make_guest(email="guest@test.dev", user=None, **extra):
    defaults = dict(
        first_name="Ada", last_name="Obi", email=email, phone="08031234567", country="Nigeria",
    )
    defaults.update(extra)
    return Guest.objects.create(user=user, **defaults)


def make_booking(guest, room_type, rooms=None, *, check_in=None, check_out=None,
                 status=Booking.Status.CONFIRMED, amount_paid="0.00", number_of_rooms=1,
                 expires_at=None, total="15000.00"):
    today = hotel_today()
    check_in = check_in or today + timedelta(days=10)
    check_out = check_out or check_in + timedelta(days=3)
    booking = Booking.objects.create(
        booking_reference=f"J1-TEST-{Booking.objects.count()+1:05d}",
        guest=guest, room_type=room_type, check_in=check_in, check_out=check_out,
        number_of_rooms=number_of_rooms, adults=2, children=0,
        currency="NGN", price_per_night=room_type.base_price,
        subtotal=Decimal(total), discount_amount=Decimal("0.00"),
        extra_guest_fee_amount=Decimal("0.00"), tax_amount=Decimal("0.00"),
        fee_amount=Decimal("0.00"), total_amount=Decimal(total),
        required_payment=Decimal(total), amount_paid=Decimal(amount_paid),
        status=status, expires_at=expires_at,
    )
    if rooms:
        for room in rooms:
            BookingRoom.objects.create(
                booking=booking, room=room, check_in=check_in, check_out=check_out,
            )
    return booking
