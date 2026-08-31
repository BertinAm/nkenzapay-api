from django.apps import AppConfig


class CommonConfig(AppConfig):
    """No models. The app exists so that the deployment checks and the media
    commands are registered."""

    name = "nkenzapay.common"
    label = "nkenzapay_common"
    verbose_name = "Common"

    def ready(self):
        from . import checks  # noqa: F401 - importing is what registers them
