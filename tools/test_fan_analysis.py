"""Synthetic telemetry checks; no hardware access or fan writes."""
import json
import math
import unittest
from fan_analysis import analyze_step


def samples(duration=180, temp=None):
    rows = []
    for t in range(0, duration+1, 2):
        value = 70 if temp is None else temp(t)
        rows.append((t, dict(board_c=value, cpu_c=value+5, gpu_c=value-5,
                      gpu_w=100, cpu_w=25, cpu_w_age_s=1, cpu_rpm=4000,
                      gpu_rpm=4500, mid_rpm=5000, stalls_per_s=0, counter_ok=1)))
    return rows


class AnalysisTest(unittest.TestCase):
    def test_hot_step_is_not_prolonged_for_settling(self):
        for sensor, limit in (("cpu_c", 88), ("gpu_c", 86)):
            with self.subTest(sensor=sensor):
                rows = samples(temp=lambda t: 70+t/60)
                rows[-1][1][sensor] = limit
                result = analyze_step(rows)
                self.assertFalse(result['can_extend'])
                self.assertIn('extra settling is skipped', result['reason'])

    def test_delayed_sustained_cooling(self):
        result = analyze_step(samples(temp=lambda t: 75-max(0,t-70)/30))
        self.assertEqual(result['status'], 'cooling')
        self.assertEqual(result['rpm_settled_s'], 30)
        self.assertTrue(result['load_stable'])
        self.assertTrue(result['can_extend'])
        onset = result['sensors']['board_c']['cooling_onset_s']
        self.assertIsNotNone(onset)
        # Window onset has uncertainty: it is not an exact physical delay.
        self.assertGreaterEqual(onset, 60)
        self.assertLessEqual(onset, 90)
        self.assertAlmostEqual(result['sensors']['board_c']['cooling_onset_c'], 75, delta=.5)
        json.dumps(result, allow_nan=False)

    def test_quantized_jitter_is_not_cooling(self):
        result = analyze_step(samples(temp=lambda t: 70+(1 if t%8 == 0 else 0)))
        self.assertEqual(result['status'], 'settled')
        self.assertIsNone(result['sensors']['board_c']['cooling_onset_s'])
        self.assertIsNone(result['sensors']['board_c']['cooling_onset_c'])
        self.assertFalse(result['can_extend'])

    def test_isolated_drop_is_not_sustained_onset(self):
        result = analyze_step(samples(temp=lambda t: 70 if t<90 else 69))
        self.assertIsNone(result['sensors']['board_c']['cooling_onset_s'])

    def test_load_collapse_invalidates_attribution(self):
        rows = samples(temp=lambda t: 75-t/30)
        for t, row in rows:
            row['gpu_w'] = 150 if t<90 else 20
        result = analyze_step(rows)
        self.assertEqual(result['status'], 'load_changed')
        self.assertFalse(result['load_stable'])
        self.assertFalse(result['can_extend'])
        self.assertIsNone(result['sensors']['board_c']['cooling_onset_s'])

    def test_late_load_collapse_and_short_sustained_transition(self):
        for beginning, ending in ((170, 180), (80, 88)):
            with self.subTest(beginning=beginning):
                rows = samples()
                for t, row in rows:
                    if beginning <= t <= ending:
                        row['gpu_w'] = 20
                result = analyze_step(rows)
                self.assertFalse(result['load_stable'])
                self.assertEqual(result['status'], 'load_changed')

    def test_isolated_power_spike_does_not_invalidate_load(self):
        rows = samples()
        rows[-2][1]['gpu_w'] = 170
        self.assertTrue(analyze_step(rows)['load_stable'])

    def test_six_second_samples_fail_nominal_density(self):
        result = analyze_step(samples()[::3])
        self.assertIsNone(result['load_stable'])
        self.assertIsNone(result['audio_clean'])
        self.assertNotEqual(result['status'], 'settled')

    def test_rpm_confirmation_follows_actual_speed_stabilization(self):
        rows = samples()
        for t, row in rows:
            row['mid_rpm'] = 2000+100*t if t < 30 else 5000
        confirmation = analyze_step(rows)['rpm_settled_s']
        self.assertGreaterEqual(confirmation, 56)
        self.assertLessEqual(confirmation, 60)

    def test_late_rpm_excursion_rejects_earlier_steady_window(self):
        rows = samples()
        for t, row in rows:
            if t >= 170:
                row['mid_rpm'] = 6000
        self.assertIsNone(analyze_step(rows)['rpm_settled_s'])

    def test_missing_or_stale_cpu_is_unknown_load(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                rows = samples(temp=lambda t: 75-t/30)
                for _, row in rows:
                    if stale:
                        row['cpu_w_age_s'] = 8
                    else:
                        del row['cpu_w']
                result = analyze_step(rows)
                self.assertIsNone(result['load_stable'])
                self.assertEqual(result['status'], 'missing_data')
                self.assertIsNone(result['sensors']['board_c']['cooling_onset_s'])
                self.assertFalse(result['can_extend'])

    def test_persistent_rpm_ramp(self):
        rows = samples()
        for t, row in rows:
            row['mid_rpm'] = 2500+20*t
        result = analyze_step(rows)
        self.assertEqual(result['status'], 'rpm_unstable')
        self.assertIsNone(result['rpm_settled_s'])

    def test_missing_temperatures_and_telemetry_gaps(self):
        rows = samples()
        for _, row in rows:
            row['board_c'] = None
        self.assertEqual(analyze_step(rows)['status'], 'missing_data')
        rows = samples()
        rows = [(t, r) for t, r in rows if not 120<t<150]
        result = analyze_step(rows)
        self.assertNotEqual(result['status'], 'settled')
        self.assertFalse(result['can_extend'])

    def test_nonfinite_values_are_json_safe(self):
        rows = samples()
        for _, row in rows:
            row.update(cpu_w=math.nan, board_c=math.inf)
        result = analyze_step(rows)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['status'], 'missing_data')
        self.assertIsNone(result['sensors']['board_c']['slope_c_min'])

    def test_cooling_does_not_override_audio_failure(self):
        rows = samples(temp=lambda t: 75-t/30)
        rows[50][1]['stalls_per_s'] = 4
        result = analyze_step(rows)
        self.assertEqual(result['status'], 'cooling')
        self.assertFalse(result['audio_clean'])
        self.assertFalse(result['can_extend'])

    def test_short_sparse_and_observation(self):
        self.assertEqual(analyze_step(samples(20))['status'], 'short')
        result = analyze_step(samples()[::10])
        self.assertNotEqual(result['status'], 'settled')
        self.assertIsNone(result['audio_clean'])
        result = analyze_step(samples(temp=lambda t: 75-t/30), observe=True)
        self.assertEqual(result['status'], 'observe')
        self.assertFalse(result['can_extend'])
        self.assertIsNone(result['sensors']['board_c']['cooling_onset_s'])

    def test_missing_counter_is_unknown_audio(self):
        rows = samples()
        rows[10][1]['counter_ok'] = 0
        self.assertIsNone(analyze_step(rows)['audio_clean'])

    def test_large_oscillation_is_not_equilibrium(self):
        result = analyze_step(samples(temp=lambda t: 70+4*math.sin(t*math.pi/10)))
        self.assertNotEqual(result['status'], 'settled')

    def test_temperature_gaps_in_equilibrium_history(self):
        rows = samples()
        for t, row in rows:
            if 94 <= t <= 110:
                row['board_c'] = None
        self.assertNotEqual(analyze_step(rows)['status'], 'settled')

    def test_need_longer_post_stabilization_window(self):
        result = analyze_step(samples(90))
        self.assertEqual(result['status'], 'unsettled')


if __name__ == '__main__':
    unittest.main()
