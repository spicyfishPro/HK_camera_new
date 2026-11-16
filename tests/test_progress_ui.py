import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.progress_sync import SyncService
from capture_fixtures import tag_payload, tag_info
from test_qr_ui import QT_AVAILABLE, QApplication, CaptureWidget, _FakeReply, _FakeScorer


@unittest.skipUnless(QT_AVAILABLE, '没有 Qt')
class ProgressUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = SyncService(self.root/'queue', start=False)
        with patch('ui.capture_widget.BRISQUEScorer', return_value=_FakeScorer()):
            self.widget = CaptureWidget('wheat', reporter=self.service)
        self.widget._save_root = self.root/'photos'
        self.widget._save_root.mkdir()
        self.widget._save_dir = self.widget._save_root
        self.widget.le_bearer_key.setText('secret-a')

    def tearDown(self):
        self.widget.shutdown()
        self.widget.close()
        self.service.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def qr(self, group_id, name, material, *, entry=1, source='qr', ck=False):
        self.widget._set_prefix_source(source)
        self.widget._clear_tag_identity()
        self.widget._qr_request_id += 1
        rid = self.widget._qr_request_id
        self.widget._qr_request_code = f'P{entry}'
        self.widget._set_qr_request_pending(True)
        payload = tag_payload(entry=entry, population_id=group_id, group=name, number=material, ck=ck)
        reply = _FakeReply(200, json.dumps(payload, ensure_ascii=False).encode())
        self.widget._on_field_tag_reply_finished(rid, reply)

    def reset(self):
        self.widget.spike_panel.workflow.reset()
        self.widget._on_workflow_state_changed(False)

    def test_group_switch_and_same_material_other_row_starts_fresh(self):
        self.qr(1, '群体甲', 'RIL8')
        first = self.widget._save_dir
        self.assertEqual(first.name, '群体甲_麦穗_张三')
        self.assertTrue(self.widget.spike_panel.workflow.active)
        self.assertFalse(self.widget.le_bearer_key.isEnabled())
        self.widget._refresh_capture_capacity()
        self.assertFalse(self.widget.btn_choose_dir.isEnabled())
        (first/'RIL8_行1_Rep1_front.png').touch()
        self.reset()
        self.qr(1, '群体甲', 'RIL8', entry=2)
        self.assertEqual(first, self.widget._save_dir)
        self.assertEqual(self.widget.spike_panel.workflow.current_step().side, 'front')
        self.assertEqual(self.widget.spike_panel.workflow.prefix, 'RIL8_行2')
        self.reset()
        self.qr(2, '群体乙', 'RIL8', entry=3)
        self.assertNotEqual(first, self.widget._save_dir)
        self.reset()
        self.qr(1, '群体甲', 'RIL8')
        self.assertEqual(self.widget.spike_panel.workflow.current_step().side, 'side')
        self.assertEqual(self.widget._active_field_tag_code, 'P1')

    def test_manual_tag_uses_same_identity_and_ck_never_starts(self):
        self.qr(None, '无群体', 'CS', source='manual')
        self.assertTrue(self.widget.spike_panel.workflow.active)
        self.assertEqual(self.widget._save_dir.name, '无群体_麦穗_张三')
        self.assertEqual(self.widget._report_context['planting_entry_id'], 1)
        self.assertIsNone(self.widget._report_context['membership_id'])
        self.assertEqual(self.widget.spike_panel.workflow.prefix, 'CS_行1')
        self.reset()
        self.qr(1, '群体甲', 'CK', entry=2, ck=True)
        self.assertFalse(self.widget.spike_panel.workflow.active)
        self.assertIsNone(self.widget._active_field_tag_code)
        self.assertIsNone(self.widget._report_context)

    def test_direct_capture_and_unresolved_start_cannot_bypass_tag_lookup(self):
        self.widget.camera.is_connected = lambda: True
        with patch.object(self.widget, '_err') as error:
            self.widget.le_prefix.setText('RIL8')
            self.widget.snap()
            error.assert_called_once()
        with patch('ui.spike_panel.QMessageBox.warning'):
            self.assertFalse(self.widget.spike_panel.start_with_prefix('RIL8'))
        self.assertIsNone(self.widget._report_context)

    def test_retry_registers_key_without_new_capture(self):
        self.widget._retry_progress_sync()
        self.assertIn('secret-a', self.service.keys.values())

    def test_other_tab_key_change_cancels_pending_qr_and_defers_during_capture(self):
        self.widget._set_prefix_source('qr')
        self.widget._qr_request_id = 12
        self.widget._set_qr_request_pending(True)
        self.widget.set_bearer_key('secret-b')
        self.assertFalse(self.widget._qr_request_pending)
        self.assertNotEqual(self.widget._qr_request_id, 12)
        self.widget.set_bearer_key('secret-a')
        self.qr(1, '群体甲', 'A1')
        original_context = dict(self.widget._report_context)
        self.widget.set_bearer_key('secret-b')
        self.assertEqual(self.widget.le_bearer_key.text(), 'secret-a')
        self.assertEqual(self.widget._report_context, original_context)
        self.reset()
        self.assertEqual(self.widget.le_bearer_key.text(), 'secret-b')

    def test_disk_failure_blocks_start_and_close_without_losing_unsaved_queue(self):
        from ui.main_window import MainWindow
        self.widget._qr_info = tag_info()
        self.widget._active_field_tag_code = 'P1'
        with patch.object(self.service.outbox, 'set', side_effect=sqlite3.OperationalError('disk full')):
            ok, message = self.widget._spike_precheck()
        self.assertFalse(ok)
        self.assertIn('统计队列', message)
        with patch('ui.capture_widget.BRISQUEScorer', return_value=_FakeScorer()):
            window = MainWindow(reporter=self.service)
        try:
            window.show()
            self.service.failed_local['test'] = ('retained',)
            with patch.object(self.service, 'retry', side_effect=sqlite3.OperationalError('disk full')):
                self.assertFalse(window.close())
            self.assertTrue(window.isVisible())
            self.assertIn('test', self.service.failed_local)
        finally:
            self.service.failed_local.clear()
            window.close()
