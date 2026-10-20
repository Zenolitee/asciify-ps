import copy
import json
import tempfile
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import asciify_ps as app


class ToneTests(unittest.TestCase):
    def setUp(self):
        self.settings = app.RenderSettings(100, 8, ' .#', 'color', False, False, 'plain')
        self.image = np.array([[[20, 80, 180], [100, 100, 100]]], dtype=np.uint8)

    def test_defaults_preserve_source_without_sharing_memory(self):
        result = app.adjust_image(self.image, self.settings)
        np.testing.assert_array_equal(result, self.image)
        self.assertFalse(np.shares_memory(result, self.image))

    def test_adjustments_have_expected_effects(self):
        original = self.image.copy()
        brighter = app.adjust_image(self.image, replace(self.settings, brightness=20))
        self.assertTrue(np.all(brighter > self.image))
        gamma = app.adjust_image(self.image, replace(self.settings, gamma=2))
        self.assertTrue(np.all(gamma > self.image))
        gray = app.adjust_image(self.image, replace(self.settings, saturation=0))
        np.testing.assert_array_equal(gray[..., 0], gray[..., 1])
        np.testing.assert_array_equal(gray[..., 1], gray[..., 2])
        flat = app.adjust_image(self.image, replace(self.settings, contrast=0))
        self.assertTrue(np.all(flat == 128))
        np.testing.assert_array_equal(self.image, original)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.gui = app.AsciiGUI(self.root, enable_dnd=False)
        self.temp = tempfile.TemporaryDirectory()
        self.preset = Path(self.temp.name) / 'portrait.json'

    def tearDown(self):
        for callback in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.temp.cleanup()

    def save_settings(self):
        with patch.object(app.filedialog, 'asksaveasfilename', return_value=str(self.preset)):
            self.gui._save_preset()
        return json.loads(self.preset.read_text(encoding='utf-8'))

    def test_preset_round_trip_and_reset(self):
        self.gui.adjustment_vars['gamma'].set(1.7)
        self.gui.adjustment_vars['brightness'].set(25)
        self.gui.adjustment_vars['edge_sensitivity'].set(160)
        self.gui.custom_chars.set(' .oO@')
        self.gui.custom_enabled.set(True)
        self.gui.edges_var.set(True)
        expected = self.gui._snapshot()
        saved = self.save_settings()
        self.gui._reset()
        self.assertEqual(self.gui._snapshot().gamma, 1)
        self.assertFalse(self.gui.custom_enabled.get())
        self.gui._apply_preset(saved)
        self.assertEqual(self.gui._snapshot(), expected)

    def test_invalid_preset_does_not_partially_change_settings(self):
        saved = self.save_settings()
        before = self.gui._snapshot()
        for key, value in [('gamma', float('nan')), ('brightness', 101),
                           ('width', 50.5), ('edges', 'yes'), ('charset', []),
                           ('format', 'unknown')]:
            bad = copy.deepcopy(saved)
            bad['settings']['width'] = 50
            bad['settings'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.gui._apply_preset(bad)
            self.assertEqual(self.gui._snapshot(), before)

    def test_custom_ramp_validation(self):
        self.gui.custom_enabled.set(True)
        for chars in ('', 'x', 'a\tb', 'a\nb', 'a界', 'a\u0301', 'x' * 256):
            self.gui.custom_chars.set(chars)
            with self.subTest(chars=chars), self.assertRaises(ValueError):
                self.gui._snapshot()
        self.gui.custom_chars.set(' .:-#')
        self.assertEqual(self.gui._snapshot().charset, ' .:-#')

    def test_worker_renders_adjustments_and_custom_ramp(self):
        image_path = Path(self.temp.name) / 'source.png'
        Image.new('RGB', (200, 100), (30, 30, 30)).save(image_path)
        self.gui.processor = app.ImgProcessor(str(image_path))
        settings = replace(self.gui._snapshot(), charset=' .#', fmt='plain')
        self.gui._worker(1, settings)
        first = self.gui._results.get_nowait()
        self.gui._worker(2, replace(settings, brightness=80))
        second = self.gui._results.get_nowait()
        self.assertNotEqual(first[1], 'err', first)
        self.assertNotEqual(second[1], 'err', second)
        # Compare rendered text, rather than job IDs / timing.
        self.assertNotEqual(first[2][1], second[2][1])
        self.gui._worker(3, replace(settings, edges=True, edge_sensitivity=200))
        result = self.gui._results.get_nowait()
        self.assertNotEqual(result[1], 'err', result)
        self.gui._worker(4, replace(settings, color_mode='bw', charset='.' * 199 + '#',
                                    brightness=100))
        result = self.gui._results.get_nowait()
        self.assertNotEqual(result[1], 'err', result)
        self.assertIn('#', result[2][1])


if __name__ == '__main__':
    unittest.main()
