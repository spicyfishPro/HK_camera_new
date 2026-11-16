"""显式指定隔离平台时执行真实 HTTP 契约测试；不默认访问生产。"""
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from core.capture_pipeline import CapturePipeline
from core.progress_sync import SyncService
from test_capture_pipeline import job, wait_for


@unittest.skipUnless(os.environ.get('CAPTURE_TEST_API_ROOT'), '需要显式指定隔离测试平台')
class HttpContractTests(unittest.TestCase):
    def test_real_saved_images_offline_queue_and_http_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = root/'中文群体_麦穗_测试设备'
            photos.mkdir()
            service = SyncService(root/'queue', start=False)
            service.device_model = 'HTTP 联调设备（测试）'
            pipeline = CapturePipeline(lambda event: None, primary_saved=service.record)
            try:
                for rep in range(1,6):
                    for side in ('front','side'):
                        number = rep*2 + (side == 'side')
                        capture = job(photos, number, score=100)
                        filename = f'DEMO-003_Rep{rep}_{side}.png'
                        meta = replace(capture.metadata, capture_id=str(uuid4()), accession_no='DEMO-003',
                                       prefix_source='manual', field_tag_code=None,
                                       mode='wheat', rep=rep, side=side, filename=filename)
                        context = {**service.snapshot(''), 'year':2026, 'location':'演示鸡泽'}
                        capture = replace(capture, metadata=meta, path=photos/filename, reporting=context)
                        self.assertTrue(pipeline.submit(capture))
                        self.assertTrue(wait_for(lambda: pipeline.pending_count == 0))
                self.assertEqual(len(list(photos.glob('*.png'))), 10)
                self.assertEqual(service.outbox.summary()[0], 10)
                with patch.object(service, '_transport', return_value=(503, {})):
                    self.assertFalse(service.sync_once())
                self.assertEqual(service.outbox.summary()[0], 10)
                service.close()
                restarted = SyncService(root/'queue', start=False)
                try:
                    with patch('core.progress_sync.API_ROOT', os.environ['CAPTURE_TEST_API_ROOT']):
                        self.assertTrue(restarted.sync_once())
                    self.assertEqual(restarted.outbox.summary()[0], 0, restarted.outbox.summary())
                finally:
                    restarted.close()
            finally:
                pipeline.shutdown()
                service.close()
