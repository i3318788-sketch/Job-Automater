from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserProfile


@receiver(post_save, sender=User)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    """Auto-create a UserProfile whenever a new User is created."""
    if created:
        UserProfile.objects.create(user=instance)
    else:
        # Ensure a profile always exists (e.g. for users created before the
        # signal was in place) and keep it saved.
        UserProfile.objects.get_or_create(user=instance)
