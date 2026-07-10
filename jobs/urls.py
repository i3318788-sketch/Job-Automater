from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('cv/upload/', views.upload_cv, name='upload_cv'),
    path('preferences/', views.edit_preferences, name='edit_preferences'),
    path('search/start/', views.start_search, name='start_search'),
    path('search/<int:run_id>/', views.search_results, name='search_results'),
    path('search/<int:run_id>/status/', views.search_status, name='search_status'),
    path('search/<int:run_id>/export/', views.export_excel, name='export_excel'),
]
