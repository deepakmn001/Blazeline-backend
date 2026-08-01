from django.core.files.storage import FileSystemStorage
from django.conf import settings


class CatalogImportStorage(FileSystemStorage):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("location", settings.MEDIA_ROOT / "catalogs")
        kwargs.setdefault("base_url", settings.MEDIA_URL + "catalogs/")
        super().__init__(*args, **kwargs)