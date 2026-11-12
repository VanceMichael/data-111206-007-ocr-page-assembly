import unittest
from pathlib import Path
from alto_source import words


class AltoTests(unittest.TestCase):
    def test_raw_words_with_coordinates(self):
        pages=words(Path('fixtures/pages/a1.xml').read_bytes())
        self.assertEqual(pages[0]['width'],1000)
        self.assertEqual(pages[0]['tokens'][0]['box'],[80,100,300,40])

    def test_confidence_not_silently_corrected(self):
        tokens=words(Path('fixtures/pages/a2.xml').read_bytes())[0]['tokens']
        token=next(row for row in tokens if row['text']=='l0')
        self.assertEqual(token['confidence'],0.46)

    def test_bad_coordinate(self):
        raw=Path('fixtures/pages/a1.xml').read_bytes().replace(b'HPOS="80"',b'HPOS="99999"',1)
        with self.assertRaises(ValueError):
            words(raw)

    def test_duplicate_word_id(self):
        raw=Path('fixtures/pages/a1.xml').read_bytes().replace(b'word-0-1',b'word-0-0')
        with self.assertRaises(ValueError):
            words(raw)

    def test_doctype_rejected(self):
        with self.assertRaises(ValueError):
            words(b'<!DOCTYPE unsafe><anything/>')
