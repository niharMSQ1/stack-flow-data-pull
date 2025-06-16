from django.urls import path
from .views import *

urlpatterns = [
    path("get-certfications", get_certifications),
    path("populate-database", populate_database),
    path("map-controls-with-policy", map_controls_with_policy),
    path('certifications/', certifications_view, name='certifications'),
    path('clause/<int:clause_id>/', clause_detail_view, name='clause_detail'),
    path('policies/', policies_view, name='policies'),
    path('api/clause/<int:clause_id>/', clause_detail_api, name='clause-detail-api'),
    path('api/control/<int:control_id>/', control_detail_api, name='control-detail-api'),
    path('api/policy/<int:policy_id>/', policy_detail_api, name='policy_detail_api'),
    path('control/<int:id>/', control_detail, name='control_detail')

]