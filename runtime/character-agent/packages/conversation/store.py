import sqlite3
import threading
import time
import uuid
import json

class ConversationStore:
    def __init__(self,path):
        self.lock=threading.Lock()
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, role TEXT, content TEXT, created_at REAL)')
        if 'conversation_id' not in [r[1] for r in self.db.execute('PRAGMA table_info(messages)')]:
            self.db.execute("ALTER TABLE messages ADD COLUMN conversation_id TEXT NOT NULL DEFAULT 'legacy'")
        self.db.execute('CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id,id)')
        self.db.execute('CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,created_at REAL,summary TEXT,source TEXT,folded INTEGER)')
        self.db.execute("INSERT OR IGNORE INTO conversations VALUES('legacy',?,'','none',0)",(time.time(),))
        self.db.execute('CREATE TABLE IF NOT EXISTS conversation_meta(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS action_plans(id TEXT PRIMARY KEY,conversation_id TEXT,created_at REAL,payload TEXT)')
        self.db.execute("INSERT OR IGNORE INTO conversation_meta VALUES('active','legacy')")
        self.conversation_id=self.db.execute("SELECT value FROM conversation_meta WHERE key='active'").fetchone()[0]
        self.db.commit()
    def append(self,role,content):
        with self.lock:
            self.db.execute('INSERT INTO messages(role,content,created_at,conversation_id) VALUES(?,?,?,?)',(role,content,time.time(),self.conversation_id))
            self.db.commit()
    def recent(self,limit=100,conversation_id=None):
        with self.lock:
            rows=self.db.execute('SELECT id,role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?',
                                 (conversation_id or self.conversation_id,max(1,min(1000,int(limit))))).fetchall()
        return [dict(id=i,role=r,content=c,created_at=t) for i,r,c,t in reversed(rows)]

    def memory(self):
        with self.lock:
            summary,source,folded=self.db.execute('SELECT summary,source,folded FROM conversations WHERE id=?',(self.conversation_id,)).fetchone()
            return dict(summary=summary,source=source,folded_messages=folded)

    def save_memory(self,memory):
        with self.lock:
            self.db.execute('UPDATE conversations SET summary=?,source=?,folded=? WHERE id=?',
                            (memory['summary'][:800],memory['source'],memory['folded_messages'],self.conversation_id))
            self.db.commit()

    def new_conversation(self):
        with self.lock:
            cid=uuid.uuid4().hex
            self.db.execute("INSERT INTO conversations VALUES(?,?,'','none',0)",(cid,time.time()))
            self.db.execute("UPDATE conversation_meta SET value=? WHERE key='active'",(cid,))
            self.db.commit();self.conversation_id=cid
            return cid

    def sessions(self):
        with self.lock:
            rows=self.db.execute('SELECT c.id,c.created_at,COUNT(m.id) FROM conversations c LEFT JOIN messages m ON c.id=m.conversation_id GROUP BY c.id ORDER BY c.created_at DESC LIMIT 100').fetchall()
        return [dict(id=cid,created_at=created,message_count=count,active=cid==self.conversation_id) for cid,created,count in rows]

    def save_plan(self,plan):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO action_plans VALUES(?,?,?,?)',
                            (plan['id'],self.conversation_id,plan['created_at'],json.dumps(plan,ensure_ascii=False)))
            self.db.commit()

    def plans(self,limit=30):
        with self.lock:
            rows=self.db.execute('SELECT payload FROM action_plans WHERE conversation_id=? ORDER BY created_at DESC LIMIT ?',
                                 (self.conversation_id,min(100,max(1,limit)))).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]
