import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from voxi import VoxiRadio
from voxi_config import DEFAULTS, VoiceSettings, effective_prompt, validate

class ConfigurationTests(unittest.TestCase):
    def test_persistence_effective_prompt_and_snapshot_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = VoiceSettings(directory)
            value = settings.snapshot()
            value['systemPrompt'] = '请用中文完整讲故事。'
            value['channels'][2]['persona'] = '像温和的朋友一样讲话。'
            settings.save(value)
            restarted = VoiceSettings(directory)
            self.assertEqual(restarted.snapshot(), value)
            self.assertIn('温和的朋友', effective_prompt(restarted.snapshot(), 2))
            value['channels'][2]['name'] = 'modified'
            self.assertNotEqual(restarted.snapshot(), value)

    def test_invalid_values_and_failed_write_do_not_change_configuration(self):
        for key, invalid in [('responseIdleTimeoutSeconds', 0), ('replyEndSilenceMs', True), ('mediaStatsIntervalSeconds', 1), ('systemPrompt', ''), ('channels', []), ('audioRecoveryPackets', 63), ('audioRetransmission', 1)]:
            value = copy.deepcopy(DEFAULTS)
            value[key] = invalid
            with self.assertRaises(ValueError): validate(value)
        with tempfile.TemporaryDirectory() as directory:
            settings = VoiceSettings(directory)
            value = settings.snapshot();value['responseIdleTimeoutSeconds'] = 60
            with patch.object(Path, 'replace', side_effect=OSError('fixture')):
                with self.assertRaises(OSError): settings.save(value)
            self.assertEqual(settings.snapshot(), DEFAULTS)

class ConfigurationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_full_prompt_roundtrip_and_rejected_update(self):
        with tempfile.TemporaryDirectory() as directory:
            radio = VoxiRadio(SimpleNamespace(config={'device_token':'private'}, runtime=Path(directory)), 'unused')
            app = web.Application();app.router.add_get('/voxi/config', radio.configuration);app.router.add_post('/voxi/config', radio.configuration)
            async with TestClient(TestServer(app)) as client:
                self.assertEqual((await client.get('/voxi/config')).status, 403)
                headers={'Authorization':'Bearer private'}
                self.assertEqual((await client.get('/voxi/config',headers={**headers,'Origin':'https://other.example'})).status,403)
                current = await (await client.get('/voxi/config',headers=headers)).json()
                self.assertEqual(current['runtime']['voice'],'cove');self.assertEqual(len(current['effectivePrompts']),4)
                self.assertNotIn('private', json.dumps(current))
                value=current['settings'];value['systemPrompt']='长故事请完整说完。'
                response=await client.post('/voxi/config',headers=headers,json=value)
                self.assertEqual(response.status,200)
                self.assertIn('长故事', (await response.json())['effectivePrompts'][0])
                self.assertEqual(VoiceSettings(directory).snapshot(), value)
                value['voice']='unsupported'
                self.assertEqual((await client.post('/voxi/config',headers=headers,json=value)).status,400)
