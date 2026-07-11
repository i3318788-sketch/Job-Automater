"""Template context processors: expose CV profile tabs on every page."""
from .models import CV
from .utils import resolve_active_cv


def cv_profiles(request):
    """Inject the user's CV profiles and the active one into all templates."""
    if not request.user.is_authenticated:
        return {}
    profiles = list(CV.objects.filter(user=request.user).order_by('id'))
    active = resolve_active_cv(request, profiles=profiles)
    return {
        'cv_profiles': profiles,
        'active_cv': active,
    }
