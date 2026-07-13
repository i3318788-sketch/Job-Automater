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
    # The options are filled in client-side from the country -> cities blob, so
    # this stays a free-text field on the server and is validated in clean().
    target_city = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'list': 'city-options', 'autocomplete': 'off',
                                      'placeholder': 'Any city'}),
        help_text='Optional. Leave blank to search the whole country.',
    )

    class Meta:
        model = UserPreferences
        fields = (
            'target_countries', 'target_city', 'salary_min', 'salary_max',
            'currency',
        )

    def clean_target_countries(self):
        # MultipleChoiceField returns a list; store it directly in the JSONField.
        return list(self.cleaned_data.get('target_countries', []))

    def clean_target_city(self):
        return (self.cleaned_data.get('target_city') or '').strip()

    def clean(self):
        cleaned = super().clean()
        lo, hi = cleaned.get('salary_min'), cleaned.get('salary_max')
        if lo is not None and hi is not None and hi < lo:
            self.add_error('salary_max', 'Maximum salary must be greater than the minimum.')

        # A city only means something inside a selected country. Rejecting a
        # mismatch here stops a search silently returning nothing because the
        # actor was asked for "Berlin, United Kingdom".
        from .services.locations import cities_for_country, is_valid_city

        city = cleaned.get('target_city')
        countries = cleaned.get('target_countries') or []
        if city and countries and not is_valid_city(city, countries):
            known = [c for country in countries for c in cities_for_country(country)]
            if known:
                self.add_error(
                    'target_city',
                    f'"{city}" is not a listed city for the selected '
                    f'{"countries" if len(countries) > 1 else "country"}. '
                    f'Choose one from the list, or clear the field to search the '
                    f'whole country.',
                )
        return cleaned


class ProfileForm(forms.ModelForm):
    """Create a new (initially empty) CV profile / tab."""

    class Meta:
        model = CV
        fields = ('name',)
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'e.g. John Doe'}),
        }

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise forms.ValidationError('Please enter a profile name.')
        return name
