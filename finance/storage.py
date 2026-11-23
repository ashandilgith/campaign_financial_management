"""Database-backed file storage for hosts without a persistent disk.

Free container hosts (SnapDeploy, Koyeb Hobby, Render free) wipe the filesystem on every
redeploy, which would lose uploaded quotes, bills and receipts. With FILE_STORAGE=database
the bytes live in Postgres next to the records that reference them (e.g. a free Neon database),
so they survive redeploys and are included in database backups.

Fine at this app's scale (a few hundred documents a month); move to S3-compatible storage
if volumes grow into many GB.
"""
import mimetypes

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class DatabaseStorage(Storage):
    def _model(self):
        from .models import StoredFile

        return StoredFile

    def _open(self, name, mode="rb"):
        obj = self._model().objects.filter(name=name).only("data", "name").first()
        if obj is None:
            raise FileNotFoundError(name)
        f = ContentFile(bytes(obj.data), name=name)
        f.mode = mode
        return f

    def _save(self, name, content):
        if hasattr(content, "seek"):
            content.seek(0)
        data = content.read()
        self._model().objects.create(
            name=name, data=data, size=len(data),
            content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
        )
        return name

    def exists(self, name):
        return self._model().objects.filter(name=name).exists()

    def delete(self, name):
        self._model().objects.filter(name=name).delete()

    def size(self, name):
        obj = self._model().objects.filter(name=name).only("size").first()
        if obj is None:
            raise FileNotFoundError(name)
        return obj.size

    def url(self, name):
        # Files are only served through the permission-checked attachment_download view.
        raise NotImplementedError("Database-stored files have no public URL.")

    def listdir(self, path):
        prefix = path.rstrip("/") + "/" if path else ""
        names = self._model().objects.filter(name__startswith=prefix).values_list("name", flat=True)
        dirs, files = set(), []
        for n in names:
            rest = n[len(prefix):]
            if "/" in rest:
                dirs.add(rest.split("/", 1)[0])
            else:
                files.append(rest)
        return sorted(dirs), sorted(files)
