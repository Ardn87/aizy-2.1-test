#!/usr/bin/env python3
"""Valide puis extrait uniquement media/, sans liens ni chemins sortants."""
import argparse
import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath

def extract(archive_path, destination):
    destination=Path(destination)
    with tarfile.open(archive_path,'r:gz') as archive:
        members=archive.getmembers()
        if len(members)>100000 or sum(m.size for m in members)>10*1024**3:
            raise ValueError('Archive media trop volumineuse.')
        for member in members:
            path=PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0]!='media' or not (member.isfile() or member.isdir()):
                raise ValueError('Archive media : chemin ou type de fichier interdit.')
        destination.mkdir(parents=True,exist_ok=False)
        archive.extractall(destination,members=members,filter='data')
    return len(members)

def fingerprint(root):
    root=Path(root)
    result={}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            h=hashlib.sha256()
            with path.open('rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
            result[path.relative_to(root).as_posix()]=h.hexdigest()
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('archive');parser.add_argument('destination')
    args=parser.parse_args();print(f'{extract(args.archive,args.destination)} entrées media validées.')
