from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class AdminTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["username"] = user.username
        return token


class AdminLoginView(TokenObtainPairView):
    serializer_class = AdminTokenObtainPairSerializer


class AdminTokenRefreshView(TokenRefreshView):
    pass