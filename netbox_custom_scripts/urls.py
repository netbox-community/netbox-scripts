from django.urls import include, path

from utilities.urls import get_model_urls

from . import views  # noqa: F401

urlpatterns: tuple = (
    path(
        'custom-script-projects/',
        include(get_model_urls('netbox_custom_scripts', 'customscriptproject', detail=False)),
    ),
    path(
        'custom-script-projects/<int:pk>/',
        include(get_model_urls('netbox_custom_scripts', 'customscriptproject')),
    ),
)
