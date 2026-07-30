from django.urls import include, path

from utilities.urls import get_model_urls

from . import views  # noqa: F401

urlpatterns: tuple = (
    path(
        'modules/',
        include(get_model_urls('netbox_custom_scripts', 'customscriptmodule', detail=False)),
    ),
    path(
        'modules/<int:pk>/',
        include(get_model_urls('netbox_custom_scripts', 'customscriptmodule')),
    ),
    path(
        'projects/',
        include(get_model_urls('netbox_custom_scripts', 'customscriptproject', detail=False)),
    ),
    path(
        'projects/<int:pk>/',
        include(get_model_urls('netbox_custom_scripts', 'customscriptproject')),
    ),
    # Detail only: Custom Scripts have no list view until the full object surface lands.
    path(
        'scripts/<int:pk>/',
        include(get_model_urls('netbox_custom_scripts', 'customscript')),
    ),
)
