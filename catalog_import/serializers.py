from rest_framework import serializers

from .models import (
    CatalogImport,
    ParsedProduct,
)


class CatalogImportSerializer(serializers.ModelSerializer):

    class Meta:
        model = CatalogImport
        fields = "__all__"


class ParsedProductSerializer(serializers.ModelSerializer):

    class Meta:
        model = ParsedProduct
        fields = "__all__"
class JsonCatalogImportSerializer(serializers.Serializer):
    json_file = serializers.FileField()        