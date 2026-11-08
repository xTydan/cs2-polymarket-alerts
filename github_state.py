"""Pobranie najnowszego zapisu SQLite + cache rynków z prywatnych artefaktów Actions.
Artefakty repozytorium publicznego mogą być dostępne osobom z prawem odczytu;
NIGDY nie umieszczaj w nich .env ani tokenu Telegram.
"""
from __future__ import annotations

import gzip
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state'
ARTIFACT_NAME = 'radar-state'


def api_get(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'Authorization': f'Bearer {token}',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'CS2-Radar-State/1.0',
    })
    class StripAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, orig, fp, code, msg, headers, newurl):
            redirected = super().redirect_request(orig, fp, code, msg, headers, newurl)
            if (redirected is not None and
                    urlsplit(orig.full_url).hostname != urlsplit(newurl).hostname):
                redirected.remove_header('Authorization')
            return redirected

    opener = urllib.request.build_opener(StripAuthOnCrossHostRedirect)
    with opener.open(req, timeout=40) as response:
        return response.read()


def restore():
    repo = os.environ.get('GH_REPOSITORY', '')
    token = os.environ.get('GH_TOKEN', '')
    if not repo or not token:
        raise RuntimeError('Brakuje GH_REPOSITORY / GH_TOKEN z GitHub Actions')
    try:
        resp = json.loads(api_get(
            f'https://api.github.com/repos/{repo}/actions/artifacts?name={ARTIFACT_NAME}&per_page=100', token))
    except (urllib.error.URLError, ValueError) as err:
        raise RuntimeError(f'Nie udało się pobrać listy zapisów GitHub: {type(err).__name__}') from None
    artifacts = [a for a in resp.get('artifacts', []) if not a.get('expired') and a.get('name') == ARTIFACT_NAME]
    artifacts.sort(key=lambda a: a.get('created_at', ''), reverse=True)
    if not artifacts:
        print('Brak poprzedniego stanu — start od teraz (bez alarmów historycznych).')
        return
    found = False
    for a in artifacts[:4]:
        try:
            archive = api_get(a['archive_download_url'], token)
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                names = z.namelist()
                gz_name = next((n for n in names if Path(n).name == 'radar.sqlite3.gz'), None)
                if not gz_name:
                    continue
                data = gzip.decompress(z.read(gz_name))
                # Nie zapisuj dowolnych ścieżek z ZIP; wydobądź wyłącznie znane pliki.
                db_path = ROOT / 'radar.sqlite3'
                db_path.write_bytes(data)
                conn = sqlite3.connect(db_path)
                try:
                    result = conn.execute('PRAGMA integrity_check').fetchone()[0]
                finally:
                    conn.close()
                if result != 'ok':
                    db_path.unlink(missing_ok=True)
                    continue
                cache_name = next((n for n in names if Path(n).name == 'market_cache.json'), None)
                if cache_name:
                    (ROOT / 'market_cache.json').write_bytes(z.read(cache_name))
                found = True
                print(f'Odtworzono stan z {a.get("created_at")} (artefakt {a["id"]}).')
                break
        except (urllib.error.URLError, ValueError, KeyError, OSError, zipfile.BadZipFile, sqlite3.Error) as err:
            print(f'Nie udało się odtworzyć artefaktu {a.get("id")}: {type(err).__name__}')
    if not found:
        raise RuntimeError('Znaleziono zapisy, ale nie udało się odtworzyć poprawnego stanu — nie uruchamiam skanu, by uniknąć utraty historii.')


def backup():
    source = ROOT / 'radar.sqlite3'
    if not source.exists():
        print('Brak bazy — pomijam zapis artefaktu.')
        return
    STATE.mkdir(parents=True, exist_ok=True)
    destination = STATE / 'radar.sqlite3'
    # Wykonaj spójną kopię zamiast kopiować otwartą bazę razem z WAL.
    con = sqlite3.connect(source)
    try:
        out = sqlite3.connect(destination)
        try:
            con.backup(out)
        finally:
            out.close()
    finally:
        con.close()
    with destination.open('rb') as f, gzip.open(STATE / 'radar.sqlite3.gz', 'wb', compresslevel=6) as out:
        shutil.copyfileobj(f, out)
    destination.unlink()
    cache = ROOT / 'market_cache.json'
    if cache.exists():
        shutil.copy2(cache, STATE / 'market_cache.json')
    print('Stan zapakowany; token Telegram nigdy nie jest zapisywany.')


if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] not in ('restore', 'backup'):
        raise SystemExit('Użycie: python github_state.py restore | backup')
    {'restore': restore, 'backup': backup}[sys.argv[1]]()
