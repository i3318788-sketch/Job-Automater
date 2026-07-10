from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import UserProfile


class UserProfileSignalTests(TestCase):
    def test_profile_created_with_user(self):
        user = User.objects.create_user(username='alice', password='pw12345!')
        self.assertTrue(UserProfile.objects.filter(user=user).exists())
        self.assertEqual(user.profile.role, UserProfile.ROLE_USER)


class RegistrationViewTests(TestCase):
    def test_registration_creates_user_and_profile(self):
        response = self.client.post(
            reverse('register'),
            {
                'username': 'bob',
                'email': 'bob@example.com',
                'candidate_name': 'Bob Builder',
                'password1': 'Sup3rSecret!23',
                'password2': 'Sup3rSecret!23',
            },
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='bob')
        self.assertEqual(user.profile.candidate_name, 'Bob Builder')
