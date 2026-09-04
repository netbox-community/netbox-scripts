from netbox.api.routers import NetBoxRouter

from . import views

app_name = 'netbox_scripts'

router = NetBoxRouter()
router.register('project-revisions', views.ScriptProjectRevisionViewSet)
router.register('projects', views.ScriptProjectViewSet)
router.register('script-files', views.ScriptFileViewSet)
router.register('scripts', views.CustomScriptViewSet)

urlpatterns = router.urls
