from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('cv/upload/', views.upload_cv, name='upload_cv'),
    path('cv/create/', views.create_profile, name='create_profile'),
    path('cv/<int:cv_id>/delete/', views.delete_cv, name='delete_cv'),
    path('preferences/', views.edit_preferences, name='edit_preferences'),
    path('search/start/', views.start_search, name='start_search'),
    path('search/clear/', views.clear_search_history, name='clear_search_history'),
    path('search/<int:run_id>/', views.search_results, name='search_results'),
    path('search/<int:run_id>/status/', views.search_status, name='search_status'),
    path('search/<int:run_id>/export/', views.export_excel, name='export_excel'),
    path('job/<int:job_id>/ats/', views.ats_report, name='ats_report'),
]
