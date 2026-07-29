from netbox.api.routers import NetBoxRouter

from . import views

app_name = 'netbox_custom_scripts'

router = NetBoxRouter()
router.register('modules', views.CustomScriptModuleViewSet)
router.register('projects', views.CustomScriptProjectViewSet)

urlpatterns = router.urls
