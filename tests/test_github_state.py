import gzip
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import github_state


class StatePersistenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.backup = github_state.ROOT, github_state.STATE
        github_state.ROOT = self.root
        github_state.STATE = self.root / 'state'
        self.addCleanup(lambda: setattr(github_state, 'ROOT', self.backup[0]))
        self.addCleanup(lambda: setattr(github_state, 'STATE', self.backup[1]))

    def test_backup_and_restore_with_previous_artifact(self):
        db = sqlite3.connect(self.root / 'radar.sqlite3')
        db.execute('CREATE TABLE t (id INT PRIMARY KEY, value TEXT)')
        db.execute('INSERT INTO t VALUES(1, "known")')
        db.commit()
        db.close()
        (self.root / 'market_cache.json').write_text('{"ok":true}')
        github_state.backup()
        self.assertTrue((self.root / 'state' / 'radar.sqlite3.gz').exists())
        zipbytes = io.BytesIO()
        with zipfile.ZipFile(zipbytes,'w') as z:
            for f in (self.root / 'state').iterdir():
                z.write(f, arcname=f.name)
        (self.root / 'radar.sqlite3').unlink()
        (self.root / 'market_cache.json').unlink()
        listing = json.dumps({'artifacts':[{'id':77, 'name':'radar-state', 'expired':False,
                              'created_at':'2026-11-08T20:00:00Z',
                              'archive_download_url':'https://api.github.com/download/77'}]}).encode()
        def fake_fetch(url, token):
            return zipbytes.getvalue() if 'download' in url else listing
        with patch.dict('os.environ',{'GH_REPOSITORY':'demo/repo','GH_TOKEN':'secret'}),patch.object(github_state, 'api_get', fake_fetch):
            github_state.restore()
        with sqlite3.connect(self.root / 'radar.sqlite3') as restored:
            self.assertEqual(restored.execute('SELECT value FROM t WHERE id=1').fetchone()[0], 'known')
        self.assertEqual((self.root / 'market_cache.json').read_text(), '{"ok":true}')

    def test_restore_without_prior_artifact(self):
        with patch.dict('os.environ',{'GH_REPOSITORY':'demo/repo','GH_TOKEN':'secret'}),patch.object(github_state,'api_get',lambda u,t:b'{"artifacts":[]}'):
            github_state.restore()
        self.assertFalse((self.root / 'radar.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
