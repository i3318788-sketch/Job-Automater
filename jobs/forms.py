from django import forms

from .models import CV, UserPreferences
from .utils import validate_cv_extension

# A pre-defined list of common target countries for the preferences form.
COUNTRY_CHOICES = [
    ('United Kingdom', 'United Kingdom'),
    ('United States', 'United States'),
    ('Canada', 'Canada'),
    ('Ireland', 'Ireland'),
    ('Germany', 'Germany'),
    ('Netherlands', 'Netherlands'),
    ('France', 'France'),
    ('Spain', 'Spain'),
    ('Italy', 'Italy'),
    ('Switzerland', 'Switzerland'),
    ('Sweden', 'Sweden'),
    ('Norway', 'Norway'),
    ('Denmark', 'Denmark'),
    ('Australia', 'Australia'),
    ('New Zealand', 'New Zealand'),
    ('United Arab Emirates', 'United Arab Emirates'),
    ('Singapore', 'Singapore'),
    ('India', 'India'),
    ('Pakistan', 'Pakistan'),
    ('Remote', 'Remote'),
]


class CVUploadForm(forms.ModelForm):
    class Meta:
        model = CV
        fields = ('original_file',)
        widgets = {
            'original_file': forms.ClearableFileInput(
                attrs={'accept': '.pdf,.docx'}
            ),
        }

    def clean_original_file(self):
        uploaded = self.cleaned_data['original_file']
        # Raises ValidationError for anything that isn't PDF/DOCX.
        validate_cv_extension(uploaded.name)
        return uploaded


class UserPreferencesForm(forms.ModelForm):
    target_countries = forms.MultipleChoiceField(
        choices=COUNTRY_CHOICES,
        required=False,
        widget=forms.SelectMultiple(attrs={'size': 10}),
        help_text='Select one or more target countries.',
    )

    class Meta:
        model = UserPreferences
        fields = ('target_countries', 'min_salary')

    def clean_target_countries(self):
        # MultipleChoiceField returns a list; store it directly in the JSONField.
        return list(self.cleaned_data.get('target_countries', []))
