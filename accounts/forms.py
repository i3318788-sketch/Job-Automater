from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User


class RegistrationForm(UserCreationForm):
    """User registration form that also captures the candidate's full name."""

    email = forms.EmailField(required=True)
    candidate_name = forms.CharField(
        max_length=255,
        required=True,
        label='Full name',
        help_text='Your full name, used when naming generated PDF files.',
    )

    class Meta:
        model = User
        fields = ('username', 'email', 'candidate_name', 'password1', 'password2')

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        if commit:
            user.save()
            # The post_save signal has already created the profile; update it
            # with the candidate name and default USER role.
            profile = user.profile
            profile.candidate_name = self.cleaned_data['candidate_name']
            profile.role = profile.ROLE_USER
            profile.save()
        return user
