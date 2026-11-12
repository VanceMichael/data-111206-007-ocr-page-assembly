import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DemoTests(unittest.TestCase):
    def test_real_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'capture.sqlite3'
            process=subprocess.run([sys.executable,'app.py','demo','--output',str(output)],capture_output=True,text=True,timeout=30)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertIsInstance(json.loads(process.stdout),dict)
            self.assertTrue(output.is_file())

    def test_pipeline_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'raw.sqlite3'
            db=Path(directory)/'pipeline.sqlite3'
            process=subprocess.run([sys.executable,'app.py','pipeline','--output',str(output),'--db',str(db)],capture_output=True,text=True,timeout=30)
            self.assertEqual(process.returncode,0,process.stderr)
            result=json.loads(process.stdout)
            self.assertEqual(sorted(result['最终发布']['可发布']),['SYN-A','SYN-B'])
            self.assertIn('SYN-C',result['最终发布']['暂停'])
            log={entry['复核项']:entry['状态'] for entry in result['决定日志']}
            self.assertEqual(log['lowconf|SYN-A|控制器#1|数量'],'未生效（词框已变化或已被取代）')
            self.assertIn('a2@1',result['原始OCR各版本仍可读取'])
            self.assertIn('a2@2',result['原始OCR各版本仍可读取'])
