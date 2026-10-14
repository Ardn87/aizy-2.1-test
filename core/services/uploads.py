import hashlib
import zipfile
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ValidationError


def check_archive(archive):
    items = archive.infolist()
    if len(items) > 5000 or sum(i.file_size for i in items) > 80 * 1024 * 1024:
        raise ValidationError('Classeur trop volumineux une fois décompressé.')
    for item in items:
        if item.file_size > 20 * 1024 * 1024 or (item.file_size > 1024 * 1024 and item.file_size / max(1, item.compress_size) > 200):
            raise ValidationError('Classeur compressé trop volumineux.')
        if item.flag_bits & 1:
            raise ValidationError('Les classeurs chiffrés ne sont pas acceptés.')
    if '[Content_Types].xml' not in archive.namelist():
        raise ValidationError('Ce fichier ne correspond pas à un classeur XLSX.')


def validate_upload(uploaded, catalogue=False):
    ext = Path(uploaded.name).suffix.lower()
    allowed = {'.pdf', '.jpg', '.jpeg', '.png'} | ({'.csv', '.xlsx', '.xslx'} if catalogue else set())
    if ext not in allowed:
        raise ValidationError('Format accepté : PDF, JPG, PNG' + (', CSV ou XLSX.' if catalogue else '.'))
    if uploaded.size > settings.DOCUMENT_MAX_UPLOAD_BYTES or uploaded.size == 0:
        raise ValidationError('Fichier vide ou trop volumineux (20 Mo maximum par défaut).')
    position = uploaded.tell()
    try:
        uploaded.seek(0); head = uploaded.read(1024)
        matches = {'.pdf': head.lstrip().startswith(b'%PDF-'), '.png': head.startswith(b'\x89PNG\r\n\x1a\n'),
                   '.jpg': head.startswith(b'\xff\xd8\xff'), '.jpeg': head.startswith(b'\xff\xd8\xff')}
        if ext in matches and not matches[ext]:
            raise ValidationError('Le contenu du fichier ne correspond pas à son extension.')
        if ext in ('.xlsx', '.xslx'):
            uploaded.seek(0)
            try:
                with zipfile.ZipFile(uploaded) as archive:
                    check_archive(archive)
                    if not any(n.startswith('xl/worksheets/') for n in archive.namelist()):
                        raise ValidationError('Classeur sans feuille exploitable.')
            except (zipfile.BadZipFile, RuntimeError):
                raise ValidationError('Classeur XLSX invalide.')
        if ext == '.csv' and b'\x00' in head:
            raise ValidationError('Le CSV doit être un fichier texte UTF-8.')
    finally:
        uploaded.seek(position)
    return uploaded


def content_hash(file):
    pos = file.tell(); file.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(1024 * 1024), b''):
        digest.update(chunk)
    file.seek(pos)
    return digest.hexdigest()
