from django.urls import re_path

from .consumers import ChannelConsumer

websocket_urlpatterns = [
    re_path(r"^ws/(?P<kind>transaction|user|admin|rates)/?(?P<key>[\w-]*)$",
            ChannelConsumer.as_asgi()),
]
