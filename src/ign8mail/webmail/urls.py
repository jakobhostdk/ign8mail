from django.urls import path

from . import views

urlpatterns = [
    path("", views.unified_inbox, name="index"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("compose/", views.compose_view, name="compose"),
    path("m/<str:name>/action/", views.mailbox_action, name="mailbox_action"),
    path("m/<str:name>/<int:uid>/", views.message_view, name="message"),
    path("m/<str:name>/", views.mailbox_view, name="mailbox"),
]
