"""Exercise compiled-image validation without Docker, builds, or deployment."""
import importlib.util
import io
import pathlib
import subprocess
import tarfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_image_assets', ROOT / 'scripts/verify-image-assets.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)
CSS = (ROOT / 'tests/fixtures/task25-frontend.css').read_bytes()
BRIGHT_CSS = (ROOT / 'tests/fixtures/brighter-frontend.css').read_bytes()
BRIGHT_SOURCE = (ROOT / 'tests/fixtures/brighter-divider-source.css').read_text()

SPECTRAL_CSS = (ROOT / 'tests/fixtures/spectral-frontend.css').read_bytes()
SPECTRAL_SOURCE = (ROOT / 'tests/fixtures/spectral-divider-source.css').read_text()

LIVING_CSS = (ROOT / 'tests/fixtures/living-frontend.css').read_bytes()
LIVING_SOURCE = (ROOT / 'tests/fixtures/living-divider-source.css').read_text()

CONFIDENT_CSS = (ROOT / 'tests/fixtures/confident-frontend.css').read_bytes()
CONFIDENT_SOURCE = (ROOT / 'tests/fixtures/confident-divider-source.css').read_text()


class FrontendVerificationTests(unittest.TestCase):
    def inspect(self, css=CSS, js=b'"lms-quiz-secondary","Check","lms-quiz-primary"', source_css=None):
        html = b'<script src="/assets/lms/frontend/assets/index.js"></script><link rel="stylesheet" href="/assets/lms/frontend/assets/index.css">'
        files = {
            f'{verify.BENCH}/apps/lms/lms/www/_lms.html': html,
            f'{verify.BENCH}/apps/lms/lms/public/frontend/assets/index.js': b'entry module',
            f'{verify.BENCH}/apps/lms/lms/public/frontend/assets/index.css': css,
        }

        def member(_container, location):
            data = files[location]
            info = tarfile.TarInfo(location)
            info.size = len(data)
            return info, data

        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as output:
            info = tarfile.TarInfo('frontend/assets/Quiz.js')
            info.size = len(js)
            output.addfile(info, io.BytesIO(js))
        result = subprocess.CompletedProcess([], 0, stdout=archive.getvalue())
        report = {}
        with patch.object(verify, 'copy_member', side_effect=member), patch.object(verify.subprocess, 'run', return_value=result) as docker:
            verify.verify_frontend('unstarted-inspection-container', report, source_css)
            self.assertEqual(docker.call_args.args[0][:2], ['docker', 'cp'])
        return report

    def mutate(self, old, new):
        self.assertIn(old, CSS)
        return CSS.replace(old, new)

    def test_current_pinned_minified_frontend_passes_and_keeps_asset_evidence(self):
        report = self.inspect()
        self.assertEqual(len(report['frontend']['assets']), 2)
        self.assertIn('waveAnimation48s', report['frontend']['waveAssertions'])
        self.assertIn('variableWaveTiming', report['frontend']['waveAssertions'])
        self.assertIn('breatheAnimation19s', report['frontend']['waveAssertions'])
        self.assertIn('leftOnly12px', report['frontend']['waveAssertions'])
        self.assertTrue(report['quizJavascript'])

    def test_old_wave_duration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'waveAnimation48s'):
            self.inspect(self.mutate(b'lms-ai-wave 48s', b'lms-ai-wave 24s'))

    def test_old_hue_duration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'singleWaveHueAnimation48s'):
            self.inspect(self.mutate(b'lms-ai-hue 48s', b'lms-ai-hue 24s'))

    def test_incorrect_speed_curve_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'variableWaveTiming'):
            self.inspect(self.mutate(b'.1112 10%', b'.1113 10%'))

    def test_incorrect_breathing_duration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'breatheAnimation19s'):
            self.inspect(self.mutate(b'lms-ai-breathe 19s', b'lms-ai-breathe 18s'))

    def test_wrong_divider_width_is_not_hidden_by_unrelated_12px_width(self):
        css = self.mutate(b'width:12px', b'width:14px') + b'.unrelated{width:12px}'
        with self.assertRaisesRegex(ValueError, 'leftOnly12px'):
            self.inspect(css)

    def test_second_painted_layer_still_fails(self):
        with self.assertRaisesRegex(ValueError, 'second painted wave'):
            self.inspect(CSS + b'.lms-ai-divider:after{content:""}')

    def test_quiz_javascript_check_is_preserved(self):
        with self.assertRaisesRegex(ValueError, 'quiz JS lacks'):
            self.inspect(js=b'missing expected action signatures')

    def test_confident_living_wave_matches_pins(self):
        report = self.inspect(CONFIDENT_CSS, source_css=CONFIDENT_SOURCE)
        self.assertEqual(report['frontend']['dividerProfile'], 'confident-living-core')

    def test_confident_wave_rejects_a_narrower_core(self):
        with self.assertRaisesRegex(ValueError, 'confident2pxCore'):
            self.inspect(CONFIDENT_CSS.replace(b'rgb(0 0 0 / 1) calc(100% - 2px)', b'rgb(0 0 0 / 1) calc(100% - 1px)'), source_css=CONFIDENT_SOURCE)

    def test_confident_wave_rejects_old_width(self):
        with self.assertRaisesRegex(ValueError, 'leftOnly18px'):
            self.inspect(CONFIDENT_CSS.replace(b'width:18px', b'width:14px'), source_css=CONFIDENT_SOURCE)

    def test_living_wave_matches_pins(self):
        report = self.inspect(LIVING_CSS, source_css=LIVING_SOURCE)
        self.assertEqual(report['frontend']['dividerProfile'], 'living-thin-core')

    def test_living_wave_rejects_synchronized_cycles(self):
        with self.assertRaisesRegex(ValueError, 'blueIndependentCycle'):
            self.inspect(LIVING_CSS.replace(b'lms-ai-blue-bloom 47s', b'lms-ai-blue-bloom 37s'), source_css=LIVING_SOURCE)

    def test_static_gradient_cannot_satisfy_living_pins(self):
        with self.assertRaisesRegex(ValueError, 'tealIndependentCycle'):
            self.inspect(SPECTRAL_CSS, source_css=LIVING_SOURCE)

    def test_spectral_wave_matches_its_pinned_source(self):
        report = self.inspect(SPECTRAL_CSS, source_css=SPECTRAL_SOURCE)
        self.assertEqual(report['frontend']['dividerProfile'], 'spectral-thin-core')
        self.assertIn('threeCoolHues', report['frontend']['waveAssertions'])

    def test_spectral_wave_requires_all_three_colours(self):
        with self.assertRaisesRegex(ValueError, 'threeCoolHues'):
            self.inspect(SPECTRAL_CSS.replace(b'#72a9d6b8', b'#ffffffb8'), source_css=SPECTRAL_SOURCE)

    def test_previous_wave_cannot_satisfy_spectral_pins(self):
        with self.assertRaisesRegex(ValueError, 'visible1pxCore'):
            self.inspect(BRIGHT_CSS, source_css=SPECTRAL_SOURCE)

    def test_brighter_wave_is_verified_against_its_own_pinned_source(self):
        report = self.inspect(BRIGHT_CSS, source_css=BRIGHT_SOURCE)
        self.assertEqual(report['frontend']['dividerProfile'], 'brighter-thin-core')
        self.assertIn('visible1pxCore', report['frontend']['waveAssertions'])
        self.assertIn('leftOnly14px', report['frontend']['waveAssertions'])

    def test_old_compiled_wave_cannot_satisfy_new_pinned_source(self):
        with self.assertRaisesRegex(ValueError, 'visible1pxCore'):
            self.inspect(CSS, source_css=BRIGHT_SOURCE)

    def test_new_wave_cannot_satisfy_old_pins(self):
        with self.assertRaisesRegex(ValueError, 'visible2pxCore'):
            self.inspect(BRIGHT_CSS)

    def test_unknown_source_profile_requires_explicit_expectations(self):
        with self.assertRaisesRegex(ValueError, 'Unrecognized pinned divider'):
            self.inspect(BRIGHT_CSS, source_css=BRIGHT_SOURCE.replace('width: 14px', 'width: 99px'))

    def test_new_wave_rejects_a_wider_core(self):
        with self.assertRaisesRegex(ValueError, 'visible1pxCore'):
            self.inspect(BRIGHT_CSS.replace(b'100% - 1px', b'100% - 2px'), source_css=BRIGHT_SOURCE)


if __name__ == '__main__':
    unittest.main()
