from django.urls import path
from . import views
app_name = "housekeeping"
urlpatterns = [
    path("", views.HousekeepingTaskListCreateView.as_view(), name="tasks"),
    path("<str:reference>/", views.HousekeepingTaskDetailView.as_view(), name="task-detail"),
    path("<str:reference>/assign/", views.HousekeepingTaskAssignView.as_view(), name="task-assign"),
    path("<str:reference>/claim/", views.HousekeepingTaskClaimView.as_view(), name="task-claim"),
    path("<str:reference>/status/", views.HousekeepingTaskStatusView.as_view(), name="task-status"),
    path("<str:reference>/comments/", views.HousekeepingTaskCommentView.as_view(), name="task-comments"),
]
