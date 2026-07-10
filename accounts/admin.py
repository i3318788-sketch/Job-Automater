from django.contrib import admin

from .models import UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'candidate_name', 'default_min_salary')
    list_filter = ('role',)
    search_fields = ('user__username', 'user__email', 'candidate_name')
