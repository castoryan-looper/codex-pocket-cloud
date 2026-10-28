import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bridge


class LocationWeatherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.p = bridge.Pocket.__new__(bridge.Pocket)
        self.p.device = {}
        self.p.location_at = 0
        self.p.weather = None
        self.p.weather_at = 0
        self.p.weather_location_key = None
        self.p.weather_last_attempt = 0
        self.p.dirty = False
        self.location = dict(source='wifi-ip', status='ready', city='Tokyo', timezone='Asia/Tokyo',
                             latitude=35.68, longitude=139.69, utcOffsetSeconds=32400, ageSeconds=0)

    def factory(self, effect=None):
        response = MagicMock()
        response.json = AsyncMock(return_value={'current': {'temperature_2m': 18, 'weather_code': 3}}, side_effect=effect)
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        session.get.return_value.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        return factory, session

    async def test_device_coordinates_override_server_fixed_city(self):
        await self.p.device_packet({'type': 'hello', 'location': self.location}, 'device')
        factory, session = self.factory()
        with patch.dict(os.environ, POCKET_WEATHER_LAT='23.13', POCKET_WEATHER_LON='113.26'), patch('bridge.ClientSession', factory):
            await self.p.refresh_weather()
        params = session.get.call_args.kwargs['params']
        self.assertEqual((params['latitude'], params['longitude']), (35.68, 139.69))
        self.assertEqual(self.p.current_weather(), {'temperature': 18, 'code': 3})
        self.assertEqual(bridge.dashboard_time(self.p.current_location()).utcoffset().total_seconds(), 32400)

    async def test_unavailable_location_never_uses_fixed_city(self):
        self.p.accept_location({'source': 'wifi-ip', 'status': 'unavailable'})
        with patch.dict(os.environ, POCKET_WEATHER_LAT='23.13', POCKET_WEATHER_LON='113.26'), patch('bridge.ClientSession') as session:
            await self.p.refresh_weather()
        session.assert_not_called()
        self.assertIsNone(self.p.current_weather())

    def test_location_rejects_invalid_or_stale_samples(self):
        for update in ({'latitude': True}, {'longitude': float('nan')}, {'ageSeconds': 5400},
                       {'utcOffsetSeconds': 999999}, {'city': 'bad\ncity'}, {'timezone': '../etc/passwd'}):
            self.p.accept_location({**self.location, **update})
            self.assertIsNone(self.p.current_location(), update)

    async def test_wifi_change_invalidates_weather_and_drops_inflight_old_city(self):
        self.p.accept_location(self.location)
        async def switch():
            self.p.accept_location({**self.location, 'city': 'London', 'timezone': 'Europe/London',
                                    'latitude': 51.5, 'longitude': -0.12, 'utcOffsetSeconds': 0})
            return {'current': {'temperature_2m': 18, 'weather_code': 3}}
        factory, _ = self.factory(switch)
        with patch('bridge.ClientSession', factory):
            await self.p.refresh_weather()
        self.assertIsNone(self.p.current_weather())
        self.assertEqual(self.p.current_location()['city'], 'London')
        self.assertEqual(self.p.weather_last_attempt, -1800)

    def test_repeated_telemetry_does_not_refetch_same_city(self):
        self.p.accept_location(self.location)
        self.p.weather_last_attempt = 123
        self.p.accept_location({**self.location, 'ageSeconds': 5})
        self.assertEqual(self.p.weather_last_attempt, 123)
        self.p.location_at = time.monotonic() - 5401
        self.assertIsNone(self.p.current_location())

    def test_fractional_timezone_and_descriptions(self):
        location = {**self.location, 'timezone': 'Missing/Zone', 'utcOffsetSeconds': 20700}
        self.assertEqual(bridge.dashboard_time(location).utcoffset().total_seconds(), 20700)
        self.assertEqual(bridge.weather_description(61), '雨')
        self.assertEqual(bridge.weather_description(95), '雷雨')


if __name__ == '__main__':
    unittest.main()
