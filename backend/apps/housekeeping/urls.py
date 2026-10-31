from django.urls import path
from . import views, asset_views
app_name = "housekeeping"
urlpatterns = [
    path("assets/catalog/", asset_views.CatalogView.as_view()),
    path("assets/configuration/", asset_views.ConfigurationView.as_view()),
    path("assets/queue/", asset_views.QueueView.as_view()),
    path("assets/inspections/", asset_views.InspectionStartView.as_view()),
    path("assets/inspections/<int:pk>/", asset_views.InspectionView.as_view()),
    path("assets/discrepancies/", asset_views.ReportView.as_view()),
    path("assets/discrepancies/<int:pk>/reviews/", asset_views.ReviewView.as_view()),
    path("", views.HousekeepingTaskListCreateView.as_view(), name="tasks"),
    path("<str:reference>/", views.HousekeepingTaskDetailView.as_view(), name="task-detail"),
    path("<str:reference>/assign/", views.HousekeepingTaskAssignView.as_view(), name="task-assign"),
    path("<str:reference>/claim/", views.HousekeepingTaskClaimView.as_view(), name="task-claim"),
    path("<str:reference>/status/", views.HousekeepingTaskStatusView.as_view(), name="task-status"),
    path("<str:reference>/comments/", views.HousekeepingTaskCommentView.as_view(), name="task-comments"),
]
