"""Exercise the real camera wrapper with ctypes SDK structures and a fake native library."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from ctypes import pointer
from pathlib import Path
from unittest.mock import Mock, patch


def load_camera_wrapper():
    root = Path(__file__).resolve().parents[1]
    # SDK structure/constant modules are Python-only. Never import/load the native DLL.
    with patch.object(sys, "path", [str(root / "MvImport"), *sys.path]):
        import CameraParams_header as headers
        shim = types.ModuleType("MvCameraControl_class")
        for name in ("MV_CC_DEVICE_INFO", "MV_CC_DEVICE_INFO_LIST", "MV_FRAME_OUT",
                     "MV_SAVE_IMAGE_TO_FILE_PARAM_EX", "MVCC_FLOATVALUE", "MVCC_INTVALUE_EX", "MVCC_ENUMVALUE"):
            setattr(shim, name, getattr(headers, name))
        shim.MvCamera = Mock()
        spec = importlib.util.spec_from_file_location("core._connection_test_camera", root / "core" / "camera.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: module, "MvCameraControl_class": shim}):
            spec.loader.exec_module(module)
    return module


camera_module = load_camera_wrapper()


class CameraConnectionTests(unittest.TestCase):
    def setUp(self):
        self.native_class = self.enterContext(patch.object(camera_module, "MvCamera"))
        self.native = self.native_class.return_value
        self.native.MV_CC_CreateHandle.return_value = 0
        self.native.MV_CC_OpenDevice.return_value = 0
        self.native.MV_CC_SetEnumValue.return_value = 0
        self.native.MV_CC_GetIntValueEx.return_value = 0
        self.enterContext(patch.object(camera_module, "init_sdk"))
        self.count = 1
        self.info = camera_module.MV_CC_DEVICE_INFO()
        self.info.nTLayerType = camera_module.MV_USB_DEVICE
        serial = b"opened-camera"
        for i, value in enumerate(serial):
            self.info.SpecialInfo.stUsb3VInfo.chSerialNumber[i] = value
        self.native_class.MV_CC_EnumDevices.side_effect = self.enumerate
        self.camera = camera_module.HikCamera()

    def enumerate(self, _transport, result):
        result.nDeviceNum = self.count
        for i in range(self.count):
            result.pDeviceInfo[i] = pointer(self.info)
        return 0

    def test_auto_connect_rechecks_count_before_opening(self):
        for count in (0, 2):
            with self.subTest(count=count):
                self.count = count
                with self.assertRaises(camera_module.CameraError) as caught:
                    self.camera.connect(0, single_device_only=True)
                self.assertEqual(caught.exception.device_count, count)
                self.native.MV_CC_CreateHandle.assert_not_called()
                self.native.MV_CC_OpenDevice.assert_not_called()

    def test_connected_summary_comes_from_opened_device_without_reenumerating(self):
        summary = self.camera.connect(0, single_device_only=True)
        self.assertEqual(summary.serial, "opened-camera")
        self.assertIs(self.camera.device, summary)
        self.native_class.MV_CC_EnumDevices.assert_called_once()
        self.native.MV_CC_OpenDevice.assert_called_once()

    def test_manual_connection_can_select_among_multiple_devices(self):
        self.count = 2
        summary = self.camera.connect(1)
        self.assertEqual(summary.index, 1)
        self.native.MV_CC_OpenDevice.assert_called_once()

    def test_failed_open_destroys_handle_and_allows_retry(self):
        self.native.MV_CC_OpenDevice.return_value = 0x80000301
        with self.assertRaises(camera_module.CameraError):
            self.camera.connect(0, single_device_only=True)
        self.native.MV_CC_DestroyHandle.assert_called_once()
        self.assertIsNone(self.camera._cam)
        self.native.MV_CC_OpenDevice.return_value = 0
        self.camera.connect(0, single_device_only=True)
        self.assertIs(self.camera._cam, self.native)


if __name__ == "__main__":
    unittest.main()
