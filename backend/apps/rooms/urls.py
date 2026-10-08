# apps/rooms/urls.py
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views, views_admin

app_name = "rooms"

# Public catalog ("rooms" == room types for guests)
urlpatterns = [
    path("", views.RoomTypeListView.as_view(), name="room-type-list"),
    # Registered BEFORE the room-type slug route so "rooms/" is never captured
    # by <slug:slug>/.
    path("<slug:slug>/rooms/", views.RoomTypeRoomsView.as_view(), name="room-type-rooms"),
    path("<slug:slug>/unavailable-dates/", views.RoomTypeUnavailableDatesView.as_view(), name="room-type-unavailable-dates"),
    path("<slug:slug>/", views.RoomTypeDetailView.as_view(), name="room-type-detail"),
]

# Staff management routers
_room_type_router = DefaultRouter()
_room_type_router.register("", views_admin.RoomTypeAdminViewSet, basename="admin-room-type")

_room_router = DefaultRouter()
_room_router.register("", views_admin.RoomAdminViewSet, basename="admin-room")

_amenity_router = DefaultRouter()
_amenity_router.register("", views_admin.AmenityAdminViewSet, basename="admin-amenity")

admin_room_types_urlpatterns = [
    # Registered BEFORE the router so "images/<pk>/" is not captured by the
    # router's "<pk>/" detail route.
    path("images/<int:pk>/", views_admin.RoomTypeImageDetailView.as_view(), name="room-type-image-detail"),
    path("<int:room_type_id>/images/", views_admin.RoomTypeImageUploadView.as_view(), name="room-type-image-upload"),
] + _room_type_router.urls
admin_rooms_urlpatterns = [
    # Physical-room image management (RoomImage — distinct from RoomTypeImage).
    path("images/<int:pk>/", views_admin.RoomImageDetailView.as_view(), name="room-image-detail"),
    path("<int:room_id>/images/", views_admin.RoomImageUploadView.as_view(), name="room-image-upload"),
] + _room_router.urls
admin_amenities_urlpatterns = _amenity_router.urls
# Legacy namespace kept for backward compatibility with older clients:
# /api/admin/room-images/{pk}/ resolved to the ROOM-TYPE image detail in the
# shipped dashboard contract, so it must keep doing so.
admin_room_images_urlpatterns = [
    path("room/<int:room_id>/", views_admin.RoomImageUploadView.as_view(), name="legacy-room-image-upload"),
    path("<int:pk>/", views_admin.RoomTypeImageDetailView.as_view(), name="legacy-room-type-image-detail"),
]
