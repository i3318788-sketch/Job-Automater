from django.contrib import admin

from .models import CV, Job, SearchRun, UserPreferences


@admin.register(CV)
class CVAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'user', 'original_file', 'upload_date')
    list_filter = ('upload_date',)
    search_fields = ('user__username', 'name')
    readonly_fields = ('upload_date',)


@admin.register(UserPreferences)
class UserPreferencesAdmin(admin.ModelAdmin):
    list_display = ('user', 'target_countries', 'salary_min', 'salary_max', 'currency')
    search_fields = ('user__username',)


class JobInline(admin.TabularInline):
    model = Job
    extra = 0
    fields = ('title', 'company', 'location', 'match_score', 'processed')
    show_change_link = True


@admin.register(SearchRun)
class SearchRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'cv', 'status', 'progress', 'min_salary', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('user__username',)
    readonly_fields = ('created_at',)
    inlines = [JobInline]


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'company', 'location', 'sponsorship_flag',
        'match_score', 'processed',
    )
    list_filter = ('sponsorship_flag', 'processed', 'employment_type')
    search_fields = ('title', 'company', 'location')
