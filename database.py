import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from instagram import AccountState

@dataclass
class Monitor:
    username: str; mode: str; channel_id: int; user_id: int; guild_id: int|None; started_at: datetime; last_state: AccountState
    followers: int = 0; following: int = 0; posts: int = 0
@dataclass
class AlertConfig:
    guild_id: int; style: str='embed'; cooldown_seconds: int=0; role_ids: tuple[int,...]=(); everyone: bool=False; dm_alerts: bool=False; webhook_url: str|None=None; summary_frequency: str='off'; summary_channel_id: int|None=None; last_summary_at: datetime|None=None
class Database:
    def __init__(self,path): self.path=path; self._init()
    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    def _init(self):
        with self._connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS monitors (username TEXT NOT NULL,mode TEXT NOT NULL,channel_id INTEGER NOT NULL,user_id INTEGER NOT NULL,started_at TEXT NOT NULL,last_state TEXT NOT NULL,last_checked TEXT NOT NULL,followers INTEGER DEFAULT 0,following INTEGER DEFAULT 0,posts INTEGER DEFAULT 0,profile_pic_url TEXT,guild_id INTEGER,PRIMARY KEY(username,guild_id))')
            columns = {r[1] for r in c.execute('PRAGMA table_info(monitors)')}
            # Older releases used username as the only key, which caused one server\'s monitor to replace another\'s.
            monitor_info = list(c.execute('PRAGMA table_info(monitors)'))
            primary_key_columns = [r[1] for r in monitor_info if r[5] > 0]
            if primary_key_columns == ['username'] and 'guild_id' in columns:
                c.execute('ALTER TABLE monitors RENAME TO monitors_legacy')
                c.execute('CREATE TABLE monitors (username TEXT NOT NULL,mode TEXT NOT NULL,channel_id INTEGER NOT NULL,user_id INTEGER NOT NULL,started_at TEXT NOT NULL,last_state TEXT NOT NULL,last_checked TEXT NOT NULL,followers INTEGER DEFAULT 0,following INTEGER DEFAULT 0,posts INTEGER DEFAULT 0,profile_pic_url TEXT,guild_id INTEGER,PRIMARY KEY(username,guild_id))')
                c.execute('INSERT OR IGNORE INTO monitors SELECT username,mode,channel_id,user_id,started_at,last_state,last_checked,followers,following,posts,profile_pic_url,guild_id FROM monitors_legacy')
                c.execute('DROP TABLE monitors_legacy')
            c.execute('CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL,event TEXT NOT NULL,detected_at TEXT NOT NULL,elapsed_seconds INTEGER NOT NULL,followers INTEGER DEFAULT 0,following INTEGER DEFAULT 0,posts INTEGER DEFAULT 0,guild_id INTEGER)')
            c.execute("CREATE TABLE IF NOT EXISTS alert_configs (guild_id INTEGER PRIMARY KEY,style TEXT NOT NULL DEFAULT 'embed',cooldown_seconds INTEGER NOT NULL DEFAULT 0,role_ids TEXT NOT NULL DEFAULT '[]',everyone INTEGER NOT NULL DEFAULT 0,dm_alerts INTEGER NOT NULL DEFAULT 0,webhook_url TEXT,summary_frequency TEXT NOT NULL DEFAULT 'off',summary_channel_id INTEGER,last_summary_at TEXT)")
            c.execute('CREATE TABLE IF NOT EXISTS alert_destinations (guild_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,PRIMARY KEY(guild_id,channel_id))')
            if 'guild_id' not in {r[1] for r in c.execute('PRAGMA table_info(history)')}: c.execute('ALTER TABLE history ADD COLUMN guild_id INTEGER')
    def add_monitor(self,u,m,ch,uid,result,guild_id=None):
        now=datetime.now(timezone.utc).isoformat(); p=result.profile
        with self._connect() as c: c.execute('INSERT INTO monitors VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(username,guild_id) DO UPDATE SET mode=excluded.mode,channel_id=excluded.channel_id,user_id=excluded.user_id,started_at=excluded.started_at,last_state=excluded.last_state,last_checked=excluded.last_checked,followers=excluded.followers,following=excluded.following,posts=excluded.posts,profile_pic_url=excluded.profile_pic_url',(u,m,ch,uid,now,result.state.value,now,p.followers if p else 0,p.following if p else 0,p.posts if p else 0,p.profile_pic_url if p else None,guild_id))
    def stop_monitor(self,u,guild_id=None):
        with self._connect() as c:
            if guild_id is None: return c.execute('DELETE FROM monitors WHERE username=?',(u,)).rowcount>0
            return c.execute('DELETE FROM monitors WHERE username=? AND guild_id=?',(u,guild_id)).rowcount>0
    def get_monitors(self,guild_id=None):
        with self._connect() as c:
            rows = c.execute('SELECT * FROM monitors '+('WHERE guild_id=? ' if guild_id is not None else '')+'ORDER BY started_at', (guild_id,) if guild_id is not None else ()).fetchall()
        return [Monitor(r['username'],r['mode'],r['channel_id'],r['user_id'],r['guild_id'],datetime.fromisoformat(r['started_at']),AccountState(r['last_state']),r['followers'] or 0,r['following'] or 0,r['posts'] or 0) for r in rows]
    def update_last_check(self,u,result,guild_id=None):
        now=datetime.now(timezone.utc).isoformat(); p=result.profile
        with self._connect() as c:
            c.execute('UPDATE monitors SET last_state=?,last_checked=?,followers=?,following=?,posts=?,profile_pic_url=? WHERE username=?'+(' AND guild_id=?' if guild_id is not None else ''),(result.state.value,now,p.followers if p else 0,p.following if p else 0,p.posts if p else 0,p.profile_pic_url if p else None,u,guild_id) if guild_id is not None else (result.state.value,now,p.followers if p else 0,p.following if p else 0,p.posts if p else 0,p.profile_pic_url if p else None,u))
    def add_history(self,u,e,started,p,guild_id=None):
        with self._connect() as c: c.execute('INSERT INTO history(username,event,detected_at,elapsed_seconds,followers,following,posts,guild_id) VALUES (?,?,?,?,?,?,?,?)',(u,e,datetime.now(timezone.utc).isoformat(),int((datetime.now(timezone.utc)-started).total_seconds()),p.followers if p else 0,p.following if p else 0,p.posts if p else 0,guild_id))
    def get_history(self,limit=10,guild_id=None):
        with self._connect() as c: return c.execute('SELECT * FROM history '+('WHERE guild_id=? ' if guild_id else '')+'ORDER BY id DESC LIMIT ?',((guild_id,limit) if guild_id else (limit,))).fetchall()
    def get_config(self,gid):
        with self._connect() as c: r=c.execute('SELECT * FROM alert_configs WHERE guild_id=?',(gid,)).fetchone()
        if not r:return AlertConfig(gid)
        return AlertConfig(gid,r['style'],r['cooldown_seconds'],tuple(json.loads(r['role_ids'])),bool(r['everyone']),bool(r['dm_alerts']),r['webhook_url'],r['summary_frequency'],r['summary_channel_id'],datetime.fromisoformat(r['last_summary_at']) if r['last_summary_at'] else None)
    def save_config(self,x):
        with self._connect() as c:c.execute('INSERT INTO alert_configs VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET style=excluded.style,cooldown_seconds=excluded.cooldown_seconds,role_ids=excluded.role_ids,everyone=excluded.everyone,dm_alerts=excluded.dm_alerts,webhook_url=excluded.webhook_url,summary_frequency=excluded.summary_frequency,summary_channel_id=excluded.summary_channel_id,last_summary_at=excluded.last_summary_at',(x.guild_id,x.style,x.cooldown_seconds,json.dumps(list(x.role_ids)),int(x.everyone),int(x.dm_alerts),x.webhook_url,x.summary_frequency,x.summary_channel_id,x.last_summary_at.isoformat() if x.last_summary_at else None))
    def add_destination(self,g,cid):
        with self._connect() as c:c.execute('INSERT OR IGNORE INTO alert_destinations VALUES (?,?)',(g,cid))
    def remove_destination(self,g,cid):
        with self._connect() as c:return c.execute('DELETE FROM alert_destinations WHERE guild_id=? AND channel_id=?',(g,cid)).rowcount>0
    def get_destinations(self,g):
        with self._connect() as c:return [r[0] for r in c.execute('SELECT channel_id FROM alert_destinations WHERE guild_id=?',(g,))]
    def recent_event(self,g,u,seconds):
        with self._connect() as c:r=c.execute('SELECT detected_at FROM history WHERE guild_id=? AND username=? ORDER BY id DESC LIMIT 1',(g,u)).fetchone()
        return bool(r and (datetime.now(timezone.utc)-datetime.fromisoformat(r[0])).total_seconds()<seconds)
    def summary_rows(self,g,since):
        with self._connect() as c:return c.execute('SELECT * FROM history WHERE guild_id=? AND detected_at>=? ORDER BY id DESC',(g,since.isoformat())).fetchall()
    def get_summary_configs(self):
        with self._connect() as c:
            rows = c.execute("SELECT guild_id FROM alert_configs WHERE summary_frequency IN ('daily','weekly') AND summary_channel_id IS NOT NULL").fetchall()
        return [self.get_config(r['guild_id']) for r in rows]
    def mark_summary_sent(self, guild_id, sent_at):
        with self._connect() as c:
            c.execute('UPDATE alert_configs SET last_summary_at=? WHERE guild_id=?',(sent_at.isoformat(),guild_id))
