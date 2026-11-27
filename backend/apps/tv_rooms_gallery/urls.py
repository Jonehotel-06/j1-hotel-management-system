from django.urls import path
from . import views

urlpatterns = [
    path('catalog/', views.Catalog.as_view()),
    path('media/<int:pk>/<str:rendition>/', views.PublicFile.as_view(), name='tv-gallery-public-file'),
    path('manage/rooms/', views.AdminRooms.as_view()),
    path('manage/media/', views.AdminMedia.as_view()),
    path('manage/media/<int:pk>/', views.AdminMediaItem.as_view()),
    path('manage/media/<int:pk>/retry-processing/', views.AdminRetryVideo.as_view()),
    path('manage/media/<int:pk>/rebuild-renditions/', views.AdminRebuildRenditions.as_view()),
    path('manage/media/<int:pk>/<str:rendition>/', views.AdminFile.as_view()),
    path('manage/settings/', views.AdminSettings.as_view()),
]
