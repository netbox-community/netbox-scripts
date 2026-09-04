from netbox.api.routers import NetBoxRouter

from . import views

app_name = 'netbox_scripts'

router = NetBoxRouter()
router.register('modules', views.CustomScriptModuleViewSet)
router.register('project-revisions', views.ScriptProjectRevisionViewSet)
router.register('projects', views.CustomScriptProjectViewSet)
router.register('scripts', views.CustomScriptViewSet)

urlpatterns = router.urls
