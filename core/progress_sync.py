"""采集元数据持久队列与后台同步。凭证只存于进程内存。"""
from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

API_ROOT = 'https://wheat.cau.edu.cn/seed-inventory/api/v1/capture-progress/'


def key_ref(key):
    return 'key:' + hashlib.sha256(key.encode('utf-8')).hexdigest()


def computer_model():
    try:
        if platform.system() == 'Windows':
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                 'Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model | ConvertTo-Json -Compress'],
                capture_output=True, text=True, timeout=5, creationflags=0x08000000, check=True)
            data = json.loads(result.stdout.lstrip('\ufeff'))
            return (str(data.get('Manufacturer', '')) + ' ' + str(data.get('Model', ''))).strip()[:160]
        if platform.system() == 'Darwin':
            return 'Apple ' + subprocess.check_output(['sysctl', '-n', 'hw.model'], text=True, timeout=3, stderr=subprocess.DEVNULL).strip()
        base = Path('/sys/devices/virtual/dmi/id')
        return ' '.join((base / name).read_text().strip() for name in ('sys_vendor', 'product_name'))[:160]
    except (OSError, ValueError, subprocess.SubprocessError):
        return platform.system() + ' 未知电脑型号'


class Outbox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, payload TEXT NOT NULL, actor TEXT NOT NULL, owner INTEGER, state TEXT NOT NULL DEFAULT "pending", error TEXT NOT NULL DEFAULT "")')
            db.execute('CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, name, default=None):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE name=?', (name,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, name, value):
        with self.lock, self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (name, json.dumps(value, ensure_ascii=False)))

    def enqueue(self, payload, actor, owner=None):
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.lock, self.connect() as db:
            existing = db.execute('SELECT payload, actor FROM records WHERE id=?', (payload['capture_id'],)).fetchone()
            if existing and existing != (body, actor):
                raise ValueError('本地拍摄编号冲突')
            db.execute('INSERT OR IGNORE INTO records(id,payload,actor,owner) VALUES (?,?,?,?)',
                       (payload['capture_id'], body, actor, owner))

    def pending(self):
        with self.lock, self.connect() as db:
            return db.execute('SELECT id,payload,actor,owner FROM records WHERE state="pending" ORDER BY rowid').fetchall()

    def update(self, capture_id, state, error=''):
        with self.lock, self.connect() as db:
            db.execute('UPDATE records SET state=?,error=? WHERE id=?', (state, error, capture_id))

    def summary(self):
        with self.lock, self.connect() as db:
            count = db.execute('SELECT count(*) FROM records WHERE state!="sent"').fetchone()[0]
            rejected = db.execute('SELECT error FROM records WHERE state="rejected" LIMIT 1').fetchone()
            return count, rejected[0] if rejected else ''

    def retry(self):
        with self.lock, self.connect() as db:
            db.execute('UPDATE records SET state="pending",error="" WHERE state="rejected"')

    def bind_owner(self, actor, owner):
        with self.lock, self.connect() as db:
            db.execute('UPDATE records SET owner=? WHERE actor=? AND owner IS NULL', (owner, actor))


