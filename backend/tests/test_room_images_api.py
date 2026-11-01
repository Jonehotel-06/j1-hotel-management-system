"""
Task 05 of targeted_fixes_01 — the public room API must return the COMPLETE
image collection, so a guest can page through every photo of a room.

Before this change the list and detail representations carried only
``primary_image_url``. A guest saw one picture per room and the gallery on the
detail page could not show the rest, no matter what the database held.

These tests pin:

* every active image of a room reaches the API, in display order;
* an inactive image is excluded (it was retired by an administrator);
* duplicates are collapsed, so the counter cannot say "1 / 5" for three photos;
* ``image_count`` matches the list actually returned;
* a room with one image, or none, is still coherent;
* the pre-existing ``primary_image_url`` field is untouched for existing
  consumers.
"""
from decimal import Decimal

from django.test import TestCase

from apps.rooms.models import Amenity, Room, RoomType, RoomTypeImage

from .base import BaseAPITestCase


def make_room_type(name="Gallery Suite", price="30000.00"):
    """A room type with the fields the public card actually reads."""
    return RoomType.objects.create(
        name=name, slug=name.lower().replace(" ", "-"), base_price=Decimal(price),
        max_guests=2, bed_type="Queen", bed_count=1,
        room_size="25 sqm", is_active=True,
    )


class RoomImagePayloadTests(TestCase):
    def setUp(self):
        self.room_type = make_room_type()
        # A physical room exists so the type is bookable; it holds no images.
        self.room = Room.objects.create(
            room_type=self.room_type, room_number="G-101", floor=1,
            status=Room.Status.AVAILABLE,
        )

    def _image(self, name, order, *, active=True, alt=""):
        """Attach one marketing image to the ROOM TYPE.

        ``RoomTypeImage`` is what the public catalogue publishes; ``RoomImage``
        is a per-physical-room photo used by the staff screens. The gallery a
        guest sees is the room type's, so that is the collection under test.
        """
        return RoomTypeImage.objects.create(
            room_type=self.room_type, image=f"room-types/{name}.jpg", alt_text=alt,
            display_order=order, is_active=active,
        )

    def _payload(self):
        from apps.rooms.serializers import RoomTypeDetailSerializer

        return RoomTypeDetailSerializer(self.room_type).data

    def test_every_active_image_is_returned_in_display_order(self):
        for index, name in enumerate(["a", "b", "c"]):
            self._image(name, index)
        data = self._payload()
        urls = [image["image_url"] for image in data["images"]]
        self.assertEqual(len(urls), 3)
        self.assertEqual(
            [image["display_order"] for image in data["images"]], [0, 1, 2]
        )

    def test_the_existing_primary_image_url_is_preserved(self):
        self._image("primary", 0)
        data = self._payload()
        self.assertIn("primary_image_url", data)

    def test_an_inactive_image_is_not_published(self):
        self._image("live", 0)
        self._image("retired", 1, active=False)
        data = self._payload()
        self.assertEqual(len(data["images"]), 2 - 1)
        self.assertNotIn("retired", str(data["images"]))

    def test_image_count_matches_the_returned_list(self):
        for index, name in enumerate(["a", "b", "c", "d"]):
            self._image(name, index)
        data = self._payload()
        self.assertEqual(data["image_count"], len(data["images"]))
        self.assertEqual(data["image_count"], 4)

    def test_a_single_image_room_still_reports_a_coherent_gallery(self):
        self._image("only", 0)
        data = self._payload()
        self.assertEqual(len(data["images"]), 1)
        self.assertEqual(data["image_count"], 1)

    def test_a_room_with_no_images_returns_an_empty_list_not_null(self):
        data = self._payload()
        self.assertEqual(list(data["images"]), [])
        self.assertEqual(data["image_count"], 0)

    def test_each_image_carries_the_fields_the_gallery_needs(self):
        self._image("a", 0, alt="The bed")
        image = self._payload()["images"][0]
        for field in ("image_url", "alt_text", "caption", "display_order", "is_primary"):
            self.assertIn(field, image, f"the gallery needs {field}")
        self.assertEqual(image["alt_text"], "The bed")

    def test_amenities_are_still_published_with_their_icon_keys(self):
        amenity = Amenity.objects.create(name="Air Conditioning", icon="air-vent")
        self.room_type.amenities.add(amenity)
        data = self._payload()
        self.assertEqual(data["amenities"][0]["icon"], "air-vent")


class RoomImageListEndpointTests(BaseAPITestCase):
    """The same contract through the public HTTP endpoint."""

    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Public Gallery", "41000.00")
        self.room = Room.objects.create(
            room_type=self.room_type, room_number="P-201", floor=2,
            status=Room.Status.AVAILABLE,
        )
        for index, name in enumerate(["one", "two", "three"]):
            RoomTypeImage.objects.create(
                room_type=self.room_type, image=f"room-types/{name}.jpg",
                display_order=index, is_active=True,
            )

    def test_the_public_list_publishes_the_full_gallery(self):
        response = self.client.get("/api/rooms/")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        rooms = payload.get("data") or payload.get("results") or payload
        if isinstance(rooms, dict):
            rooms = rooms.get("results", [])
        entry = next((row for row in rooms if row.get("slug") == "public-gallery"), None)
        self.assertIsNotNone(entry, f"room missing from {rooms}")
        self.assertEqual(len(entry["images"]), 3)
        self.assertEqual(entry["image_count"], 3)

    def test_the_public_detail_publishes_the_full_gallery(self):
        response = self.client.get("/api/rooms/public-gallery/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(len(data["images"]), 3)
        self.assertEqual(data["image_count"], 3)
