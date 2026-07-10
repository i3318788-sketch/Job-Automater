from django.contrib.auth.models import User
from django.db import models


class UserProfile(models.Model):
    """Extra per-user data attached one-to-one to Django's built-in User."""

    ROLE_ADMIN = 'ADMIN'
    ROLE_USER = 'USER'
    ROLE_CHOICES = [
        (ROLE_ADMIN, 'Admin'),
        (ROLE_USER, 'User'),
    ]

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='profile'
    )
    role = models.CharField(
        max_length=10, choices=ROLE_CHOICES, default=ROLE_USER
    )
    candidate_name = models.CharField(
        max_length=255,
        blank=True,
        help_text='Full name, used when naming generated PDF files.',
    )
    default_min_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Optional per-user default minimum salary.',
    )

    def __str__(self):
        return f'{self.user.username} ({self.get_role_display()})'
