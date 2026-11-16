import tempfile
import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from core.capture_storage import StorageError, resolve_directory, prepare_row_storage
from capture_fixtures import tag_info
from core.progress_sync import SyncService
from test_acquisition_metadata import sample_metadata


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_share_group_directory_but_never_prefix_or_resume(self):
        first = prepare_row_storage(self.root, info=tag_info(number='RIL8'), mode='wheat', extension='png')
        (first.folder / (first.prefix + '_Rep1_front.png')).touch()
        second = prepare_row_storage(self.root, info=tag_info(entry=2, number='RIL8'), mode='wheat', extension='png')
        self.assertEqual(first.folder, second.folder)
        self.assertEqual(first.prefix, 'RIL8_行1')
        self.assertEqual(second.prefix, 'RIL8_行2')
        self.assertFalse((second.folder / (second.prefix + '_Rep1_front.png')).exists())
        self.assertEqual(first, prepare_row_storage(self.root, info=tag_info(number='RIL8'), mode='wheat', extension='png'))

    def test_renumber_keeps_binding_and_original_format_with_notice(self):
        first = prepare_row_storage(self.root, info=tag_info(), mode='wheat', extension='png')
        again = prepare_row_storage(self.root, info=tag_info(number='NEW'), mode='wheat', extension='jpg')
        self.assertEqual(again.prefix, first.prefix)
        self.assertEqual(again.extension, 'png')
        self.assertIn('NEW', again.notice)

    def test_group_switch_identity_collisions_and_no_population(self):
        first = resolve_directory(self.root, info=tag_info(group='A/B'), mode='wheat')
        second = resolve_directory(self.root, info=tag_info(entry=2, population_id=2, group='A:B'), mode='wheat')
        self.assertNotEqual(first, second)
        standalone = prepare_row_storage(self.root, info=tag_info(entry=3, population_id=None, number='CS', name='中国春'), mode='wheat', extension='png')
        self.assertEqual(standalone.folder.name, '无群体_麦穗_张三')
        self.assertEqual(standalone.prefix, 'CS_行3')

    def test_same_row_number_in_different_plan_rejected_and_old_directory_preserved(self):
        resolve_directory(self.root, info=tag_info(), mode='wheat')
        with self.assertRaises(StorageError):
            resolve_directory(self.root, info=tag_info(plan=2), mode='wheat')
        old = self.root/'old'; old.mkdir(); (old/'A1.png').touch()
        with self.assertRaises(StorageError):
            resolve_directory(old, info=tag_info(), mode='wheat')
        self.assertTrue((old/'A1.png').exists())

    def test_unbound_existing_files_ck_and_prefix_collision_are_blocked(self):
        folder = resolve_directory(self.root, info=tag_info(), mode='wheat')
        (folder/'A1_行1_Rep1_front.png').touch()
        with self.assertRaises(StorageError):
            prepare_row_storage(self.root, info=tag_info(), mode='wheat', extension='png')
        with self.assertRaises(StorageError):
            resolve_directory(self.root, info=tag_info(ck=True), mode='wheat')
        first = prepare_row_storage(self.root, info=tag_info(entry=2, number='RIL8'), mode='wheat', extension='png')
        with self.assertRaises(StorageError):
            prepare_row_storage(self.root, info=tag_info(entry=3, row='2', number='RIL8'), mode='wheat', extension='png')
        self.assertEqual(first.prefix, 'RIL8_行2')


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []
        self.status = 200
        self.response_status = 'accepted'
        self.service = SyncService(self.root, transport=self.transport, start=False)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def transport(self, endpoint, key=None, records=None):
        self.calls.append((endpoint, key, records))
        if endpoint == 'me':
            return 200, {'id': 1 if key in ('secret-a', 'rotated-a') else 2, 'display_name':'用户'}
        if endpoint == 'contexts':
            return 200, {'contexts':[]}
        return self.status, {'results':[{'capture_id':r['capture_id'], 'status':self.response_status, 'detail':'待核对'} for r in records]}

    def save(self, key='', rep=1):
        meta = replace(sample_metadata(), capture_id=str(uuid4()), rep=rep, side='front')
        reporting = {**self.service.snapshot(key), 'year':2026, 'location':'鸡泽'}
        job = SimpleNamespace(metadata=meta, reporting=reporting)
        self.service.record(job)
        return job

    def test_restart_offline_and_no_secrets(self):
        job = self.save('secret-a')
        self.status = 503
        self.assertFalse(self.service.sync_once())
        self.assertEqual(self.service.outbox.summary()[0], 1)
        raw = self.service.outbox.path.read_bytes()
        self.assertNotIn(b'secret-a', raw)
        other = SyncService(self.root, transport=self.transport, start=False)
        try:
            self.status = 200
            other.sync_once()
            self.assertEqual(other.outbox.summary()[0], 1)
            other.register_key('secret-a')
            other.sync_once()
            self.assertEqual(other.outbox.summary()[0], 0)
            self.assertEqual(self.calls[-1][2][0]['capture_id'], job.metadata.capture_id)
        finally:
            other.close()

    def test_v2_identity_survives_restart_and_account_switch(self):
        info = tag_info(population_id=None, number='CS', name='中国春')
        self.service.register_key('secret-a', {'id':1})
        reporting = {**self.service.snapshot('secret-a'), **info.identity_payload(),
                     'year':info.year, 'location':info.location}
        meta = replace(sample_metadata(), capture_id=str(uuid4()), accession_no='CS',
                       field_tag_code=info.code, planting=info.snapshot(),
                       filename_prefix=info.filename_prefix,
                       filename=info.filename_prefix + '_Rep1_front.png')
        self.service.record(SimpleNamespace(metadata=meta, reporting=reporting))
        reporting['planting_entry_id'] = 999
        stored = json.loads(self.service.outbox.pending()[0][1])
        self.assertEqual(stored['planting_entry_id'], info.planting_entry_id)
        self.assertIsNone(stored['membership_id'])
        self.assertIsNone(stored['planting_population_id'])
        self.assertEqual(stored['global_material_id'], info.global_material['id'])
        other = SyncService(self.root, transport=self.transport, start=False)
        try:
            other.register_key('secret-b', {'id':2})
            other.sync_once()
            self.assertEqual(other.outbox.summary()[0], 1)
            other.register_key('rotated-a', {'id':1})
            other.sync_once()
            self.assertEqual(other.outbox.summary()[0], 0)
            self.assertEqual(self.calls[-1][2][0], stored)
            self.assertEqual(stored['filename'], 'CS_行1_Rep1_front.png')
        finally:
            other.close()

    def test_switching_accounts_cannot_reattribute_and_anonymous_stays_anonymous(self):
        self.service.register_key('secret-a', {'id':1})
        self.save('secret-a')
        self.service.keys.clear()
        self.service.register_key('secret-b', {'id':2})
        self.service.sync_once()
        self.assertEqual(self.service.outbox.summary()[0], 1)
        self.save()
        self.service.sync_once()
        batches = [c for c in self.calls if c[0] == 'batch']
        self.assertIsNone(batches[-1][1])
        self.service.register_key('rotated-a', {'id':1})
        self.service.sync_once()
        self.assertEqual(self.service.outbox.summary()[0], 0)

    def test_duplicate_ack_and_rejected_record(self):
        job = self.save()
        self.service.record(job)
        self.assertEqual(self.service.outbox.summary()[0], 1)
        self.response_status = 'duplicate'
        self.service.sync_once()
        self.assertEqual(self.service.outbox.summary()[0], 0)
        self.save()
        self.response_status = 'rejected'
        self.service.sync_once()
        self.assertEqual(self.service.outbox.summary(), (1, '待核对'))

    def test_queue_failure_is_visible_and_retryable(self):
        with patch.object(self.service.outbox, 'enqueue', side_effect=OSError('disk full')):
            job = self.save()
        self.assertIn(job.metadata.capture_id, self.service.failed_local)
        self.assertIn('队列写入失败', self.service.error)
        self.service.sync_once()
        self.assertFalse(self.service.failed_local)
        self.assertEqual(self.service.outbox.summary()[0], 0)

    def test_ck_is_not_enqueued(self):
        meta = sample_metadata()
        self.service.record(SimpleNamespace(metadata=meta, reporting={'is_ck':True}))
        self.assertEqual(self.service.outbox.summary()[0], 0)

    def test_save_after_identity_lookup_can_replay_with_rotated_key(self):
        reporting = {**self.service.snapshot('secret-a'), 'year':2026, 'location':'鸡泽'}
        self.assertIsNone(reporting['owner'])
        self.service.sync_once()
        job = SimpleNamespace(metadata=replace(sample_metadata(), capture_id=str(uuid4())), reporting=reporting)
        self.service.record(job)
        self.status = 503
        self.service.sync_once()
        other = SyncService(self.root, transport=self.transport, start=False)
        try:
            other.register_key('rotated-a')
            self.status = 200
            other.sync_once()
            self.assertEqual(other.outbox.summary()[0], 0)
        finally:
            other.close()
