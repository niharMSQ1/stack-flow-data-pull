from django.urls import path
from .views import *


urlpatterns = [
    path("get-certfications", get_certifications),
    path("populate-database", populate_database),
    path("map-controls-with-policy", map_controls_with_policy),
    path('certifications/', certifications_view, name='certifications'),
    path("assembling-trustcloud-controls/",assembling_trustCloud_controls),
    path('pulling_eramba_certifications/', pulling_eramba_frameworkds),
    path('clause/<int:clause_id>/', clause_detail_view, name='clause_detail'),
    path("ingest-eramba-policies/", ingest_eramba_policies_view),
    path("controls-section", controlsSection, name = "controls-section"),
    path('policies/', policies_view, name='policies'),
    path('api/clause/<int:clause_id>/', clause_detail_api, name='clause-detail-api'),
    path('api/control/<int:control_id>/', control_detail_api, name='control-detail-api'),
    path('api/policy/<int:policy_id>/', policy_detail_api, name='policy_detail_api'),
    path('control/<int:id>/', control_detail, name='control_detail'),
    path("api/policy/<int:policy_id>/template/", policy_template_view, name="policy_template_view"),
    path("get-eramaba-clauses/",get_eramba_clauses),
    path("get-eramaba-controls/",get_eramba_controls),
    path("map-eramba-clauses-controls/", mapping_eramaba_clauses_controls),
    path('check-sync-lock/', check_sync_lock, name='check-sync-lock'),
    path('acquire-sync-lock/', acquire_sync_lock, name='acquire-sync-lock'),
    path('release-sync-lock/', release_sync_lock, name='release-sync-lock'),
    # path("mapping-trustCloud-controls-and-complainaces", mapping_trustCloud_controls_and_compliances, name="mapping-trustCloud-controls-and-complainaces"),
    # path("sync-now/", sync_all, name="sync-all"),
    path('assign-clause-parents/', assign_clause_parents),
    path("trust_cloud_templates/",trust_cloud_policy_templates)
]



'''
path("populate-database", populate_database),
path("populate-database", populate_database),
path("map-controls-with-policy", map_controls_with_policy),
path('pulling_eramba_certifications/', pulling_eramba_frameworkds),
path("ingest-eramba-policies/", ingest_eramba_policies_view),
path("ingest-eramba-policies/", ingest_eramba_policies_view),
path("get-eramaba-clauses/",get_eramba_clauses),
path("get-eramaba-controls/",get_eramba_controls),
'''