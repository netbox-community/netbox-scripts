from django.urls import include, path

from utilities.urls import get_model_urls

from . import views

urlpatterns: tuple = (
    # The passes act on the built-in feature rather than on a model of this plugin, so their
    # routes are declared here rather than through get_model_urls.
    path('migration/', views.MigrationView.as_view(), name='migration'),
    path('migration/inventory/', views.MigrationInventoryView.as_view(), name='migration_inventory'),
    path('migration/stage/', views.MigrationStagingView.as_view(), name='migration_stage'),
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
    # Detail and actions only: revisions are history, so they are read from a project's
    # Revisions tab and carry no list, edit, or delete route of their own.
    path(
        'revisions/<int:pk>/',
        include(get_model_urls('netbox_custom_scripts', 'customscriptprojectrevision')),
    ),
    path(
        'scripts/',
        include(get_model_urls('netbox_custom_scripts', 'customscript', detail=False)),
    ),
    path(
        'scripts/<int:pk>/',
        include(get_model_urls('netbox_custom_scripts', 'customscript')),
    ),
)
