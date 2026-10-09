"""Text GPU preferences and fallback paths without allocating a real GPU model."""
import sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT.parent/'ardy'),str(ROOT/'services'/'ardy-worker')]
from ardy.model.kimodo_text_encoder import KimodoTextEncoder
import text_encoder_config as config

class Tests(unittest.TestCase):
    def make(self,folder):
        folder=Path(folder)
        for name in ('model.gguf','cpu.exe','gpu.exe'):(folder/name).touch()
        return KimodoTextEncoder(folder/'model.gguf',folder/'cpu.exe'),folder/'gpu.exe'

    def test_settings_validation_and_missing_gpu(self):
        for bad in ({'mode':'cuda'},{'layer_chunk':0},{'layer_chunk':9},{'layer_chunk':True},{'other':1}):
            with self.assertRaises(ValueError):config.validate(bad)
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(config,'PATH',Path(folder)/'settings.json'),patch.object(config,'GPU_EXE',Path(folder)/'missing.exe'):
                with self.assertRaises(ValueError):config.save({'mode':'vulkan-stream'})
                self.assertEqual(config.save({'mode':'cpu'})['mode'],'cpu')

    def test_missing_gpu_preserves_requested_mode_and_uses_cpu(self):
        with tempfile.TemporaryDirectory() as folder:
            raw,gpu=self.make(folder);raw.select_backend('vulkan-stream',gpu.with_name('missing.exe'),2)
            self.assertEqual(raw.backend,'cpu');self.assertEqual(raw.requested_mode,'vulkan-stream')
            self.assertIn('not installed',raw.fallback_reason)

    def test_gpu_execution_failure_retries_cpu(self):
        with tempfile.TemporaryDirectory() as folder:
            raw,gpu=self.make(folder);raw.select_backend('vulkan-stream',gpu,2)
            def start():
                if raw.backend=='vulkan':raise RuntimeError('GPU allocation failed')
                raw.active_backend='cpu'
            with patch.object(raw,'_start',side_effect=start),patch.object(raw,'_encode_one',return_value=np.ones(4096,dtype=np.float32)):
                data,length=raw('hello')
            self.assertEqual(tuple(data.shape),(1,4096));self.assertEqual(length,1)
            self.assertEqual(raw.active_backend,'cpu');self.assertIn('allocation',raw.fallback_reason)

    def test_cancellation_never_falls_back_or_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            raw,gpu=self.make(folder);raw.select_backend('vulkan-stream',gpu,2)
            with patch.object(raw,'_start'),patch.object(raw,'_encode_one',side_effect=RuntimeError('text encoding cancelled')):
                with self.assertRaises(RuntimeError):raw('hello')
            self.assertEqual(raw.backend,'vulkan');self.assertIsNone(raw.fallback_reason)

    def test_missing_gpu_dll_falls_back_to_cpu(self):
        with tempfile.TemporaryDirectory() as folder:
            raw,gpu=self.make(folder);raw.select_backend('vulkan-stream',gpu,4)
            def start():
                if raw.backend=='vulkan':raise OSError('Missing Vulkan DLL')
                raw.active_backend='cpu'
            with patch.object(raw,'_start',side_effect=start),patch.object(raw,'_encode_one',return_value=np.ones(4096,dtype=np.float32)):
                raw('hello')
            self.assertEqual(raw.active_backend,'cpu');self.assertIn('DLL',raw.fallback_reason)

    def test_resident_insufficient_memory_streams_and_loaded_resident_stays(self):
        with tempfile.TemporaryDirectory() as folder:
            raw,gpu=self.make(folder);raw.select_backend('vulkan-resident',gpu,2)
            raw.model_path=SimpleNamespace(stat=lambda:SimpleNamespace(st_size=4*1024**3))
            with patch('torch.cuda.is_available',return_value=True),patch('torch.cuda.mem_get_info',return_value=(2*1024**3,6*1024**3)):
                raw._check_resident_memory()
            self.assertEqual(raw.backend,'vulkan');self.assertEqual(raw.layer_chunk,2)
            raw.layer_chunk=32;raw._process=SimpleNamespace(poll=lambda:None,stdin=None,wait=lambda timeout:None)
            with patch('torch.cuda.mem_get_info',side_effect=AssertionError('Must not reevaluate after weights are loaded')):raw._check_resident_memory()
            self.assertEqual(raw.layer_chunk,32);raw.close()

if __name__=='__main__':unittest.main()
