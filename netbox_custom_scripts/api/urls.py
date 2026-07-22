from netbox.api.routers import NetBoxRouter

from . import views

app_name = 'netbox_custom_scripts'

router = NetBoxRouter()
router.register('custom-script-projects', views.CustomScriptProjectViewSet)

urlpatterns = router.urls
