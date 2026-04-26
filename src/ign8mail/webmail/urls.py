from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("<str:name>/action/", views.mailbox_action, name="mailbox_action"),
    path("<str:name>/<str:seq>/", views.message_view, name="message"),
    path("<str:name>/", views.mailbox_view, name="mailbox"),
]
