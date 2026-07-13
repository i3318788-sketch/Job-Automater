from django.contrib.auth.models import User
from django.db import models


class CV(models.Model):
    """A candidate profile: a named CV plus its extracted text and data.

    A single user may own several CVs ("profiles"), each shown as a tab in the UI.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cvs')
    # Profile / tab name (e.g. "John Doe"). Optional file so empty profiles exist.
    name = models.CharField(max_length=120, blank=True)
    original_file = models.FileField(upload_to='cvs/', blank=True)
    parsed_text = models.TextField(blank=True)
    parsed_data = models.JSONField(default=dict, blank=True)
    upload_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'CV'
        verbose_name_plural = 'CVs'
        # Stable order for tabs (oldest first).
        ordering = ['id']

    def __str__(self):
        return f'{self.display_name} ({self.user.username})'

    @property
    def display_name(self):
        return self.name or f'Profile {self.pk}'

    @property
    def has_file(self):
        return bool(self.original_file)


class UserPreferences(models.Model):
    """Per-user job search preferences."""

    CURRENCY_GBP = 'GBP'
    CURRENCY_USD = 'USD'
    CURRENCY_EUR = 'EUR'
    CURRENCY_CHOICES = [
        (CURRENCY_GBP, '£ (GBP)'),
        (CURRENCY_USD, '$ (USD)'),
        (CURRENCY_EUR, '€ (EUR)'),
    ]
    CURRENCY_SYMBOLS = {CURRENCY_GBP: '£', CURRENCY_USD: '$', CURRENCY_EUR: '€'}

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='preferences'
    )
    target_countries = models.JSONField(
        default=list, blank=True, help_text='List of target country names.'
    )
    salary_min = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Minimum acceptable salary. Overrides the system default when set.',
    )
    salary_max = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Maximum acceptable salary (optional; blank = no upper limit).',
    )
    currency = models.CharField(
        max_length=3, choices=CURRENCY_CHOICES, default=CURRENCY_GBP
    )

    class Meta:
        verbose_name = 'User preferences'
        verbose_name_plural = 'User preferences'

    def __str__(self):
        return f'Preferences of {self.user.username}'

    @property
    def currency_symbol(self):
        return self.CURRENCY_SYMBOLS.get(self.currency, '£')


class SearchRun(models.Model):
    """A single job search execution for a user."""

    STATUS_PENDING = 'PENDING'
    STATUS_RUNNING = 'RUNNING'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_FAILED = 'FAILED'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_RUNNING, 'Running'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED, 'Failed'),
    ]

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='search_runs'
    )
    # The candidate profile this search was run for (kept if the CV is deleted).
    cv = models.ForeignKey(
        'CV', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='search_runs',
    )
    countries = models.JSONField(default=list, blank=True)
    min_salary = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    max_salary = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    # Number of jobs fetched for this run (used for "processing X of Y").
    total_jobs = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    # Percentage of jobs processed (0-100), updated as the async task runs.
    progress = models.PositiveIntegerField(default=0)
    # Populated with the error detail when status is FAILED.
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'SearchRun #{self.pk} for {self.user.username} ({self.status})'


class Job(models.Model):
    """A single job posting discovered during a search run."""

    SPONSORED = 'SPONSORED'
    NOT_MENTIONED = 'NOT_MENTIONED'
    NOT_SPONSORED = 'NOT_SPONSORED'
    SPONSORSHIP_CHOICES = [
        (SPONSORED, 'Sponsored'),
        (NOT_MENTIONED, 'Not Mentioned'),
        (NOT_SPONSORED, 'Not Sponsored'),
    ]

    # ATS outcome. REJECTED means the CV failed a hard filter (parsing or a
    # knock-out) and would never reach a human for this job, whatever its score.
    ATS_NOT_CHECKED = 'NOT_CHECKED'
    ATS_PASSED = 'PASSED'
    ATS_BELOW_THRESHOLD = 'BELOW_THRESHOLD'
    ATS_REJECTED = 'REJECTED'
    ATS_STATUS_CHOICES = [
        (ATS_NOT_CHECKED, 'Not checked'),
        (ATS_PASSED, 'Passed'),
        (ATS_BELOW_THRESHOLD, 'Below threshold'),
        (ATS_REJECTED, 'ATS Rejected'),
    ]

    search_run = models.ForeignKey(
        SearchRun, on_delete=models.CASCADE, related_name='jobs'
    )
    title = models.CharField(max_length=255)
    company = models.CharField(max_length=255)
    location = models.CharField(max_length=255)
    # Full job description text (used for matching and later CV tailoring).
    description = models.TextField(blank=True)
    # Kept as CharField because upstream date formats are inconsistent.
    date_posted = models.CharField(max_length=100, blank=True)
    employment_type = models.CharField(max_length=50, blank=True)
    seniority_level = models.CharField(max_length=50, blank=True)
    salary = models.CharField(max_length=100, blank=True)
    sponsorship_flag = models.CharField(
        max_length=20, choices=SPONSORSHIP_CHOICES, default=NOT_MENTIONED
    )
    match_score = models.IntegerField(null=True, blank=True)
    match_reason = models.TextField(blank=True)
    # Skills mined from the job description, and those the CV is missing.
    job_skills = models.JSONField(default=list, blank=True)
    missing_skills = models.JSONField(default=list, blank=True)
    # Estimated ATS score (0-100) of the tailored CV against this job.
    ats_score = models.IntegerField(null=True, blank=True)
    ats_status = models.CharField(
        max_length=20, choices=ATS_STATUS_CHOICES, default=ATS_NOT_CHECKED,
        help_text='Outcome of the ATS check (phases 1 & 2 are hard filters).',
    )
    application_link = models.URLField(max_length=500)
    tailored_pdf = models.FileField(
        upload_to='tailored_cvs/', null=True, blank=True
    )
    tailored_text = models.TextField(blank=True)
    processed = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.title} @ {self.company}'

    @property
    def ats_rejected(self):
        return self.ats_status == self.ATS_REJECTED


class ATSReport(models.Model):
    """The full ATS report behind a job's headline ATS score.

    ``report_data`` holds the complete phase-by-phase dict produced by
    ``ATSChecker.get_detailed_report`` — kept whole so the methodology can change
    without a migration, and so the UI can surface any part of it later.
    """

    job = models.OneToOneField(
        Job, on_delete=models.CASCADE, related_name='ats_report'
    )
    overall_score = models.IntegerField()
    report_data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'ATS report'
        verbose_name_plural = 'ATS reports'

    def __str__(self):
        return f'ATS report for {self.job} ({self.overall_score})'

    @property
    def recommendations(self):
        return (self.report_data or {}).get('recommendations', [])

    @property
    def rejection_reasons(self):
        return (self.report_data or {}).get('rejection_reasons', [])
