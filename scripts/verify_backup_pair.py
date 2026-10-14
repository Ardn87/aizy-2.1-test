#!/usr/bin/env python3
import argparse
import hashlib
from pathlib import Path


def verify(database, media):
    check=Path(str(database)+'.sha256')
    if not check.exists():return False  # Compatibilité : anciennes sauvegardes contrôlées à la restauration.
    expected={}
    for row in check.read_text().splitlines():
        digest,name=row.split(maxsplit=1);expected[Path(name.lstrip('*')).name]=digest
    for file in (Path(database),Path(media)):
        h=hashlib.sha256()
        with file.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
        if expected.get(file.name)!=h.hexdigest():raise ValueError('Empreinte de sauvegarde incorrecte : '+file.name)
    return True

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('database');p.add_argument('media');a=p.parse_args()
    print('Empreintes vérifiées.' if verify(a.database,a.media) else 'Ancien backup sans empreintes : validation complète de la zone temporaire requise.')