class SyncService:
    def __init__(self, folder: Path, *, transport=None, start=True):
        self.outbox = Outbox(folder / 'progress.sqlite3')
        self.device_id = self.outbox.get('device_id') or str(uuid4())
        self.outbox.set('device_id', self.device_id)
        self.device_model = self.outbox.get('device_model') or computer_model()
        self.outbox.set('device_model', self.device_model)
        self.keys = {}
        self.users = {}
        self.invalid = set()
        self.lock = threading.RLock()
        self.error = ''
        self.failed_local = {}
        self.last_success = self.outbox.get('last_success', '')
        self.contexts = self.outbox.get('contexts', [])
        self._need_contexts = True
        self._force_retry = False
        self._transport = transport or self._http
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._run, daemon=True, name='capture-sync')
            self.thread.start()

    def register_key(self, key, user=None):
        if not key:
            return 'device:' + self.device_id
        ref = key_ref(key)
        if not key.isascii() or key != key.strip() or any(ord(c) < 33 or ord(c) == 127 for c in key):
            self.error = '密钥格式无效，请重新输入'
            return ref
        with self.lock:
            if ref not in self.keys or user:
                self._force_retry = True
            self.keys[ref] = key
            self.invalid.discard(ref)
            if user:
                self.users[ref] = dict(user)
        self._wake.set()
        return ref

    def snapshot(self, key):
        ref = self.register_key(key)
        with self.lock:
            user = self.users.get(ref, {})
            return {'actor': ref, 'owner': user.get('id'), 'device_id': self.device_id,
                    'device_model': self.device_model}

    def record(self, job):
        context = job.reporting
        if not context or context.get('is_ck'):
            return
        meta = job.metadata
        payload = dict(capture_id=meta.capture_id, accession_no=meta.accession_no,
                       field_tag_code=meta.field_tag_code or '', year=context['year'],
                       location=context['location'], mode=meta.mode, rep=meta.rep,
                       side=meta.side or '', captured_at=meta.captured_at,
                       filename=meta.filename, device_id=context['device_id'],
                       device_model=context['device_model'])
        if context.get('schema_version') == 2:
            payload.update({key: context[key] for key in ('schema_version', 'planting_entry_id',
                'planting_plan_id', 'global_material_id', 'membership_id', 'planting_population_id', 'section_id')})
        item = (payload, context['actor'], context.get('owner'))
        try:
            self.outbox.enqueue(*item)
            self._wake.set()
        except Exception:
            # 不能让统计错误把已经成功保存的照片变成保存失败。
            with self.lock:
                self.failed_local[meta.capture_id] = item
                self.error = '本地统计队列写入失败；照片已保存，请保留程序并检查磁盘后重试'

    def retry(self):
        self.outbox.retry()
        with self.lock:
            self.invalid.clear()
            self._force_retry = True
        self._wake.set()

    def _http(self, endpoint, key=None, records=None):
        headers = {'Accept': 'application/json'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        body = None
        if records is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps({'records': records}, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(API_ROOT + endpoint + '/', data=body, headers=headers)
        # 不允许重定向携带 Authorization 到其他地址。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=5) as reply:
                return reply.status, json.loads(reply.read(256 * 1024))
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    def sync_once(self):
        with self.lock:
            failures = dict(self.failed_local)
            keys = dict(self.keys)
        for capture_id, item in failures.items():
            self.outbox.enqueue(*item)
            with self.lock:
                self.failed_local.pop(capture_id, None)
        # 获取本人身份后可允许同账号轮换密钥继续补传。
        for ref, key in keys.items():
            if ref not in self.users and ref not in self.invalid:
                status, user = self._transport('me', key)
                if status == 200 and type(user.get('id')) is int:
                    with self.lock:
                        self.users[ref] = user
                    self.outbox.bind_owner(ref, user['id'])
                elif status in (401, 403):
                    with self.lock:
                        self.invalid.add(ref)
        with self.lock:
            users, invalid = dict(self.users), set(self.invalid)
        # 保存任务可能晚于身份查询入队，补齐这些记录的已验证账号归属。
        for ref, user in users.items():
            self.outbox.bind_owner(ref, user['id'])
        batch, chosen_ref = [], None
        waiting_identity = False
        for capture_id, payload, actor, owner in self.outbox.pending():
            ref = None
            if actor.startswith('key:'):
                if actor in keys and actor not in invalid:
                    ref = actor
                else:
                    ref = next((k for k, u in users.items() if owner is not None and u.get('id') == owner and k in keys and k not in invalid), None)
                if ref is None:
                    waiting_identity = True
                    continue
            selected = ref or 'anonymous'
            if chosen_ref is None:
                chosen_ref = selected
            if chosen_ref == selected:
                batch.append(json.loads(payload))
            if len(batch) == 50:
                break
        if batch:
            status, data = self._transport('batch', keys.get(chosen_ref), batch)
            if status in (401, 403):
                with self.lock:
                    self.invalid.add(chosen_ref)
                    self.users.pop(chosen_ref, None)
                self.error = '密钥无效或权限不足，待验证后补传'
                return False
            if status != 200 or not isinstance(data.get('results'), list):
                self.error = f'服务器暂未接收（HTTP {status}），将自动重试'
                return False
            ids = {r['capture_id'] for r in batch}
            confirmed = set()
            accepted = False
            for result in data['results']:
                capture_id = result.get('capture_id')
                if capture_id not in ids:
                    continue
                if result.get('status') in ('accepted', 'duplicate'):
                    self.outbox.update(capture_id, 'sent')
                    confirmed.add(capture_id)
                    accepted = True
                elif result.get('status') == 'rejected':
                    self.outbox.update(capture_id, 'rejected', str(result.get('detail', '记录被拒绝'))[:300])
                    confirmed.add(capture_id)
            if accepted:
                self.last_success = datetime.now().astimezone().strftime('%m-%d %H:%M:%S')
                self.outbox.set('last_success', self.last_success)
            if confirmed != ids:
                self.error = '部分上报未获确认，将自动重试'
                return False
        self.error = '有记录等待原拍摄账号密钥，请输入后补传' if waiting_identity else ''
        if self._need_contexts:
            status, data = self._transport('contexts')
            if status == 200 and isinstance(data.get('contexts'), list):
                self.contexts = data['contexts']
                self.outbox.set('contexts', self.contexts)
                self._need_contexts = False
        return True

    def _run(self):
        delay = 1
        retry_at = 0
        while not self._stop.is_set():
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                break
            with self.lock:
                force = self._force_retry
                self._force_retry = False
            if not force and time.monotonic() < retry_at:
                delay = max(.1, retry_at - time.monotonic())
                continue
            try:
                ok = self.sync_once()
            except Exception:
                self.error = '同步暂不可用，请检查网络与本地磁盘；队列将保留并重试'
                ok = False
            delay = 2 if ok else min(300, max(5, delay * 2))
            retry_at = 0 if ok else time.monotonic() + delay

    def close(self):
        self._stop.set()
        self._wake.set()
        if self.thread:
            self.thread.join(timeout=.1)
