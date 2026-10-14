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
DUAL_CSS = (ROOT / 'tests/fixtures/dual-frontend.css').read_bytes()
DUAL_SOURCE = (ROOT / 'tests/fixtures/dual-divider-source.css').read_text()


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

    def test_dual_wave_matches_its_pinned_source(self):
        report = self.inspect(DUAL_CSS, source_css=DUAL_SOURCE)
        self.assertEqual(report['frontend']['dividerProfile'], 'dual-living-streams')

    def test_dual_wave_rejects_synchronized_colour_cycles(self):
        with self.assertRaisesRegex(ValueError, 'blueIndependentCycle'):
            self.inspect(DUAL_CSS.replace(b'lms-ai-blue-bloom 47s', b'lms-ai-blue-bloom 37s'), source_css=DUAL_SOURCE)

    def test_dual_wave_rejects_a_narrower_core(self):
        with self.assertRaisesRegex(ValueError, 'confident2pxCore'):
            self.inspect(DUAL_CSS.replace(b'rgb(0 0 0 / 1) calc(100% - 2px)', b'rgb(0 0 0 / 1) calc(100% - 1px)'), source_css=DUAL_SOURCE)

    def test_dual_wave_rejects_simultaneous_blackout(self):
        with self.assertRaisesRegex(ValueError, 'complementaryPresencePhase'):
            self.inspect(DUAL_CSS.replace(b'-8s,-20.5s', b'-8s,0s'), source_css=DUAL_SOURCE)

    def test_old_image_cannot_satisfy_new_source(self):
        with self.assertRaises(ValueError):
            self.inspect(CSS, source_css=DUAL_SOURCE)

    def test_new_image_cannot_satisfy_old_source(self):
        with self.assertRaises(ValueError):
            self.inspect(DUAL_CSS)

    def test_unknown_source_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unrecognized pinned divider'):
            self.inspect(DUAL_CSS, source_css=DUAL_SOURCE.replace('width: 22px', 'width: 99px'))


if __name__ == '__main__':
    unittest.main()
