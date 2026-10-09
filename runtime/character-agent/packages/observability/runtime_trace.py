"""Local per-run trace and provider-reported usage accounting; no request credentials."""
import gzip
import json
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path


class TraceStore:
    def __init__(self, directory, kind='live', run_id=None):
        self.kind = kind
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', self.run_id):raise ValueError('Invalid run id')
        self.run_dir = self.directory/self.run_id
        self.run_dir.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory/'trace.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, started REAL)')
        if 'kind' not in [row[1] for row in self.db.execute('PRAGMA table_info(runs)')]:
            self.db.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'live'")
        self.db.execute('CREATE TABLE IF NOT EXISTS usage(run_id TEXT, call_id TEXT, role TEXT, attempt INTEGER, '
                        'input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, raw TEXT, '
                        'PRIMARY KEY(run_id,call_id,attempt))')
        self.db.execute('INSERT INTO runs(id,started,kind) VALUES(?,?,?)', (self.run_id, time.time(), self.kind))
        self.db.commit()
        self.record('run_started', {'run_id': self.run_id, 'kind': self.kind})
        self._save_summary()

    def attach_console(self):
        """Preserve startup/inference stdout and stderr even after the next restart."""
        class Tee:
            def __init__(self, original, path):
                self.original = original
                self.file = path.open('a', encoding='utf-8')
                self.lock = threading.Lock()
            def write(self, text):
                with self.lock:
                    self.file.write(text); self.file.flush()
                    return self.original.write(text)
            def flush(self):
                with self.lock:self.file.flush(); self.original.flush()
            def __getattr__(self, key):return getattr(self.original, key)
        sys.stdout = Tee(sys.stdout, self.run_dir/'stdout.log')
        sys.stderr = Tee(sys.stderr, self.run_dir/'stderr.log')

    def record(self, kind, data=None):
        event = {'time': time.time(), 'run_id': self.run_id, 'event': kind, 'data': data or {}}
        with self.lock:
            with (self.run_dir/'events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False)+'\n')

    def motion(self, packet):
        # Raw generated outputs are retained separately to keep the readable trace small.
        if packet.get('cached_replay'):return  # Identical idle data is not logged forever.
        with self.lock:
            with gzip.open(self.run_dir/'motion.jsonl.gz', 'at', encoding='utf-8') as stream:
                stream.write(json.dumps({'time': time.time(), 'packet': packet}, separators=(',', ':'), allow_nan=False)+'\n')

    @staticmethod
    def _token(value):
        return value if type(value) is int and value >= 0 else None

    def llm(self, role, call_id, **data):
        self.record('llm', {'role': role, 'call_id': call_id, **data})
        if data.get('stage') == 'llm_request':
            with self.lock:
                self.db.execute('INSERT OR IGNORE INTO usage VALUES(?,?,?,?,?,?,?,?)',
                                (self.run_id, call_id, role, data.get('llm_attempt', 1), None, None, None, 'null'))
                self.db.commit()
                self._save_summary()
        if 'llm_usage' not in data:
            return
        raw = data.get('llm_usage')
        usage = raw if isinstance(raw, dict) else {}
        incoming = self._token(usage.get('prompt_tokens', usage.get('input_tokens')))
        outgoing = self._token(usage.get('completion_tokens', usage.get('output_tokens')))
        total = self._token(usage.get('total_tokens'))
        # Keep total unknown if the provider didn't report it; no tokenizer estimates.
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO usage VALUES(?,?,?,?,?,?,?,?)',
                            (self.run_id, call_id, role, data.get('llm_attempt', 1), incoming, outgoing,
                             total, json.dumps(raw, ensure_ascii=False)))
            self.db.commit()
            self._save_summary()

    def _save_summary(self):
        temporary = self.run_dir/'usage-summary.tmp'
        temporary.write_text(json.dumps(self.summary(), ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.run_dir/'usage-summary.json')

    def summary(self):
        def scope(run_id=None):
            clause, args = (' AND usage.run_id=?', (self.kind, run_id)) if run_id else ('', (self.kind,))
            rows = self.db.execute('SELECT role,COUNT(*),SUM(input_tokens),SUM(output_tokens),SUM(total_tokens),'
                                   'SUM(CASE WHEN total_tokens IS NULL THEN 1 ELSE 0 END) FROM usage JOIN runs ON runs.id=usage.run_id WHERE runs.kind=?'+clause+' GROUP BY role', args).fetchall()
            result = {role: {'attempts': count, 'input_tokens_reported': incoming,
                           'output_tokens_reported': outgoing, 'total_tokens_reported': total,
                           'attempts_without_total_usage': unknown}
                    for role, count, incoming, outgoing, total, unknown in rows}
            raw_rows = self.db.execute('SELECT role,raw FROM usage JOIN runs ON runs.id=usage.run_id WHERE runs.kind=?'+clause, args).fetchall()
            for role, raw in raw_rows:
                usage = json.loads(raw) or {}
                details = usage.get('prompt_tokens_details') or {}
                cached = usage.get('prompt_cache_hit_tokens', details.get('cached_tokens'))
                reasoning = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
                for field, value in (('cached_input_tokens_reported', cached), ('reasoning_tokens_reported', reasoning)):
                    if self._token(value) is not None:result[role][field] = result[role].get(field, 0)+value
            return result
        with self.lock:
            started = self.db.execute('SELECT started FROM runs WHERE id=?', (self.run_id,)).fetchone()[0]
            return {'run_id': self.run_id, 'elapsed_s': round(time.time()-started, 1),
                    'run_kind': self.kind,
                    'current_run': scope(self.run_id), 'all_runs': scope(),
                    'usage_source': 'provider_reported_only', 'directory': str(self.run_dir)}

    def close(self):
        self.record('run_closed')
        with self.lock:
            self._save_summary()
            self.db.close()
